#!/usr/bin/env python3
"""DB coverage audit + missing year detection."""
import argparse
import json
import os
import sqlite3
from datetime import datetime


def find_db():
    for p in [
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "keiba.db"),
        "keiba.db",
    ]:
        if os.path.exists(p):
            return os.path.abspath(p)
    raise FileNotFoundError("keiba.db not found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=6)
    ap.add_argument("--races-check", nargs="*", default=["日本ダービー", "目黒記念", "オークス"])
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db or find_db())
    cur = conn.cursor()
    current_year = datetime.now().year

    # 年別 races
    year_coverage = {}
    for y in range(current_year - args.years, current_year + 1):
        cur.execute("SELECT COUNT(*) FROM races WHERE substr(race_date,1,4)=?", (str(y),))
        year_coverage[y] = cur.fetchone()[0]

    # 年別 predictions_cache
    pred_coverage = {}
    for y in range(current_year - args.years, current_year + 1):
        cur.execute("""
            SELECT COUNT(*) FROM predictions_cache pc
            JOIN races r ON pc.race_id = r.race_id
            WHERE substr(r.race_date,1,4) = ?
        """, (str(y),))
        pred_coverage[y] = cur.fetchone()[0]

    # 欠落年判定 (年間 100R 未満)
    missing_years = [y for y, n in year_coverage.items() if n < 100 and y < current_year]

    # レース別ギャップ
    race_specific = {}
    for race_name in args.races_check:
        cur.execute("""
            SELECT substr(race_date,1,4) yr FROM races
            WHERE race_name LIKE ? AND race_date < ? AND race_date >= ?
            ORDER BY race_date DESC
        """, (f"%{race_name}%", f"{current_year}-12-31", f"{current_year - args.years}-01-01"))
        years_have = {int(r[0]) for r in cur.fetchall()}
        expected = set(range(current_year - args.years, current_year))
        gaps = sorted(expected - years_have)
        if gaps:
            race_specific[race_name] = gaps

    # 推奨アクション
    actions_set = set(missing_years)
    for gaps in race_specific.values():
        actions_set.update(g for g in gaps if g >= 2020)
    actions = [f"gh workflow run seed_historical.yml -f year={y}" for y in sorted(actions_set)]

    result = {
        "year_coverage": year_coverage,
        "predictions_cache_coverage": pred_coverage,
        "missing_years": sorted(missing_years),
        "race_specific_gaps": race_specific,
        "recommended_actions": actions,
        "data_complete": len(actions) == 0,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
