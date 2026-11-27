#!/usr/bin/env python3
"""#181: 追い切り評価が1件も無いレースだけ、追い切りページを取り直して保存する。

追い切りは開催日の朝に fetch_weekend_races が取るが、2026-11-21〜23 は保存・push の直後に
古い DB を持った race_day_runner の push で全部消えた (#70 型)。既存の取り直し
(backfill_horse_meta.py workouts) は「既にある行の空欄を埋める」だけで、行ごと消えた分は
戻せない。collect_results の自己修復 (欠けた開催日の取り直し) から日付ごとに呼ぶ。

取得するのは「追い切りの行が1件も無いレース」だけなので、揃っている日は netkeiba に
1回も問い合わせない (過大な取得で通信を止められないように)。

usage:
  python3 scripts/refetch_workouts.py --date 20261121
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def target_races(conn, iso_date):
    """その日のレースのうち、追い切りの行が1件も無いもの。"""
    return [r[0] for r in conn.execute("""
        SELECT ra.race_id FROM races ra
        WHERE ra.race_date = ?
          AND CAST(substr(ra.race_id, 5, 2) AS INT) BETWEEN 1 AND 10   -- JRA のみ
          AND EXISTS (SELECT 1 FROM results r WHERE r.race_id = ra.race_id)
          AND NOT EXISTS (SELECT 1 FROM workouts w WHERE w.race_id = ra.race_id)
        ORDER BY ra.race_id""", (iso_date,))]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", required=True, help="YYYYMMDD または YYYY-MM-DD")
    ap.add_argument("--db", default="keiba.db")
    args = ap.parse_args()
    d = args.date.replace("-", "")
    iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    import database
    database.DB_PATH = args.db           # save_workouts_to_db (get_db) もこの DB に書く
    conn = sqlite3.connect(args.db)
    rids = target_races(conn, iso)
    conn.close()
    if not rids:
        print(f"🏃 {iso}: 追い切りが欠けたレースなし")
        return 0

    from scraper import NetkeibaScraper   # _get が取得間隔を入れる
    s = NetkeibaScraper()
    saved = empty = streak = 0
    for rid in rids:
        try:
            ws = s.scrape_workouts(rid)
        except Exception as e:
            print(f"  ⚠️ {rid}: {e}")
            ws = []
        if not ws:
            empty += 1
            streak += 1
            # ページの解析が壊れた・取得を絞られた時に、毎晩の自己修復で同じ日を
            # 空振りし続けないよう、5レース続けて取れなければその日は打ち切る (#181 レビュー)
            if streak >= 5 and saved == 0:
                print(f"::warning::{iso}: 追い切りが5レース続けて取れないので打ち切り "
                      f"(ページの構造変更か取得制限の可能性)")
                break
            continue
        streak = 0
        saved += s.save_workouts_to_db(ws)
    print(f"🏃 {iso}: 追い切りを取り直し {len(rids)} レース → 保存 {saved} 件"
          f"{f' / 取得できなかったレース {empty}' if empty else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
