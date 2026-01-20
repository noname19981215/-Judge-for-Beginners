import discord
import asyncio
import traceback
import os  # 【変更点1】Renderの設定を読み込むためのライブラリ
from discord.ext import commands
from riotwatcher import LolWatcher, RiotWatcher, ApiError
from keep_alive import keep_alive  # 【変更点1】Webサーバー機能を読み込む

# ==========================================
# 設定項目 (Renderの環境変数から読み込む)
# ==========================================
# 【変更点2】直接キーを書かず、Renderの設定画面から読み込むように変更
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
RIOT_API_KEY = os.getenv('RIOT_API_KEY')

# ↓ ID類は他人に知られても問題ないので、そのままでOKです
ADMIN_USER_ID = 269068756075020288  # あなたのDiscord User ID
GUILD_ID = 1445037162968907890  # サーバーID

ROLE_MEMBER = "Member"
ROLE_WAITING = "waiting_review"

REGION_PLATFORM = 'jp1'
REGION_ACCOUNT = 'asia'

# ==========================================
# Bot & API 初期化
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='/', intents=intents)

# APIキーがない状態で起動するとエラーになるため、取得できたかチェック
if not RIOT_API_KEY:
    print("⚠️ 注意: RIOT_API_KEY が設定されていません。RenderのEnvironment Variablesを確認してください。")
    # エラー回避のためダミーを入れるか、ここで処理を止める
    # (ここではとりあえず空で初期化しますが、APIを叩くとエラーになります)
    lol_watcher = LolWatcher('dummy')
    riot_watcher = RiotWatcher('dummy')
else:
    lol_watcher = LolWatcher(RIOT_API_KEY)
    riot_watcher = RiotWatcher(RIOT_API_KEY)


# ==========================================
# 戦績分析ロジック
# ==========================================
async def analyze_player_stats(riot_id_name, riot_id_tag):
    try:
        print(f"--- データ集計開始: {riot_id_name}#{riot_id_tag} ---")

        # 1. Riot ID -> PUUID
        account = riot_watcher.account.by_riot_id(REGION_ACCOUNT, riot_id_name, riot_id_tag)
        puuid = account.get('puuid')
        if not puuid:
            return {"status": "ERROR", "reason": "PUUID取得不可", "data": locals()}

        # 2. アカウントレベル取得
        summoner = lol_watcher.summoner.by_puuid(REGION_PLATFORM, puuid)
        acct_level = summoner.get('summonerLevel', 0)

        # 3. 直近20試合の戦績取得
        matches = lol_watcher.match.matchlist_by_puuid(REGION_ACCOUNT, puuid, count=20)
        match_count = len(matches)

        if match_count == 0:
            return {"status": "REVIEW", "reason": "試合データなし(Unranked?)", "data": locals()}

        wins = 0
        total_kills = 0
        total_deaths = 0
        total_assists = 0
        recent_10_wins = 0

        # 試合データ集計ループ
        for idx, match_id in enumerate(matches):
            await asyncio.sleep(0.5)

            try:
                match_detail = lol_watcher.match.by_id(REGION_ACCOUNT, match_id)
            except Exception:
                continue

            if 'info' in match_detail and 'participants' in match_detail['info']:
                for participant in match_detail['info']['participants']:
                    if participant['puuid'] == puuid:
                        # 勝敗
                        if participant['win']:
                            wins += 1
                            if idx < 10: recent_10_wins += 1

                        # KDA
                        total_kills += participant['kills']
                        total_deaths += participant['deaths']
                        total_assists += participant['assists']
                        break

        # 指標計算
        win_rate = (wins / match_count) * 100 if match_count > 0 else 0
        avg_deaths = total_deaths if total_deaths > 0 else 1
        kda = (total_kills + total_assists) / avg_deaths

        data_snapshot = {
            "riot_id": f"{riot_id_name}#{riot_id_tag}",
            "level": acct_level,
            "win_rate": round(win_rate, 1),
            "kda": round(kda, 2),
            "matches": match_count,
            "recent_10_wins": recent_10_wins
        }

        reasons = []
        if win_rate >= 60: reasons.append(f"⚠️高勝率({round(win_rate)}%)")
        if kda >= 4.0: reasons.append(f"⚠️高KDA({round(kda, 2)})")
        if acct_level < 50: reasons.append(f"⚠️低レベル(Lv{acct_level})")
        if recent_10_wins >= 8: reasons.append("⚠️直近絶好調(8勝以上)")

        if not reasons: reasons.append("戦績は平均的 (要ランク確認)")

        return {"status": "REVIEW", "reason": ", ".join(reasons), "data": data_snapshot}

    except ApiError as err:
        if err.response.status_code == 404:
            return {"status": "ERROR", "reason": "Riot IDが見つかりません"}
        elif err.response.status_code == 429:
            return {"status": "ERROR", "reason": "API制限中。時間を置いてください。"}
        return {"status": "ERROR", "reason": f"APIエラー: {err.response.status_code}"}
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
async def link(ctx, riot_id_str):
    if '#' not in riot_id_str:
        await ctx.send("❌ 形式エラー: `Name#Tag` で入力してください。")
        return

    name, tag = riot_id_str.split('#', 1)
    await ctx.send(f"📊 `{name}#{tag}` の戦績を集計中... (約10秒)")

    result = await analyze_player_stats(name, tag)
    status = result['status']

    if status == "ERROR":
        await ctx.send(f"❌ エラー: {result['reason']}")
        return

    member = ctx.author
    guild = ctx.guild
    role_waiting = discord.utils.get(guild.roles, name=ROLE_WAITING)

    if not role_waiting:
        await ctx.send("⚠️ 設定エラー: waiting_review ロールがありません")
        return

    if status == "REVIEW":
        await member.add_roles(role_waiting)
        await ctx.send("📋 戦績を集計しました。管理者の承認をお待ちください。")

        try:
            admin_user = await bot.fetch_user(ADMIN_USER_ID)
            if admin_user:
                d = result['data']
                opgg_link = f"https://www.op.gg/summoners/jp/{name}-{tag}"

                msg = (
                    f"**【新規参加申請】**\n"
                    f"対象: {member.mention}\n"
                    f"ID: `{d['riot_id']}`\n"
                    f"Lv: {d['level']}\n"
                    f"勝率: **{d['win_rate']}%** (直近20戦)\n"
                    f"KDA: **{d['kda']}**\n"
                    f"判定メモ: {result['reason']}\n\n"
                    f"🔗 [OP.GGでランクを確認]({opgg_link})\n\n"
                    f"操作:\n`/approve {member.id}` (承認)\n`/reject {member.id}` (拒否)"
                )
                await admin_user.send(msg)
        except Exception as e:
            print(f"通知エラー: {e}")


@bot.command()
async def approve(ctx, user_id: int):
    if ctx.author.id != ADMIN_USER_ID: return
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user_id)
    if member:
        role_member = discord.utils.get(guild.roles, name=ROLE_MEMBER)
        role_waiting = discord.utils.get(guild.roles, name=ROLE_WAITING)

        if role_waiting in member.roles:
            await member.remove_roles(role_waiting)
        if role_member:
            await member.add_roles(role_member)

        await ctx.send(f"✅ {member.display_name} を承認しました。")


@bot.command()
async def reject(ctx, user_id: int):
    if ctx.author.id != ADMIN_USER_ID: return
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user_id)
    if member:
        await guild.kick(member, reason="審査拒否")
        await ctx.send(f"🚫 {member.display_name} を拒否(Kick)しました。")


# ==========================================
# 起動処理
# ==========================================
keep_alive()  # 【変更点3】Botを起動する前にWebサーバーを立ち上げる

if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("❌ エラー: DISCORD_TOKEN が設定されていません。")