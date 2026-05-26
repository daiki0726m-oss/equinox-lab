#!/usr/bin/env python3
"""記事用ファクトを DB から一括取得 (記事内の全数値の source of truth)。

Usage:
    python3 .claude/skills/race-article-writer/scripts/pull_race_facts.py <race_name> [--years 6] [--upcoming-race-id RACE_ID]

Output: JSON to stdout
{
  "race_name": "目黒記念",
  "winners": [{year, horse_name, popularity, odds, sire, damsire, prev_race, prev_finish, impost}, ...],
  "course": {venue, surface, distance},
  "course_stats": {
    "sire_top": [{sire, runs, top3, pct}, ...],
    "post_position_bias": [{post, runs, top3, pct}, ...],
    "pace_decisive": {fastest_3f_n, wins, top3},
    "jockey_top": [{name, runs, top3, pct}, ...]
  },
  "entries": [{horse_number, horse_name, sire, jockey_name, impost, provisional_number}, ...],
  "winner_pop_summary": {1: 2, 2: 1, 4: 1, ...},  # 人気帯別勝数
  "winner_impost_band": [54.0, 57.5, ...],
  "gaps": {"missing_race_years": [2021], "missing_year_data": []}
}

これを source of truth として記事を書く。ハードコードの数字は禁止。
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime


def find_db():
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "keiba.db"),
        "keiba.db",
    ]
    for p in candidates:
        if os.path.exists(p):
            return os.path.abspath(p)
    raise FileNotFoundError("keiba.db not found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("race_name", help="例: 目黒記念")
    ap.add_argument("--years", type=int, default=6)
    ap.add_argument("--upcoming-race-id", default=None,
                    help="対象 race_id を直接指定 (entries 取得用)。未指定なら今年の同名レース")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    db = args.db or find_db()
    conn = sqlite3.connect(db)
    cur = conn.cursor()

    current_year = datetime.now().year

    # === 1. 過去勝ち馬 ===
    cur.execute(
        """
        SELECT substr(r.race_date,1,4) yr, r.race_id, h.horse_name, res.popularity, res.odds,
               h.sire, h.damsire, res.impost, res.horse_id,
               r.venue, r.surface, r.distance
        FROM races r
        JOIN results res ON r.race_id = res.race_id AND res.finish_position = 1
        JOIN horses h ON res.horse_id = h.horse_id
        WHERE r.race_name LIKE ?
          AND r.race_date >= ? AND r.race_date < ?
        ORDER BY r.race_date DESC
        """,
        (f"%{args.race_name}%",
         f"{current_year - args.years}-01-01",
         f"{current_year + 1}-01-01"),
    )
    winners_raw = cur.fetchall()

    winners = []
    venue = surface = distance = None
    for yr, rid, name, pop, odds, sire, damsire, impost, hid, v, s, d in winners_raw:
        venue, surface, distance = v, s, d
        # 前走
        cur.execute(
            """
            SELECT r2.race_name, res2.finish_position
            FROM results res2 JOIN races r2 ON res2.race_id = r2.race_id
            WHERE res2.horse_id = ? AND r2.race_date < (SELECT race_date FROM races WHERE race_id = ?)
              AND res2.finish_position > 0
            ORDER BY r2.race_date DESC LIMIT 1
            """,
            (hid, rid),
        )
        prev = cur.fetchone()
        winners.append({
            "year": int(yr),
            "race_id": rid,
            "horse_name": name,
            "popularity": pop,
            "odds": odds,
            "sire": sire,
            "damsire": damsire,
            "impost": impost,
            "prev_race": prev[0] if prev else None,
            "prev_finish": prev[1] if prev else None,
        })

    # === 2. 出走予定馬 ===
    upcoming_race_id = args.upcoming_race_id
    if not upcoming_race_id and venue and distance:
        # 今年の同名レースを推測 (race_date >= 今日)
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute(
            "SELECT race_id FROM races WHERE race_name LIKE ? AND race_date >= ? LIMIT 1",
            (f"%{args.race_name}%", today),
        )
        r = cur.fetchone()
        upcoming_race_id = r[0] if r else None

    entries = []
    if upcoming_race_id:
        cur.execute(
            """
            SELECT res.horse_number, h.horse_name, h.sire, h.damsire,
                   j.jockey_name, res.impost, res.popularity
            FROM results res
            JOIN horses h ON res.horse_id = h.horse_id
            LEFT JOIN jockeys j ON res.jockey_id = j.jockey_id
            WHERE res.race_id = ?
            ORDER BY res.horse_number
            """,
            (upcoming_race_id,),
        )
        for hn, name, sire, damsire, jockey, impost, pop in cur.fetchall():
            entries.append({
                "horse_number": hn,
                "horse_name": name,
                "sire": sire,
                "damsire": damsire,
                "jockey_name": jockey,
                "impost": impost,
                "popularity": pop or 0,
                "provisional_number": (hn == 0 or hn is None),
            })

    # === 3. コース統計 ===
    course_stats = {}
    if venue and surface and distance:
        from_date = f"{current_year - args.years}-01-01"

        # 種牡馬 TOP (run >= 3)
        cur.execute(
            """
            SELECT h.sire, COUNT(*) runs,
                   SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) top3
            FROM races r JOIN results res ON r.race_id = res.race_id
            JOIN horses h ON res.horse_id = h.horse_id
            WHERE r.surface=? AND r.distance=? AND r.venue=?
              AND r.race_date >= ? AND res.finish_position > 0 AND h.sire IS NOT NULL
            GROUP BY h.sire HAVING runs >= 3
            ORDER BY 1.0*top3/runs DESC, runs DESC LIMIT 8
            """,
            (surface, distance, venue, from_date),
        )
        course_stats["sire_top"] = [
            {"sire": r[0], "runs": r[1], "top3": r[2], "pct": round(100*r[2]/r[1], 1)}
            for r in cur.fetchall()
        ]

        # 枠順
        cur.execute(
            """
            SELECT res.post_position, COUNT(*) runs,
                   SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) top3
            FROM races r JOIN results res ON r.race_id = res.race_id
            WHERE r.surface=? AND r.distance=? AND r.venue=?
              AND r.race_date >= ? AND res.finish_position > 0
              AND res.post_position BETWEEN 1 AND 8
            GROUP BY res.post_position ORDER BY res.post_position
            """,
            (surface, distance, venue, from_date),
        )
        course_stats["post_position_bias"] = [
            {"post": r[0], "runs": r[1], "top3": r[2], "pct": round(100*r[2]/r[1], 1)}
            for r in cur.fetchall()
        ]

        # 末脚最速馬
        cur.execute(
            """
            WITH fastest AS (
              SELECT r.race_id, MIN(res.last_3f) min_3f
              FROM races r JOIN results res ON r.race_id = res.race_id
              WHERE r.surface=? AND r.distance=? AND r.venue=?
                AND r.race_date >= ? AND res.last_3f > 0
              GROUP BY r.race_id
            )
            SELECT COUNT(*) n,
                   SUM(CASE WHEN res.finish_position = 1 THEN 1 ELSE 0 END) wins,
                   SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) top3
            FROM fastest f JOIN results res ON f.race_id = res.race_id
            WHERE res.last_3f = f.min_3f AND res.finish_position > 0
            """,
            (surface, distance, venue, from_date),
        )
        n, wins, top3 = cur.fetchone() or (0, 0, 0)
        course_stats["pace_decisive"] = {
            "fastest_3f_n": n or 0,
            "wins": wins or 0,
            "top3": top3 or 0,
            "win_pct": round(100*(wins or 0)/(n or 1), 1),
            "top3_pct": round(100*(top3 or 0)/(n or 1), 1),
        }

        # 騎手 TOP (3年)
        from_date_j = f"{current_year - 3}-01-01"
        cur.execute(
            """
            SELECT j.jockey_name, COUNT(*) runs,
                   SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) top3
            FROM races r JOIN results res ON r.race_id = res.race_id
            JOIN jockeys j ON res.jockey_id = j.jockey_id
            WHERE r.surface=? AND r.distance=? AND r.venue=?
              AND r.race_date >= ? AND res.finish_position > 0 AND j.jockey_name IS NOT NULL
            GROUP BY j.jockey_name HAVING runs >= 3
            ORDER BY 1.0*top3/runs DESC LIMIT 8
            """,
            (surface, distance, venue, from_date_j),
        )
        course_stats["jockey_top"] = [
            {"name": r[0], "runs": r[1], "top3": r[2], "pct": round(100*r[2]/r[1], 1)}
            for r in cur.fetchall()
        ]

    # === 4. 集計 (記事の自動検算用) ===
    winner_pop_summary = dict(Counter(w["popularity"] for w in winners if w["popularity"]))
    winner_impost_band = [w["impost"] for w in winners if w["impost"]]

    # === 5. ギャップ検出 ===
    race_years = {w["year"] for w in winners}
    expected_years = set(range(current_year - args.years, current_year))
    missing_race_years = sorted(expected_years - race_years)

    missing_year_data = []
    for y in range(current_year - args.years, current_year):
        cur.execute("SELECT COUNT(*) FROM races WHERE substr(race_date,1,4)=?", (str(y),))
        if cur.fetchone()[0] < 100:
            missing_year_data.append(y)

    result = {
        "race_name": args.race_name,
        "course": {"venue": venue, "surface": surface, "distance": distance},
        "winners": winners,
        "winner_pop_summary": winner_pop_summary,
        "winner_impost_band": winner_impost_band,
        "course_stats": course_stats,
        "upcoming_race_id": upcoming_race_id,
        "entries": entries,
        "gaps": {
            "missing_race_years": missing_race_years,
            "missing_year_data": missing_year_data,
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
