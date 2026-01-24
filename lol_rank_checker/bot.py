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
MAX_LEVEL = 500

# モード設定
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

    except Exception as e:
        print(traceback.format_exc())
        return {"status": "ERROR", "reason": f"エラー: {e}"}


# ==========================================
# コマンド群
# ==========================================
@bot.event
async def on_ready():
    print(f'Bot is ready: {bot.user.name}')


# --- ★ コマンド一覧表示コマンド (NEW) ---
@bot.command()
async def manual(ctx):
    """コマンド一覧を表示"""
    embed = discord.Embed(title="📜 Botコマンド一覧", description="利用可能なコマンドのマニュアルです。",
                          color=discord.Color.blue())

    # 誰でも使えるコマンド
    general_cmds = (
        "**/link Name#Tag**\n"
        "自分のRiotアカウントを紐付けて審査を申請します。\n"
        "例: `/link Hide on bush#KR1`\n\n"
        "**/list**\n"
        "登録済みメンバーのOP.GGリンク集を表示します。\n"
        "スマホからでもワンタップで戦績を確認できます。"
    )
    embed.add_field(name="🔰 一般・メンバー用", value=general_cmds, inline=False)

    # 管理者用 (権限がある人だけに見えるようにする)
    if is_admin_or_owner(ctx):
        admin_cmds = (
            "**--- 審査・人事 ---**\n"
            "`/approve [ID]` : 申請を承認してメンバー化\n"
            "`/reject [ID]` : 申請を拒否 (Kick)\n"
            "`/graduate [ID]` : 卒業させる (Kick+DM)\n\n"
            "**--- 管理・分析 ---**\n"
            "`/audit` : 全員のレベルを一括再検査 (助言者はスルー)\n"
            "`/export` : 名簿をExcel用CSVで出力\n"
            "`/set_mode [beginner/intermediate/advanced]` : 基準変更\n"
            "`/settings` : Botの設定確認・管理者変更"
        )
        embed.add_field(name="👑 管理者用 (Admin Only)", value=admin_cmds, inline=False)

    await ctx.send(embed=embed)


# --- 通常コマンド ---
@bot.command()
async def link(ctx, riot_id_str):
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
            admin = await bot.fetch_user(current_admin_id)
            if admin:
                d = result['data']
                await admin.send(
                    f"**【🎓 卒業推奨】**\n対象: {member.mention}\nID: `{d['riot_id']}`\nLv: **{d['level_raw']}** (上限:{MAX_LEVEL})\n`/graduate {member.id}`")
        except:
            pass
        return

    role_waiting = discord.utils.get(ctx.guild.roles, name=ROLE_WAITING)
    if role_waiting: await member.add_roles(role_waiting)

    await ctx.send("📋 集計完了。管理者の承認をお待ちください。")
    try:
        admin = await bot.fetch_user(current_admin_id)
        if admin:
            d = result['data']
            opgg = f"https://www.op.gg/summoners/jp/{name}-{tag}"
            cfg = THRESHOLDS[current_mode]

            advisor_mark = "🔰(助言者/免除)" if is_exempt else f"{cfg['name']}"

            msg = (
                f"**【新規申請 / {advisor_mark}】**\n対象: {member.mention}\nID: `{d['riot_id']}`\n"
                f"Lv:{d['fmt_level']} Win:{d['fmt_win']} KDA:{d['fmt_kda']}\n"
                f"CS:{d['fmt_cspm']} GPM:{d['fmt_gpm']} Dmg:{d['fmt_dmg']}\n"
                f"警告: {d['troll']}\n🔗 [OP.GG]({opgg})\n`/approve {member.id}` / `/reject {member.id}`"
            )
            await admin.send(msg)
    except:
        pass


@bot.command()
async def audit(ctx):
    if not is_admin_or_owner(ctx): return
    if not users_col: return await ctx.send("❌ DB未接続")

    msg = await ctx.send("🔍 全員分の最新データを取得中... (助言者はスキップします)")
    users = list(users_col.find())
    graduates = []

    role_advisor = discord.utils.get(ctx.guild.roles, name=ROLE_ADVISOR)

    for u in users:
        member = ctx.guild.get_member(u['discord_id'])
        if member and role_advisor and role_advisor in member.roles:
            continue

        await asyncio.sleep(1.2)
        try:
            summ = lol_watcher.summoner.by_puuid(REGION_PLATFORM, u['puuid'])
            new_level = summ['summonerLevel']

            if new_level != u['level']:
                users_col.update_one({"_id": u['_id']}, {"$set": {"level": new_level}})

            if new_level >= MAX_LEVEL:
                graduates.append(f"<@{u['discord_id']}> (Lv.{new_level})")
        except Exception as e:
            print(f"Error checking {u['riot_name']}: {e}")
            continue

    if graduates:
        await ctx.send(f"⚠️ **卒業対象者が見つかりました:**\n" + "\n".join(graduates))
    else:
        await ctx.send("✅ 全員レベル基準内です。")


@bot.command()
async def approve(ctx, user_id: int):
    if ctx.author.id != current_admin_id: return
    member = ctx.guild.get_member(user_id)
    if member:
        role_mem = discord.utils.get(ctx.guild.roles, name=ROLE_MEMBER)
        role_wait = discord.utils.get(ctx.guild.roles, name=ROLE_WAITING)
        if role_wait in member.roles: await member.remove_roles(role_wait)
        if role_mem: await member.add_roles(role_mem)
        await ctx.send(f"✅ {member.display_name} を承認しました")


@bot.command()
async def reject(ctx, user_id: int):
    if ctx.author.id != current_admin_id: return
    member = ctx.guild.get_member(user_id)
    if member:
        await ctx.guild.kick(member, reason="審査拒否")
        await ctx.send(f"🚫 {member.display_name} を拒否しました")


@bot.command()
async def graduate(ctx, user_id: int):
    if ctx.author.id != current_admin_id: return
    member = ctx.guild.get_member(user_id)
    if member:
        try:
            await member.send(
                f"🌸 レベル上限({MAX_LEVEL})に達したため、サーバーを卒業となります。ご利用ありがとうございました！")
        except:
            pass
        await ctx.guild.kick(member, reason="卒業")
        if users_col: users_col.delete_one({"discord_id": user_id})
        await ctx.send(f"🎓 {member.display_name} を卒業(Kick)させました。")


@bot.command()
async def list(ctx):
    if not users_col: return await ctx.send("❌ DB未接続")
    users = users_col.find()
    msg = "**📋 メンバーリスト**\n"
    count = 0
    for u in users:
        count += 1
        name_safe = u['riot_name'].replace(" ", "%20")
        url = f"https://www.op.gg/summoners/jp/{name_safe}-{u['riot_tag']}"
        discord_user = ctx.guild.get_member(u['discord_id'])
        d_name = discord_user.display_name if discord_user else "退室済み"
        line = f"• **{d_name}**: [{u['riot_name']}#{u['riot_tag']}]({url}) (Lv.{u['level']})\n"
        if len(msg + line) > 1900:
            msg += "...(他省略)"
            break
        msg += line
    if count == 0: msg += "登録なし"
    await ctx.send(msg)


@bot.command()
async def export(ctx):
    if not is_admin_or_owner(ctx): return
    if not users_col: return await ctx.send("❌ DB未接続")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Discord Name', 'Discord ID', 'Riot Name', 'Riot Tag', 'Level', 'OP.GG Link'])
    for u in users_col.find():
        name_safe = u['riot_name'].replace(" ", "%20")
        url = f"https://www.op.gg/summoners/jp/{name_safe}-{u['riot_tag']}"
        discord_user = ctx.guild.get_member(u['discord_id'])
        d_name = discord_user.name if discord_user else "Unknown"
        writer.writerow([d_name, u['discord_id'], u['riot_name'], u['riot_tag'], u['level'], url])
    output.seek(0)
    await ctx.send("📊 メンバーリストを出力しました。", file=discord.File(output, "members.csv"))


@bot.command()
async def set_mode(ctx, mode: str):
    if not is_admin_or_owner(ctx): return
    global current_mode
    mode = mode.upper()
    if mode in THRESHOLDS:
        current_mode = mode
        await ctx.send(f"✅ モード変更: {THRESHOLDS[mode]['name']}")


@bot.group(invoke_without_command=True)
async def settings(ctx):
    if not is_admin_or_owner(ctx): return
    admin_user = await bot.fetch_user(current_admin_id) if current_admin_id else None
    admin_name = admin_user.name if admin_user else "未設定"
    target_guild = bot.get_guild(current_guild_id)
    guild_name = target_guild.name if target_guild else "未設定"
    msg = (
        f"⚙️ **Bot設定** ⚙️\n"
        f"👤 管理者: `{admin_name}`\n"
        f"🏠 サーバー: `{guild_name}`\n"
        f"📊 モード: `{THRESHOLDS[current_mode]['name']}`\n"
        f"🎓 卒業レベル: `{MAX_LEVEL}`\n"
        f"🛡️ 免除ロール: `{ROLE_ADVISOR}`"
    )
    await ctx.send(msg)


@settings.command()
async def admin(ctx, user: discord.User):
    if not is_admin_or_owner(ctx): return
    global current_admin_id
    current_admin_id = user.id
    await ctx.send(f"✅ 管理者を変更: {user.mention}")


@settings.command()
async def server(ctx):
    if not is_admin_or_owner(ctx): return
    global current_guild_id
    current_guild_id = ctx.guild.id
    await ctx.send(f"✅ 対象サーバーを変更: {ctx.guild.name}")


keep_alive()
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)