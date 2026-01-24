import discord
import asyncio
import traceback
import os
import csv
import io
import datetime
import certifi
from discord.ext import commands
from riotwatcher import LolWatcher, RiotWatcher, ApiError
from pymongo import MongoClient
from keep_alive import keep_alive

# ==========================================
# 設定項目 & DB接続
# ==========================================
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
RIOT_API_KEY = os.getenv('RIOT_API_KEY')
MONGO_URL = os.getenv('MONGO_URL')

ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', 0))
GUILD_ID = int(os.getenv('GUILD_ID', 0))

current_admin_id = ADMIN_USER_ID
current_guild_id = GUILD_ID

# ロール設定
ROLE_MEMBER = "Member"
ROLE_WAITING = "waiting_review"
ROLE_ADVISOR = "助言者"

REGION_PLATFORM = 'jp1'
REGION_ACCOUNT = 'asia'
MAX_LEVEL = 150

# モード設定
# ここでの設定値は「このティアの適正上限（これを超えると強すぎる）」を表します
current_mode = "BEGINNER"
THRESHOLDS = {
    "BEGINNER": {"name": "🔰 初心者帯 (Iron/Bronze)", "win_rate": 60, "kda": 4.0, "cspm": 7.0, "gpm": 450, "dmg": 30.0},
    "INTERMEDIATE": {"name": "🛡️ 中級者帯 (Silver/Gold)", "win_rate": 60, "kda": 4.5, "cspm": 7.5, "gpm": 500,
                     "dmg": 32.0},
    "ADVANCED": {"name": "⚔️ 上級者帯 (Plat+)", "win_rate": 65, "kda": 5.0, "cspm": 8.5, "gpm": 550, "dmg": 35.0}
}

# ==========================================
# 初期化
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='/', intents=intents)

if not RIOT_API_KEY:
    lol_watcher = LolWatcher('dummy')
    riot_watcher = RiotWatcher('dummy')
else:
    lol_watcher = LolWatcher(RIOT_API_KEY)
    riot_watcher = RiotWatcher(RIOT_API_KEY)

# MongoDB接続
mongo_client = None
db = None
users_col = None

if MONGO_URL:
    try:
        mongo_client = MongoClient(MONGO_URL, tlsCAFile=certifi.where())
        db = mongo_client.lol_bot_db
        users_col = db.users
        print("✅ MongoDB接続成功")
    except Exception as e:
        print(f"❌ MongoDB接続エラー: {e}")


# ==========================================
# 補助関数
# ==========================================
def is_admin_or_owner(ctx):
    return ctx.author.id == current_admin_id or ctx.author.id == ctx.guild.owner_id


def save_user_to_db(discord_id, riot_name, riot_tag, puuid, level):
    if users_col is None: return
    now = datetime.datetime.now()
    user_data = {
        "discord_id": discord_id,
        "riot_name": riot_name,
        "riot_tag": riot_tag,
        "puuid": puuid,
        "level": level,
        "last_updated": now
    }
    users_col.update_one({"discord_id": discord_id}, {"$set": user_data}, upsert=True)
    print(f"💾 DB保存完了: {riot_name}#{riot_tag}")


# ==========================================
# 分析ロジック
# ==========================================
async def analyze_player_stats(riot_id_name, riot_id_tag, discord_id_for_save=None, is_exempt=False):
    config = THRESHOLDS[current_mode]
    try:
        account = riot_watcher.account.by_riot_id(REGION_ACCOUNT, riot_id_name, riot_id_tag)
        puuid = account.get('puuid')
        if not puuid: return {"status": "ERROR", "reason": "PUUID取得不可", "data": locals()}

        summoner = lol_watcher.summoner.by_puuid(REGION_PLATFORM, puuid)
        acct_level = summoner.get('summonerLevel', 0)

        if discord_id_for_save:
            save_user_to_db(discord_id_for_save, riot_id_name, riot_id_tag, puuid, acct_level)

        if not is_exempt and acct_level >= MAX_LEVEL:
            return {"status": "GRADUATE", "reason": f"レベル上限超過 (Lv{acct_level})",
                    "data": {"riot_id": f"{riot_id_name}#{riot_id_tag}", "level_raw": acct_level}}

        matches = lol_watcher.match.matchlist_by_puuid(REGION_ACCOUNT, puuid, count=20)
        if not matches:
            return {"status": "REVIEW", "reason": "試合データなし", "data": locals()}

        wins = 0;
        valid = 0
        kills = 0;
        deaths = 0;
        assists = 0
        cspm = 0;
        gpm = 0;
        dmg_share = 0
        troll_deaths = 0;
        troll_items = 0;
        troll_dmg = 0;
        troll_ff = 0

        for match_id in matches:
            await asyncio.sleep(0.5)
            try:
                match = lol_watcher.match.by_id(REGION_ACCOUNT, match_id)
            except:
                continue

            info = match['info']
            if info['gameDuration'] < 300: continue

            valid += 1
            duration_min = info['gameDuration'] / 60

            me = next((p for p in info['participants'] if p['puuid'] == puuid), None)
            if not me: continue

            team_dmg = sum(
                p['totalDamageDealtToChampions'] for p in info['participants'] if p['teamId'] == me['teamId'])

            if me['win']:
                wins += 1
            elif info['gameDuration'] < 1200:
                troll_ff += 1

            kills += me['kills']
            deaths += me['deaths']
            assists += me['assists']

            cs = me['totalMinionsKilled'] + me['neutralMinionsKilled']
            cspm += cs / duration_min
            gpm += me['goldEarned'] / duration_min

            if team_total_dmg := team_dmg:
                dmg_share += (me['totalDamageDealtToChampions'] / team_total_dmg) * 100

            if me['deaths'] >= 12: troll_deaths += 1
            item_cnt = sum(1 for i in range(6) if me.get(f'item{i}', 0) != 0)
            if item_cnt <= 1 and duration_min > 10: troll_items += 1
            if team_total_dmg > 0 and (me['totalDamageDealtToChampions'] / team_total_dmg) * 100 < 5.0: troll_dmg += 1

        if valid == 0: return {"status": "REVIEW", "reason": "有効データなし", "data": locals()}

        win_rate = (wins / valid) * 100
        avg_kda = (kills + assists) / (deaths if deaths > 0 else 1)
        avg_cspm = cspm / valid
        avg_gpm = gpm / valid
        avg_dmg = dmg_share / valid

        def fmt(val, thresh, unit="", low_bad=False):
            s = f"{round(val, 1)}"
            is_bad = val < thresh if low_bad else val >= thresh
            return f"⚠️ **{s}{unit}**" if is_bad else f"{s}{unit}"

        trolls = []
        if troll_deaths >= valid * 0.3: trolls.append(f"💀OverDeath({troll_deaths})")
        if troll_items >= 1: trolls.append(f"💀NoItem")
        if troll_dmg >= 2: trolls.append(f"💀LowDmg")
        if (valid - wins) > 0 and (troll_ff / (valid - wins)) >= 0.5: trolls.append(f"💀EarlyFF")

        data_snapshot = {
            "riot_id": f"{riot_id_name}#{riot_id_tag}",
            "level_raw": acct_level,
            "fmt_level": fmt(acct_level, 50, "", True),
            "fmt_win": fmt(win_rate, config["win_rate"], "%"),
            "fmt_kda": fmt(avg_kda, config["kda"]),
            "fmt_cspm": fmt(avg_cspm, config["cspm"]),
            "fmt_gpm": fmt(avg_gpm, config["gpm"]),
            "fmt_dmg": fmt(avg_dmg, config["dmg"], "%"),
            "troll": " / ".join(trolls) if trolls else "なし",
            "matches": valid
        }

        return {"status": "REVIEW", "reason": "完了", "data": data_snapshot}


# ==========================================
# コマンド群
# ==========================================
@bot.event
async def on_ready():
    print(f'Bot is ready: {bot.user.name}')


# --- ★ 用語・基準値解説コマンド (NEW) ---
@bot.command()
async def standards(ctx):
    """📊 このBotが使用している指標と、ランク帯ごとの基準値を詳しく解説します"""

    # 用語解説のEmbed
    embed_term = discord.Embed(title="📖 LoL戦績指標の解説", description="Botの審査で使用している各数値の意味です。",
                               color=discord.Color.green())
    embed_term.add_field(name="⚔️ KDA (Kill Death Assist)",
                         value="`(キル + アシスト) ÷ デス` の数値。\n戦闘への貢献度と生存能力を表します。\n**目安:** 3.0以上で優秀、4.0を超えると非常に強力です。",
                         inline=False)
    embed_term.add_field(name="🌾 CS/min (CSPM)",
                         value="`1分間あたりのミニオン撃破数`。\nファーム(育成)の速度を表す最も重要な指標です。\n**目安:** 6.0以上で安定、7.0以上はキャリーの素質があります。",
                         inline=False)
    embed_term.add_field(name="💰 GPM (Gold Per Minute)",
                         value="`1分間あたりの獲得ゴールド`。\nキル、CS、タワー破壊などを含めた「稼ぐ力」です。\n**目安:** 400前後が一般的。450を超えると装備が早く揃います。",
                         inline=False)
    embed_term.add_field(name="💥 DMG% (Damage Share)",
                         value="`チーム全体のダメージに対する自分の割合`。\n集団戦でどれだけ火力を出したかを表します。\n**目安:** 20%で平均、30%を超えるとチームのエース級です。",
                         inline=False)

    await ctx.send(embed=embed_term)

    # 基準値一覧のEmbed
    embed_std = discord.Embed(title="⚖️ ランク帯別・スマーフ検知ライン",
                              description="以下の数値を超えている場合、そのランク帯の適正レベルを超えている(強すぎる)と判定され、警告が出ます。",
                              color=discord.Color.orange())

    # 各モードのデータをループで表示
    for key, data in THRESHOLDS.items():
        text = (
            f"**勝率:** {data['win_rate']}% 以上\n"
            f"**KDA:** {data['kda']} 以上\n"
            f"**CS/分:** {data['cspm']} 以上\n"
            f"**GPM:** {data['gpm']} 以上\n"
            f"**DMG%:** {data['dmg']}% 以上"
        )
        embed_std.add_field(name=data['name'], value=text, inline=True)

    embed_std.set_footer(text=f"現在のモード設定: {THRESHOLDS[current_mode]['name']}")
    await ctx.send(embed=embed_std)


@bot.command()
async def manual(ctx):
    """📘 コマンド一覧を見やすく表示します"""
    embed = discord.Embed(title="📜 Botコマンド一覧", description="利用可能なコマンドのマニュアルです。",
                          color=discord.Color.blue())

    general_cmds = (
        "**/link Name#Tag**\n"
        "自分のRiotアカウントを紐付けて審査を申請します。\n"
        "例: `/link Hide on bush#KR1`\n\n"
        "**/standards**\n"
        "KDAやGPMなどの用語解説と、合格/警告ラインの基準値を表示します。★New\n\n"
        "**/list**\n"
        "登録済みメンバーのOP.GGリンク集を表示します。"
    )
    embed.add_field(name="🔰 一般・メンバー用", value=general_cmds, inline=False)

    if is_admin_or_owner(ctx):
        admin_cmds = (
            "**--- 審査・人事 ---**\n"
            "`/approve [ID]` : 承認 (メンバー化)\n"
            "`/reject [ID]` : 拒否 (Kick)\n"
            "`/graduate [ID]` : Lv上限卒業 (Kick+DM)\n"
            "`/graduate_rank [ID]` : ランク昇格卒業 (Kick+祝いDM)\n\n"
            "**--- 管理・分析 ---**\n"
            "`/audit` : 全員を一括再検査 (助言者はスルー)\n"
            "`/export` : 名簿をExcel用CSVで出力\n"
            "`/set_mode` : 基準変更\n"
            "`/settings` : 設定確認"
        )
        embed.add_field(name="👑 管理者用 (Admin Only)", value=admin_cmds, inline=False)

    await ctx.send(embed=embed)


# --- 通常コマンド ---
@bot.command()
async def link(ctx, riot_id_str):
    """📝 Riotアカウントを紐付けて審査を申請します (例: /link Name#Tag)"""
    if '#' not in riot_id_str:
        await ctx.send("❌ `Name#Tag` で入力してください")
        return
    if current_guild_id != 0 and ctx.guild.id != current_guild_id:
        await ctx.send("⚠️ 対象外サーバーです")
        return

    role_advisor = discord.utils.get(ctx.guild.roles, name=ROLE_ADVISOR)
    is_exempt = False
    if role_advisor and role_advisor in ctx.author.roles:
        is_exempt = True

    name, tag = riot_id_str.split('#', 1)
    await ctx.send(f"📊 `{name}#{tag}` を分析中... {'(助言者モード)' if is_exempt else ''}")

    result = await analyze_player_stats(name, tag, ctx.author.id, is_exempt=is_exempt)
    status = result['status']

    if status == "ERROR":
        await ctx.send(f"❌ エラー: {result['reason']}")
        return

    member = ctx.author

    if status == "GRADUATE":
        await ctx.send("🎓 レベル上限を超えているため、卒業対象となります。")
        try:
            admin = await bot.fetch