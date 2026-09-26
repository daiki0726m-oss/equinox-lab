#!/usr/bin/env python3
"""開催日の「やり残し」を翌朝に検査する (#161)。

2026-09-21(月・祝) は、予想を投稿したのに
  - 阪神 10R/11R(神戸新聞杯G2)/12R の着順が収集されず
  - 結果報告が一度も配信されなかった
が、どちらも通知されず、監査で見つかるまで5日間誰も知らなかった。
各層 (runner / watchdog / セーフティネット) は「自分の担当時刻に何をしたか」しか見ておらず、
「その日が最後まで終わったか」を見る者がいなかった。

ここでは直近の開催日 (race_calendar.json、今日より前) ごとに:
  1. 出走馬がいるのに1着が記録されていないレース (= 着順の取りこぼし)
  2. 予想を投稿した (posted_marks がある) のに results の lock が無い (= 結果報告の未配信)
を調べ、見つかれば exit 1 (feature_parity.yml が Issue を立てる)。

着順の取りこぼしは collect_results の夜間スイープ (#161) が直近10日分を自動で拾い直すので、
ここに出るのは「スイープでも拾えなかった」もの。結果報告の遅れ配信は人が判断する。
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
JST = dt.timezone(dt.timedelta(hours=9))


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default=os.environ.get("COMPLETENESS_REPORT", "/tmp/raceday_gaps.md"))
    args = ap.parse_args()

    today = dt.datetime.now(JST).date()
    cal = _load(os.path.join(DATA, "race_calendar.json")) or {}
    days = sorted(d for d in (cal.get("dates") or {})
                  if (today - dt.timedelta(days=args.days)).isoformat() <= d < today.isoformat())
    if not days:
        print("ℹ️ 検査対象の開催日なし")
        return 0

    db = os.path.join(ROOT, "keiba.db")
    conn = sqlite3.connect(db) if os.path.exists(db) else None
    gaps = []
    for d in days:
        d8 = d.replace("-", "")
        if conn is not None:
            rows = conn.execute("""
                SELECT g.venue, g.race_number, g.race_name FROM races g
                WHERE g.race_date = ?
                  AND CAST(substr(g.race_id, 5, 2) AS INT) BETWEEN 1 AND 10
                  AND EXISTS (SELECT 1 FROM results r WHERE r.race_id = g.race_id)
                  AND NOT EXISTS (SELECT 1 FROM results w
                                  WHERE w.race_id = g.race_id AND w.finish_position = 1)
                ORDER BY g.venue, g.race_number
            """, (d,)).fetchall()
            if rows:
                gaps.append(f"{d}: 着順が未収集 {len(rows)}R — "
                            + "、".join(f"{v}{n}R {nm}" for v, n, nm in rows))
        marks = _load(os.path.join(DATA, f"posted_marks_{d8}.json"))
        slots = (_load(os.path.join(DATA, f"posted_slots_{d8}.json")) or {}).get("slots") or {}
        if marks and "results" not in slots:
            gaps.append(f"{d}: 予想は投稿済み (posted_marks あり) だが結果報告が未配信 "
                        f"(posted_slots に results が無い)")

    print(f"🔎 開催日の完了検査: {', '.join(days)}")
    if not gaps:
        print("✅ やり残しなし")
        return 0
    for g in gaps:
        print(f"❌ {g}")
    try:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(f"- {g}" for g in gaps) + "\n")
    except OSError:
        pass
    return 1


if __name__ == "__main__":
    sys.exit(main())
