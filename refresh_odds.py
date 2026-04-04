"""
📡 最新オッズ取得 & 馬場状態更新 & 予測キャッシュ更新スクリプト
10時のロック前に最新のリアルオッズ＋馬場状態を取得して予測に反映する

使い方:
  python refresh_odds.py --date 20260321
  python refresh_odds.py --no-track   (馬場更新をスキップ)
  python refresh_odds.py  (→ 当日分を自動判定)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

import re
import requests
from bs4 import BeautifulSoup
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


def fetch_track_condition(race_id):
    """netkeibaのレースページから馬場状態・天候を取得"""
    url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://race.netkeiba.com/"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = "EUC-JP"
        soup = BeautifulSoup(resp.text, "lxml")

        race_data01 = soup.find("div", class_="RaceData01") or soup.find("dl", class_="RaceData01")
        if not race_data01:
            return None

        text = race_data01.get_text()
        result = {}

        # 天候
        weather_match = re.search(r"天候:(\S+)", text)
        if weather_match:
            result["weather"] = weather_match.group(1)

        # 馬場状態
        condition_match = re.search(r"馬場:(\S+)", text)
        if condition_match:
            result["track_condition"] = condition_match.group(1)
        else:
            for cond in ["不良", "稍重", "重", "良"]:
                if cond in text:
                    result["track_condition"] = cond
                    break

        return result if result else None
    except Exception as e:
        print(f"  ⚠️ 馬場状態取得エラー: {e}")
        return None


def refresh_odds(date_str, update_track=True):
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

    # 馬場状態の更新（開催場ごとに1回だけ取得）
    if update_track:
        venues_updated = set()
        for race in races:
            venue = race['venue']
            if venue in venues_updated:
                continue
            rid = race['race_id']
            track_info = fetch_track_condition(rid)
            if track_info:
                tc = track_info.get('track_condition', '')
                wt = track_info.get('weather', '')
                if tc:
                    with get_db() as conn:
                        # 同じ開催場の全レースを更新
                        conn.execute("""
                            UPDATE races SET track_condition = ?, weather = ?
                            WHERE (race_date = ? OR race_date = ?) AND venue = ?
                        """, (tc, wt, date_str,
                              f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
                              venue))
                    print(f"  🏇 {venue}: 馬場={tc} 天候={wt}")
                    venues_updated.add(venue)
            time.sleep(0.5)
        if venues_updated:
            print(f"  ✅ {len(venues_updated)}場の馬場状態を更新")
        else:
            print(f"  ⚠️ 馬場状態はまだ未発表")
        print()

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
    parser = argparse.ArgumentParser(description="📡 最新オッズ＋馬場状態取得")
    parser.add_argument("--date", help="対象日 (YYYYMMDD, デフォルト=当日)")
    parser.add_argument("--no-track", action="store_true", help="馬場状態の更新をスキップ")
    args = parser.parse_args()

    if args.date:
        date_str = args.date
    else:
        date_str = now_jst().strftime("%Y%m%d")

    print(f"🔄 {date_str} のオッズ＋馬場状態を更新します\n")
    refresh_odds(date_str, update_track=not args.no_track)


if __name__ == "__main__":
    main()
