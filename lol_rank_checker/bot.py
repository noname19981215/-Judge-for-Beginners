import discord
import time
import traceback
import random
from discord.ext import commands
from riotwatcher import LolWatcher, RiotWatcher, ApiError

# ==========================================
# 設定項目
# ==========================================
DISCORD_TOKEN = ''
RIOT_API_KEY = ''
ADMIN_USER_ID =   # 通知を送る管理者のDiscord User ID
GUILD_ID =   # 対象のサーバーID

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

lol_watcher = LolWatcher(RIOT_API_KEY)
riot_watcher = RiotWatcher(RIOT_API_KEY)

TIER_VALUE = {
    "IRON": 1, "BRONZE": 2, "SILVER": 3, "GOLD": 4,
    "PLATINUM": 5, "EMERALD": 6, "DIAMOND": 7,
    "MASTER": 8, "GRANDMASTER": 9, "CHALLENGER": 10
}


# ==========================================
# 補助関数: 試合からレートを推定する (デバッグ強化版)
# ==========================================
def estimate_rank_from_match(match_id, my_puuid):
    print(f"\n--- [DEBUG] 推定ロジック開始 (MatchID: {match_id}) ---")
    try:
        # 試合詳細を取得
        match_detail = lol_watcher.match.by_id(REGION_ACCOUNT, match_id)
        participants = match_detail['info']['participants']

        # 自分以外のサモナーIDリストを作成
        others_summoner_ids = []
        for p in participants:
            # Bot戦などでIDがない場合や、自分自身を除外
            if p['puuid'] != my_puuid:
                if 'summonerId' in p:
                    others_summoner_ids.append(p['summonerId'])
                else:
                    print(f"[DEBUG] 参加者 {p.get('riotIdGameName')} のsummonerIdが欠落しています")

        print(f"[DEBUG] 取得できた他プレイヤーID数: {len(others_summoner_ids)}人")

        if len(others_summoner_ids) == 0:
            print("[WARN] 他プレイヤーのIDが一つも取得できませんでした。")
            return "UNKNOWN"

        # ランダムに3人ピックアップ
        target_ids = random.sample(others_summoner_ids, min(len(others_summoner_ids), 3))

        tiers_found = []

        for s_id in target_ids:
            time.sleep(1.0)  # ★間隔を広げました(1.0秒)。429エラー防止
            try:
                # ここでエラーが起きている可能性が高い
                leagues = lol_watcher.league.by_summoner(REGION_PLATFORM, s_id)

                found_tier = "UNRANKED"
                for league in leagues:
                    if league['queueType'] == 'RANKED_SOLO_5x5':
                        found_tier = league['tier']
                        break

                print(f"[DEBUG] ID: {s_id[:8]}... -> Rank: {found_tier}")
                tiers_found.append(found_tier)

            except ApiError as err:
                print(f"[ERROR] ランク取得失敗 (HTTP {err.response.status_code}): {err}")
            except Exception as e:
                print(f"[ERROR] 予期せぬエラー: {e}")

        print(f"[DEBUG] 最終抽出ランク: {tiers_found}")

        # 集計ロジック
        if not tiers_found:
            return "UNKNOWN"

        highest_score = 0
        highest_tier = "UNRANKED"
        score_sum = 0
        valid_count = 0

        for t in tiers_found:
            val = TIER_VALUE.get(t, 0)
            if val > 0:
                score_sum += val
                valid_count += 1
                if val > highest_score:
                    highest_score = val
                    highest_tier = t

        # 判定
        if valid_count > 0:
            avg_score = score_sum / valid_count
            print(f"[DEBUG] 平均スコア: {avg_score} (Max: {highest_tier})")

            # 平均がシルバー(3)に近い、または誰か一人でもGold(4)以上ならアウトにする
            if avg_score >= 2.5 or highest_score >= 4:
                return highest_tier
            else:
                return "IRON/BRONZE"
        else:
            return "UNKNOWN"

    except Exception as e:
        print(f"[ERROR] 推定関数全体でエラー: {e}")
        return "UNKNOWN"


# ==========================================
# メイン判定ロジック
# ==========================================
async def analyze_player(riot_id_name, riot_id_tag):
    try:
        print(f"--- 審査開始: {riot_id_name}#{riot_id_tag} ---")

        # 1. Riot ID -> PUUID
        account = riot_watcher.account.by_riot_id(REGION_ACCOUNT, riot_id_name, riot_id_tag)
        puuid = account.get('puuid')
        if not puuid: return {"status": "ERROR", "reason": "PUUID取得不可", "data": locals()}

        # 2. PUUID -> Summoner ID
        summoner = lol_watcher.summoner.by_puuid(REGION_PLATFORM, puuid)
        summ_id = summoner.get('id')
        acct_level = summoner.get('summonerLevel', 0)

        # 3. ランク取得
        current_rank_tier = "UNKNOWN"
        if summ_id:
            try:
                leagues = lol_watcher.league.by_summoner(REGION_PLATFORM, summ_id)
                for league in leagues:
                    if league['queueType'] == 'RANKED_SOLO_5x5':
                        current_rank_tier = league['tier']
                        break
            except:
                pass

        print(f"[DEBUG] 本人ランク: {current_rank_tier}")

        # 判定A: 即BAN
        if current_rank_tier in ['SILVER', 'GOLD', 'PLATINUM', 'EMERALD', 'DIAMOND', 'MASTER', 'GRANDMASTER',
                                 'CHALLENGER']:
            return {"status": "BAN", "reason": f"現在ランクが高すぎます: {current_rank_tier}", "data": locals()}

        # 4. 試合履歴取得
        matches = lol_watcher.match.matchlist_by_puuid(REGION_ACCOUNT, puuid, count=20)
        match_count = len(matches)

        if match_count == 0:
            return {"status": "REVIEW", "reason": "試合データなし", "data": locals()}

        # ★判定B: 推定ランクチェック (本人ランク不明の場合)
        estimated_tier = "UNKNOWN"
        if current_rank_tier == "UNKNOWN" and match_count > 0:
            # 最新の試合IDを使用
            latest_match_id = matches[0]
            estimated_tier = estimate_rank_from_match(latest_match_id, puuid)

            print(f"[DEBUG] 推定結果: {estimated_tier}")

            if estimated_tier in ['SILVER', 'GOLD', 'PLATINUM', 'EMERALD', 'DIAMOND', 'MASTER', 'GRANDMASTER',
                                  'CHALLENGER']:
                return {"status": "BAN", "reason": f"推定ランクが高すぎます(周囲: {estimated_tier})", "data": locals()}

        # 5. 戦績集計
        wins = 0
        total_kills = 0
        total_deaths = 0
        total_assists = 0
        recent_10_wins = 0

        print("[DEBUG] 戦績集計開始...")
        for idx, match_id in enumerate(matches):
            time.sleep(0.5)
            try:
                match_detail = lol_watcher.match.by_id(REGION_ACCOUNT, match_id)
            except:
                continue

            if 'info' in match_detail and 'participants' in match_detail['info']:
                for participant in match_detail['info']['participants']:
                    if participant['puuid'] == puuid:
                        if participant['win']:
                            wins += 1
                            if idx < 10: recent_10_wins += 1

                        total_kills += participant['kills']
                        total_deaths += participant['deaths']
                        total_assists += participant['assists']
                        break

        win_rate = (wins / match_count) * 100
        avg_deaths = total_deaths if total_deaths > 0 else 1
        kda = (total_kills + total_assists) / avg_deaths

        data_snapshot = {
            "riot_id": f"{riot_id_name}#{riot_id_tag}",
            "rank": f"{current_rank_tier} (推定: {estimated_tier})",
            "level": acct_level,
            "win_rate": round(win_rate, 1),
            "kda": round(kda, 2),
            "matches": match_count,
            "recent_10_wins": recent_10_wins
        }

        # 判定ロジック
        if current_rank_tier == "UNKNOWN":
            reasons = []
            reasons.append(f"ランク情報取得不可 (周囲推定: {estimated_tier})")
            if win_rate > 60: reasons.append("勝率高め")
            if kda > 3.5: reasons.append("KDA高め")
            return {"status": "REVIEW", "reason": ", ".join(reasons), "data": data_snapshot}

        if win_rate <= 60 and kda <= 3.5:
            return {"status": "APPROVE", "reason": "基準内", "data": data_snapshot}

        reasons = []
        if 61 <= win_rate <= 69: reasons.append("勝率61-69%")
        if win_rate >= 70: reasons.append("勝率70%以上")
        if 3.6 <= kda <= 4.5: reasons.append("KDA 3.6-4.5")
        if kda > 4.5: reasons.append("高KDA")
        if recent_10_wins >= 7: reasons.append("直近10戦で7勝以上")
        if acct_level < 40 and win_rate > 60: reasons.append("低Lv(Lv<40)かつ高勝率")

        if not reasons: reasons.append("自動許可基準外")

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
        await ctx.send("❌ 形式エラー: `GameName#Tag` で入力してください。")
        return

    name, tag = riot_id_str.split('#', 1)
    await ctx.send(f"🔍 `{name}#{tag}` を審査中... (時間がかかります)")

    result = await analyze_player(name, tag)
    status = result['status']

    if status == "ERROR":
        await ctx.send(f"❌ エラー: {result['reason']}")
        return

    member = ctx.author
    guild = ctx.guild
    role_member = discord.utils.get(guild.roles, name=ROLE_MEMBER)
    role_waiting = discord.utils.get(guild.roles, name=ROLE_WAITING)

    if not role_member or not role_waiting:
        await ctx.send("⚠️ ロール設定エラー")
        return

    if status == "BAN":
        await ctx.send(f"🚫 参加要件を満たしていません (理由: {result['reason']})")
        try:
            await guild.kick(member, reason=f"Bot自動判定: {result['reason']}")
        except:
            await ctx.send("⚠️ Kick権限がありません")

    elif status == "APPROVE":
        await member.add_roles(role_member)
        await ctx.send(f"✅ 審査通過！ようこそ `{result['data']['riot_id']}` さん")

    elif status == "REVIEW":
        await member.add_roles(role_waiting)
        await ctx.send("⚠️ 詳細審査が必要です。管理者に通知を送りました。")
        try:
            admin_user = await bot.fetch_user(ADMIN_USER_ID)
            if admin_user:
                d = result['data']
                msg = (
                    f"**【審査依頼】**\n"
                    f"対象: {member.mention}\n"
                    f"ID: `{d['riot_id']}`\n"
                    f"ランク: **{d['rank']}**\n"
                    f"勝率: {d['win_rate']}%\n"
                    f"KDA: {d['kda']}\n"
                    f"理由: {result['reason']}\n"
                    f"操作:\n`/approve {member.id}`\n`/reject {member.id}`"
                )
                await admin_user.send(msg)
        except:
            pass


@bot.command()
async def approve(ctx, user_id: int):
    if ctx.author.id != ADMIN_USER_ID: return
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user_id)
    if member:
        role_member = discord.utils.get(guild.roles, name=ROLE_MEMBER)
        role_waiting = discord.utils.get(guild.roles, name=ROLE_WAITING)
        if role_waiting in member.roles: await member.remove_roles(role_waiting)
        await member.add_roles(role_member)
        await ctx.send(f"✅ {member.display_name} を承認しました")


@bot.command()
async def reject(ctx, user_id: int):
    if ctx.author.id != ADMIN_USER_ID: return
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(user_id)
    if member:
        await guild.kick(member, reason="審査拒否")
        await ctx.send(f"🚫 {member.display_name} を拒否しました")


bot.run(DISCORD_TOKEN)