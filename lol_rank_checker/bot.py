import discord
import asyncio
import traceback
import os
from discord.ext import commands
from riotwatcher import LolWatcher, RiotWatcher, ApiError
from keep_alive import keep_alive

# ==========================================
# 設定項目
# ==========================================
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
RIOT_API_KEY = os.getenv('RIOT_API_KEY')
ADMIN_USER_ID = 269068756075020288  # 通知を送る管理者のDiscord User ID
GUILD_ID = 1445037162968907890  # 対象のサーバーID

ROLE_MEMBER = "Member"
ROLE_WAITING = "waiting_review"

REGION_PLATFORM = 'jp1'
REGION_ACCOUNT = 'asia'

# 基準値設定
current_mode = "BEGINNER"
THRESHOLDS = {
    "BEGINNER": {
        "name": "🔰 初心者帯 (Iron/Bronze)",
        "win_rate": 60, "kda": 4.0, "cspm": 7.0, "gpm": 450, "dmg": 30.0
    },
    "INTERMEDIATE": {
        "name": "🛡️ 中級者帯 (Silver/Gold)",
        "win_rate": 60, "kda": 4.5, "cspm": 7.5, "gpm": 500, "dmg": 32.0
    },
    "ADVANCED": {
        "name": "⚔️ 上級者帯 (Plat+)",
        "win_rate": 65, "kda": 5.0, "cspm": 8.5, "gpm": 550, "dmg": 35.0
    }
}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='/', intents=intents)

if not RIOT_API_KEY:
    print("⚠️ RIOT_API_KEY が設定されていません。")
    lol_watcher = LolWatcher('dummy')
    riot_watcher = RiotWatcher('dummy')
else:
    lol_watcher = LolWatcher(RIOT_API_KEY)
    riot_watcher = RiotWatcher(RIOT_API_KEY)


# ==========================================
# 戦績分析ロジック (トロール検知追加版)
# ==========================================
async def analyze_player_stats(riot_id_name, riot_id_tag):
    config = THRESHOLDS[current_mode]
    try:
        print(f"--- 集計開始: {riot_id_name}#{riot_id_tag} ---")

        account = riot_watcher.account.by_riot_id(REGION_ACCOUNT, riot_id_name, riot_id_tag)
        puuid = account.get('puuid')
        if not puuid: return {"status": "ERROR", "reason": "PUUID取得不可", "data": locals()}

        summoner = lol_watcher.summoner.by_puuid(REGION_PLATFORM, puuid)
        acct_level = summoner.get('summonerLevel', 0)

        matches = lol_watcher.match.matchlist_by_puuid(REGION_ACCOUNT, puuid, count=20)
        match_count = len(matches)

        if match_count == 0:
            return {"status": "REVIEW", "reason": "試合データなし", "data": locals()}

        # 集計用変数
        wins = 0
        recent_10_wins = 0
        total_kills = 0;
        total_deaths = 0;
        total_assists = 0
        total_cspm = 0;
        total_gpm = 0;
        total_dmg_share = 0
        valid_game_count = 0

        # ★トロール検知用カウンタ
        high_death_games = 0  # 12デス以上の試合数
        no_item_games = 0  # アイテム放棄試合数
        low_dmg_games = 0  # ダメージ放棄試合数(5%未満)
        ff_games = 0  # 20分未満での敗北(早期サレンダー)

        for idx, match_id in enumerate(matches):
            await asyncio.sleep(0.5)
            try:
                match_detail = lol_watcher.match.by_id(REGION_ACCOUNT, match_id)
            except:
                continue

            game_duration = match_detail['info']['gameDuration']
            if game_duration < 300: continue  # Remake除外

            game_duration_min = game_duration / 60
            valid_game_count += 1
            participants = match_detail['info']['participants']

            # 自分のデータ取得
            my_part = None
            team_total_dmg = 0
            my_team_id = 0

            for p in participants:
                if p['puuid'] == puuid:
                    my_part = p
                    my_team_id = p['teamId']

            # 味方総ダメージ計算 (後で使う)
            for p in participants:
                if p['teamId'] == my_team_id:
                    team_total_dmg += p['totalDamageDealtToChampions']

            if my_part:
                # 1. 基本スタッツ
                if my_part['win']:
                    wins += 1
                    if idx < 10: recent_10_wins += 1
                else:
                    # 敗北時に時間が短い = FFの可能性大 (15分〜20分)
                    if game_duration < 1200:
                        ff_games += 1

                total_kills += my_part['kills']
                total_deaths += my_part['deaths']
                total_assists += my_part['assists']

                cs = my_part['totalMinionsKilled'] + my_part['neutralMinionsKilled']
                total_cspm += (cs / game_duration_min)
                total_gpm += (my_part['goldEarned'] / game_duration_min)

                # 2. ダメージ比率
                my_dmg = my_part['totalDamageDealtToChampions']
                dmg_share = 0
                if team_total_dmg > 0:
                    dmg_share = (my_dmg / team_total_dmg) * 100
                    total_dmg_share += dmg_share

                # --- ★トロール判定カウント ---

                # A. 過度なデス (Feed)
                if my_part['deaths'] >= 12:
                    high_death_games += 1

                # B. アイテム売却 (トロールビルド)
                # アイテムスロット(item0~5)が空っぽかどうか
                item_count = 0
                for i in range(6):
                    if my_part.get(f'item{i}', 0) != 0:
                        item_count += 1
                if item_count <= 1 and game_duration > 600:  # 10分以上でアイテム1個以下
                    no_item_games += 1

                # C. ダメージ放棄 (Sup以外で極端に低い)
                # (ロール判定は難しいので一律判定だが、Supでも5%は超えるはず)
                if dmg_share < 5.0:
                    low_dmg_games += 1

        if valid_game_count == 0:
            return {"status": "REVIEW", "reason": "有効な試合データなし", "data": locals()}

        # 平均計算
        win_rate = (wins / valid_game_count) * 100
        avg_deaths = total_deaths if total_deaths > 0 else 1
        kda = (total_kills + total_assists) / avg_deaths
        avg_cspm = total_cspm / valid_game_count
        avg_gpm = total_gpm / valid_game_count
        avg_dmg_share = total_dmg_share / valid_game_count

        data_snapshot = {
            "riot_id": f"{riot_id_name}#{riot_id_tag}",
            "level": acct_level,
            "win_rate": round(win_rate, 1),
            "kda": round(kda, 2),
            "cspm": round(avg_cspm, 1),
            "gpm": round(avg_gpm, 0),
            "dmg_share": round(avg_dmg_share, 1),
            "matches": valid_game_count,
        }

        # --- 判定ロジック ---
        reasons = []

        # 1. スマーフ・代行判定 (既存)
        if win_rate >= config["win_rate"]: reasons.append(f"⚠️高勝率({round(win_rate)}%)")
        if kda >= config["kda"]: reasons.append(f"⚠️高KDA({round(kda, 2)})")
        if avg_cspm >= config["cspm"]: reasons.append(f"⚠️高CS({round(avg_cspm, 1)}/分)")
        if avg_dmg_share >= config["dmg"]: reasons.append(f"⚠️高ダメ比率({round(avg_dmg_share)}%)")
        if avg_gpm >= config["gpm"]: reasons.append(f"⚠️金持ち({round(avg_gpm)}G/分)")
        if acct_level < 50: reasons.append(f"⚠️低Lv(Lv{acct_level})")
        if recent_10_wins >= 8: reasons.append("⚠️直近8勝以上")

        # 2. ★トロール・トキシック判定 (新規追加)

        # デス過多: 全試合の30%以上で12デッド以上している
        if high_death_games >= (valid_game_count * 0.3):
            reasons.append(f"💀フィード気味({high_death_games}試合で12Death超)")

        # アイテム放棄
        if no_item_games >= 1:
            reasons.append(f"💀アイテム放棄検出({no_item_games}試合)")

        # ダメージなし (AFK疑惑)
        if low_dmg_games >= 2:
            reasons.append(f"💀寄生・AFK疑惑({low_dmg_games}試合でDmg5%未満)")

        # 早期サレンダー率が高い (メンタル弱い)
        # 敗北試合の50%以上が早期サレンダー
        losses = valid_game_count - wins
        if losses > 0 and (ff_games / losses) >= 0.5:
            reasons.append(f"💀早期FF多め({ff_games}回)")

        if not reasons:
            reasons.append("基準内")

        return {"status": "REVIEW", "reason": ", ".join(reasons), "data": data_snapshot}

    except Exception as e:
        print(traceback.format_exc())
        return {"status": "ERROR", "reason": f"システムエラー: {e}"}


# ==========================================
# Discord コマンド
# ==========================================
@bot.event
async def on_ready():
    print(f'Bot is ready: {bot.user.name}')


@bot.command()
async def set_mode(ctx, mode_name: str = None):
    if ctx.author.id != ADMIN_USER_ID: return
    global current_mode
    if mode_name is None:
        msg = f"📊 **現在の設定:** `{THRESHOLDS[current_mode]['name']}`\n`/set_mode beginner/intermediate/advanced`"
        await ctx.send(msg)
        return
    key = mode_name.upper()
    if key in THRESHOLDS:
        current_mode = key
        await ctx.send(f"✅ 設定変更: `{THRESHOLDS[key]['name']}`")
    else:
        await ctx.send("❌ 無効なモードです")


@bot.command()
async def link(ctx, riot_id_str):
    if '#' not in riot_id_str:
        await ctx.send("❌ 形式エラー: `Name#Tag`")
        return

    name, tag = riot_id_str.split('#', 1)
    await ctx.send(f"📊 `{name}#{tag}` を分析中... (モード: {current_mode})")

    result = await analyze_player_stats(name, tag)
    status = result['status']

    if status == "ERROR":
        await ctx.send(f"❌ エラー: {result['reason']}")
        return

    member = ctx.author
    role_waiting = discord.utils.get(ctx.guild.roles, name=ROLE_WAITING)

    if status == "REVIEW":
        if role_waiting: await member.add_roles(role_waiting)
        await ctx.send("📋 集計完了。管理者通知を確認してください。")

        try:
            admin_user = await bot.fetch_user(ADMIN_USER_ID)
            if admin_user:
                d = result['data']
                opgg_link = f"https://www.op.gg/summoners/jp/{name}-{tag}"

                # 理由に💀が含まれていたらトロール警告を見出しにする
                alert_emoji = "🚨" if "💀" in result['reason'] else "⚠️"

                msg = (
                    f"**【{alert_emoji} 新規申請 / {THRESHOLDS[current_mode]['name']}】**\n"
                    f"対象: {member.mention}\n"
                    f"ID: `{d['riot_id']}`\n"
                    f"Lv: {d['level']}\n"
                    f"勝率: **{d['win_rate']}%**\n"
                    f"KDA: **{d['kda']}**\n"
                    f"CS/分: **{d['cspm']}**\n"
                    f"判定: {result['reason']}\n\n"
                    f"🔗 [OP.GG]({opgg_link})\n\n"
                    f"`/approve {member.id}` / `/reject {member.id}`"
                )
                await admin_user.send(msg)
        except:
            pass


@bot.command()
async def approve(ctx, user_id: int):
    if ctx.author.id != ADMIN_USER_ID: return
    member = ctx.guild.get_member(user_id)
    if member:
        role_mem = discord.utils.get(ctx.guild.roles, name=ROLE_MEMBER)
        role_wait = discord.utils.get(ctx.guild.roles, name=ROLE_WAITING)
        if role_wait in member.roles: await member.remove_roles(role_wait)
        if role_mem: await member.add_roles(role_mem)
        await ctx.send(f"✅ {member.display_name} を承認しました")


@bot.command()
async def reject(ctx, user_id: int):
    if ctx.author.id != ADMIN_USER_ID: return
    member = ctx.guild.get_member(user_id)
    if member:
        await ctx.guild.kick(member, reason="審査拒否")
        await ctx.send(f"🚫 {member.display_name} を拒否しました")


keep_alive()
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)