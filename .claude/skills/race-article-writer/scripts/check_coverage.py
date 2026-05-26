#!/usr/bin/env python3
"""DB年別カバレッジ確認 + 欠落あれば backfill 推奨を出力。

Usage:
    python3 .claude/skills/race-article-writer/scripts/check_coverage.py <race_name> [--years 6]

Output (stdout, JSON):
    {
      "coverage": {2020: 3456, 2021: 278, ...},
      "race_editions": [{"year": 2025, "race_id": "...", "horse_name": "..."}, ...],
      "missing_years": [2021],
      "missing_race_years": [2021],
      "recommended_actions": ["gh workflow run seed_historical.yml -f year=2021"]
    }

非ゼロ exit code は致命的な DB 接続失敗のみ。欠落自体は exit 0 で情報として返す
(autonomy のため: 呼び出し側で「次にどうするか」を判断する)。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime


def find_db():
    """keiba.db を探す (worktree 対応)"""
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "keiba.db"),
        "keiba.db",
    ]
    for p in candidates:
        if os.path.exists(p):
            return os.path.abspath(p)
    raise FileNotFoundError("keiba.db not found in any candidate path")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("race_name", help="対象レース名 (例: 目黒記念)")
    ap.add_argument("--years", type=int, default=6, help="過去何年分必要か (default 6)")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    db = args.db or find_db()
    conn = sqlite3.connect(db)
    cur = conn.cursor()

    current_year = datetime.now().year

    # 1. 年別 race count
    coverage = {}
    for y in range(current_year - args.years, current_year + 1):
        cur.execute("SELECT COUNT(*) FROM races WHERE substr(race_date,1,4) = ?", (str(y),))
        coverage[y] = cur.fetchone()[0]

    # 2. 対象レースの過去エディション
    cur.execute(
        """
        SELECT substr(r.race_date,1,4) AS year, r.race_id, h.horse_name, res.popularity, res.odds
        FROM races r
        JOIN results res ON r.race_id = res.race_id AND res.finish_position = 1
        JOIN horses h ON res.horse_id = h.horse_id
        WHERE r.race_name LIKE ? AND r.race_date < ?
        ORDER BY r.race_date DESC
        """,
        (f"%{args.race_name}%", f"{current_year + 1}-01-01"),
    )
    race_editions = [
        {"year": int(r[0]), "race_id": r[1], "horse_name": r[2],
         "popularity": r[3], "odds": r[4]}
        for r in cur.fetchall()
    ]

    # 3. 欠落分析
    # 年カバレッジ的に薄い年 (100R未満)
    missing_years = [y for y, n in coverage.items() if n < 100]
    # 対象レースのエディションが欠落してる年
    race_years = {e["year"] for e in race_editions}
    expected_years = set(range(current_year - args.years, current_year))
    missing_race_years = sorted(expected_years - race_years)

    # 4. 推奨アクション
    actions = []
    for y in sorted(set(missing_years) | set(missing_race_years)):
        if y >= 2020:  # 取得対象
            actions.append(f"gh workflow run seed_historical.yml -f year={y}")

    result = {
        "race_name": args.race_name,
        "coverage": coverage,
        "race_editions": race_editions,
        "missing_years": sorted(missing_years),
        "missing_race_years": missing_race_years,
        "recommended_actions": actions,
        "data_complete": len(actions) == 0,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
