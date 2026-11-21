#!/usr/bin/env python3
"""開催日カレンダーを текст サイドカーに書き出す (#158)

なぜ要るか
----------
このシステムの「レース日」判定は全て曜日ベース (cron の `6,0` と `DOW -ge 6`) で、
**祝日の3日間開催 (月曜) が構造的に見えない**。2026-09-21 (敬老の日) は
阪神10R・神戸新聞杯(G2) が組まれていたが、レース枠は DB に登録された一方で
出走馬の取得も予測も走らず、ダッシュボードは発走1時間半前まで404だった。
3日間開催は年に数回ある (成人の日/GW/敬老の日/スポーツの日/文化の日 等)。

曜日を足して回る対症療法はやめる。#2 の馬番ガードと同じ考え方で
**「DB にレースが入っているか」をデータ駆動の判定にする**。
ただし watchdog は keiba.db を持たない (正本は .gz、展開もしない #150) ので、
DB を読めるワークフロー側でこのカレンダーを書き出し、
watchdog は git 上のテキストを読むだけにする。

出力: docs/data/race_calendar.json
  {"generated_at": ..., "dates": {"2026-09-21": {"races": 10, "venues": ["阪神"]}, ...}}
  対象は「今日を含む前後14日」。
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "data", "race_calendar.json")


def main():
    db = os.path.join(ROOT, "keiba.db")
    if not os.path.exists(db):
        print("⚠️ keiba.db が無いのでカレンダーを更新しません (既存値を維持)")
        return 1
    today = datetime.now(JST).date()
    lo, hi = today - timedelta(days=14), today + timedelta(days=14)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    dates = {}
    for r in conn.execute(
        "SELECT race_date, COUNT(*) n, GROUP_CONCAT(DISTINCT venue) v FROM races "
        "WHERE race_date BETWEEN ? AND ? AND race_date != '' "
        # #161: JRA の場コード (01-10) だけ。地方競馬の残骸行 (venue 空) が1行でもあると、
        # その日が「開催日」になり非開催日に予測・投稿を dispatch しうる。
        "AND CAST(substr(race_id, 5, 2) AS INT) BETWEEN 1 AND 10 AND venue != '' "
        "GROUP BY race_date",
            (lo.isoformat(), hi.isoformat())):
        dates[r["race_date"]] = {
            "races": r["n"],
            "venues": sorted((r["v"] or "").split(",")) if r["v"] else [],
        }
    conn.close()

    payload = {"generated_at": datetime.now(JST).isoformat(),
               "window": [lo.isoformat(), hi.isoformat()], "dates": dates}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    t = dates.get(today.isoformat())
    print(f"📅 開催日カレンダー更新: {len(dates)}日分 "
          f"({lo} 〜 {hi})")
    print(f"   本日 {today} は " +
          (f"開催あり ({t['races']}R / {'・'.join(t['venues'])})" if t else "開催なし"))
    # 曜日ベースの想定と食い違う日を目立たせる (3日間開催の検知)
    extra = [d for d in sorted(dates)
             if datetime.fromisoformat(d).weekday() < 5]
    if extra:
        print(f"   ⚠️ 平日開催 (曜日ベースの判定から漏れる日): {', '.join(extra)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
