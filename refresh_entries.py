"""
🏇 確定馬番リフレッシュスクリプト

枠順確定前 (月曜朝) の出馬表は netkeiba の馬番欄が空で、scrape_shutuba が
五十音順/登録順で「仮馬番 1..N」を採番してしまう。枠順抽選 (金 11:00) 後に
確定馬番へ更新しないと、馬番と馬名がズレたまま予測・投稿される (#44)。

このスクリプトは当日の全レースを scrape_shutuba で再取得し、save_race_to_db
(確定馬番で INSERT OR REPLACE + 仮馬番の同期削除) で DB を最新化する。
予測 (predict) の直前に実行するのが肝心。

使い方:
  python refresh_entries.py --date 20260531
  python refresh_entries.py            (→ 当日分を自動判定)
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, get_db
from scraper import NetkeibaScraper

JST = timezone(timedelta(hours=9))


def now_jst():
    return datetime.now(JST)


def refresh_entries(date_str):
    """指定日の全レースの出走馬を確定馬番で再取得して DB を更新"""
    init_db()
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    with get_db() as conn:
        races = conn.execute("""
            SELECT race_id, venue, race_number
            FROM races
            WHERE race_date = ? OR race_date = ?
            ORDER BY venue, race_number
        """, (date_str, date_hyphen)).fetchall()

    if not races:
        print(f"❌ {date_str} のレースが見つかりません")
        return False

    print(f"🏇 {len(races)}レースの確定馬番を取得中...")
    scraper = NetkeibaScraper()
    updated = 0
    changed = 0

    for race in races:
        rid = race["race_id"]
        # 更新前の馬番→馬名スナップショット (変化検出用)
        with get_db() as conn:
            before = {
                r["horse_number"]: r["horse_name"]
                for r in conn.execute("""
                    SELECT re.horse_number, h.horse_name
                    FROM results re JOIN horses h ON re.horse_id = h.horse_id
                    WHERE re.race_id = ? AND re.finish_position = 0
                """, (rid,)).fetchall()
            }
        try:
            shutuba = scraper.scrape_shutuba(rid)
            if shutuba and shutuba.get("entries"):
                scraper.save_race_to_db(shutuba)
                updated += 1
                # 変化チェック
                after = {e.get("horse_number"): e.get("horse_name") for e in shutuba["entries"]}
                if before and any(before.get(hn) != after.get(hn) for hn in after):
                    changed += 1
                    print(f"  🔧 {race['venue']}{race['race_number']}R: 馬番を確定版に更新")
        except Exception as e:
            print(f"  ⚠️ {race['venue']}{race['race_number']}R 取得エラー: {e}")
        time.sleep(0.3)

    print(f"\n✅ {updated}/{len(races)}レース更新完了 (馬番変更: {changed}レース)")
    return True


def main():
    parser = argparse.ArgumentParser(description="🏇 確定馬番リフレッシュ")
    parser.add_argument("--date", help="対象日 (YYYYMMDD, デフォルト=当日)")
    args = parser.parse_args()
    date_str = args.date or now_jst().strftime("%Y%m%d")
    print(f"🔄 {date_str} の確定馬番を更新します\n")
    refresh_entries(date_str)


if __name__ == "__main__":
    main()
