"""
📡 最新オッズ取得 & 予測キャッシュ更新スクリプト
10時のロック前に最新のリアルオッズを取得して予測に反映する

使い方:
  python refresh_odds.py --date 20260321
  python refresh_odds.py  (→ 当日分を自動判定)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

import requests
from database import init_db, get_db


JST = timezone(timedelta(hours=9))


def now_jst():
    return datetime.now(JST)


def fetch_odds_from_api(race_id):
    """netkeiba APIから単勝オッズを取得"""
    url = f"https://race.netkeiba.com/api/api_get_jra_odds.html?race_id={race_id}&type=1&action=update"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://race.netkeiba.com/"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()

        if data.get("status") and data.get("data", {}).get("odds"):
            odds_raw = data["data"]["odds"].get("1", {})
            result = {}
            for horse_num_str, vals in odds_raw.items():
                if isinstance(vals, list) and len(vals) >= 3:
                    odds_val = float(vals[0]) if vals[0] and vals[0] != '---.-' else 0
                    pop_val = int(vals[2]) if vals[2] else 0
                    result[int(horse_num_str)] = {
                        "odds_win": odds_val,
                        "popularity": pop_val,
                    }
            return result
    except Exception as e:
        print(f"  ⚠️ API取得エラー: {e}")
    return {}


def refresh_odds(date_str):
    """指定日の全レースのオッズを取得してDB/キャッシュを更新"""
    init_db()

    # 日付フォーマット変換
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    with get_db() as conn:
        races = conn.execute("""
            SELECT race_id, race_name, venue, race_number
            FROM races
            WHERE race_date = ? OR race_date = ?
            ORDER BY venue, race_number
        """, (date_str, date_hyphen)).fetchall()

    if not races:
        print(f"❌ {date_str} のレースが見つかりません")
        return False

    print(f"📡 {len(races)}レースの最新オッズを取得中...")

    updated = 0
    for race in races:
        rid = race['race_id']
        odds = fetch_odds_from_api(rid)

        if not odds:
            continue

        with get_db() as conn:
            # DBのresultsテーブルを更新
            for horse_num, o in odds.items():
                conn.execute("""
                    UPDATE results SET odds = ?, popularity = ?
                    WHERE race_id = ? AND horse_number = ?
                """, (o["odds_win"], o["popularity"], rid, horse_num))

            # predictions_cache を更新
            cache = conn.execute(
                "SELECT predictions_json FROM predictions_cache WHERE race_id = ?",
                (rid,)
            ).fetchone()

            if cache:
                preds = json.loads(cache['predictions_json'])
                for p in preds:
                    hn = p['horse_number']
                    if hn in odds:
                        p['odds_win'] = odds[hn]["odds_win"]
                        p['popularity'] = odds[hn]["popularity"]

                conn.execute("""
                    UPDATE predictions_cache SET predictions_json = ?
                    WHERE race_id = ?
                """, (json.dumps(preds, ensure_ascii=False), rid))

        updated += 1
        rn = race['race_number']
        if rn in (9, 10, 11):
            sample = list(odds.items())[:2]
            s_txt = ", ".join([f"{k}番={v['odds_win']}倍({v['popularity']}人気)" for k, v in sample])
            print(f"  ✅ {race['venue']}R{rn} {race['race_name']}: {s_txt}")

        time.sleep(0.3)

    print(f"\n✅ {updated}/{len(races)}レースのオッズを更新完了")
    return True


def main():
    parser = argparse.ArgumentParser(description="📡 最新オッズ取得")
    parser.add_argument("--date", help="対象日 (YYYYMMDD, デフォルト=当日)")
    args = parser.parse_args()

    if args.date:
        date_str = args.date
    else:
        date_str = now_jst().strftime("%Y%m%d")

    print(f"🔄 {date_str} のオッズを更新します\n")
    refresh_odds(date_str)


if __name__ == "__main__":
    main()
