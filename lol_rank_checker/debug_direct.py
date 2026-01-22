import requests
import json

# ============================
# ここだけ書き換えてください
# ============================
API_KEY = "RGAPI-87c7b47c-1c9f-4e0a-a544-c0f585908c9c"
GAME_NAME = "sikami0siki"
TAG_LINE = "JP1"


# ============================

def debug_direct_access():
    print(f"--- 診断開始: {GAME_NAME}#{TAG_LINE} ---")

    # 1. PUUIDを取得 (Account-V1)
    url_account = f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{GAME_NAME}/{TAG_LINE}"
    headers = {"X-Riot-Token": API_KEY}

    print(f"\n[1] Account API へのリクエスト: {url_account}")
    resp_acct = requests.get(url_account, headers=headers)

    if resp_acct.status_code != 200:
        print(f"❌ Account API エラー: {resp_acct.status_code}")
        print(resp_acct.text)
        return

    data_acct = resp_acct.json()
    puuid = data_acct.get("puuid")
    print(f"✅ PUUID取得成功: {puuid}")

    # 2. Summoner情報を取得 (Summoner-V4) - ここが問題の箇所
    # ライブラリを通さず、直接URLを叩いて「生のデータ」を見ます
    url_summoner = f"https://jp1.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"

    print(f"\n[2] Summoner API へのリクエスト: {url_summoner}")
    resp_summ = requests.get(url_summoner, headers=headers)

    if resp_summ.status_code != 200:
        print(f"❌ Summoner API エラー: {resp_summ.status_code}")
        print(resp_summ.text)
        return

    # 生のJSONデータを表示
    raw_data = resp_summ.json()
    print("\n⬇️⬇️⬇️ Riotから返ってきた生のデータ(中身を確認してください) ⬇️⬇️⬇️")
    print(json.dumps(raw_data, indent=4, ensure_ascii=False))
    print("⬆️⬆️⬆️⬆️⬆️⬆️")

    # IDチェック
    if "id" in raw_data:
        print(f"\n✅ 'id' は存在します！ ID: {raw_data['id']}")
        print("結論: ライブラリ(riotwatcher)側のバグの可能性が高いです。")
    else:
        print("\n❌ 生データにも 'id' がありません。")
        print("結論: Riot API側、またはアカウント自体の異常です。")


if __name__ == "__main__":
    debug_direct_access()