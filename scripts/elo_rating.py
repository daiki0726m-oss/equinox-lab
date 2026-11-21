#!/usr/bin/env python3
"""Track 3 (simple): 馬の Elo rating system

各馬の "真の能力" を Elo rating で推定。レース結果から相対比較で更新。
既存 ML predictions と並列に運用 → ensemble で精度向上を狙う。

Algorithm:
  - 初期 rating = 1500 (全馬共通)
  - レース後、 finish_position pair-wise で update
  - K-factor は race grade で調整 (G1 では K 大、未勝利では K 小)

  rating_i_new = rating_i + K * (S_i - E_i)
    S_i = 1 if i > j (i がより上位), 0.5 if 引き分け, 0 if i < j
    E_i = 1 / (1 + 10^((R_j - R_i)/400))

Usage:
    python3 scripts/elo_rating.py [--update] [--from 2018-01-01] [--to 2025-12-31]
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import get_db, init_db


# K-factor by grade
K_BY_GRADE = {
    "G1": 32,
    "G2": 28,
    "G3": 24,
    None: 20,  # OP/特別
    "": 20,
}


def get_k_factor(grade, race_class):
    k = K_BY_GRADE.get(grade, 16)
    if race_class in ("未勝利", "新馬"):
        k = 8  # 未勝利は信頼度低い (馬の素性わからない)
    return k


def classify_race(name):
    if not name: return "OP"
    if "未勝利" in name or "新馬" in name: return "未勝利"
    if "1勝" in name: return "1勝"
    if "2勝" in name: return "2勝"
    if "3勝" in name: return "3勝"
    return "OP"


def update_elo(ratings, race_results, k):
    """1 race の結果から各馬の rating を更新。
    race_results: [(horse_id, finish_position), ...] (sorted by finish_position)
    """
    n = len(race_results)
    new_ratings = {}
    # pair-wise すべての組み合わせで更新
    updates = defaultdict(float)
    for i in range(n):
        for j in range(i + 1, n):
            hi, pi = race_results[i]
            hj, pj = race_results[j]
            ri = ratings.get(hi, 1500)
            rj = ratings.get(hj, 1500)
            # S_i = 1 (i が上位), S_j = 0
            ei = 1 / (1 + 10 ** ((rj - ri) / 400))
            ej = 1 - ei
            # update factor scaled by n (large fields → smaller per-pair update)
            updates[hi] += k * (1 - ei) / (n - 1)
            updates[hj] += k * (0 - ej) / (n - 1)
    for h, delta in updates.items():
        new_ratings[h] = ratings.get(h, 1500) + delta
    return new_ratings


def build_ratings(date_from=None, date_to=None):
    """全期間 race results から Elo ratings を時系列で構築"""
    init_db()
    print(f"📥 全 races 取得中 ({date_from} - {date_to})...")

    with get_db() as conn:
        # race_date 順に取得
        q = """
            SELECT r.race_id, r.race_date, r.race_name, r.grade,
                   GROUP_CONCAT(res.horse_id || ':' || res.finish_position) AS results
            FROM races r
            JOIN results res ON r.race_id = res.race_id
            WHERE res.finish_position > 0
        """
        params = []
        if date_from:
            q += " AND r.race_date >= ?"; params.append(date_from)
        if date_to:
            q += " AND r.race_date <= ?"; params.append(date_to)
        q += " GROUP BY r.race_id ORDER BY r.race_date, r.race_id"
        rows = conn.execute(q, params).fetchall()

    print(f"📊 races: {len(rows)}")

    ratings = {}  # horse_id -> rating
    rating_history = []  # 時系列の rating snapshot (毎月)
    race_count = 0
    last_month = None

    for row in rows:
        race_id, race_date, race_name, grade = row["race_id"], row["race_date"], row["race_name"], row["grade"]
        results_str = row["results"] or ""
        if not results_str: continue

        race_class = classify_race(race_name)
        k = get_k_factor(grade, race_class)

        try:
            race_results = []
            for item in results_str.split(","):
                hid, pos = item.split(":")
                race_results.append((hid, int(pos)))
            race_results.sort(key=lambda x: x[1])
        except Exception:
            continue

        if len(race_results) < 3: continue

        new_ratings = update_elo(ratings, race_results, k)
        ratings.update(new_ratings)
        race_count += 1

        month = race_date[:7] if race_date else None
        if month != last_month and month:
            rating_history.append({
                "month": month,
                "n_horses_rated": len(ratings),
                "max_rating": max(ratings.values()) if ratings else 0,
                "min_rating": min(ratings.values()) if ratings else 0,
            })
            last_month = month
            if race_count % 1000 == 0:
                print(f"  {month}: rated horses={len(ratings)} (processed {race_count} races)")

    print(f"\n✅ 集計完了: {len(ratings)} 頭の rating 構築 ({race_count} races)")
    return ratings, rating_history


def verify_elo_predictive(ratings, date_from, date_to):
    """Elo rating の予測力を検証 (テスト期間で 1人気予測 vs Elo top 予測)"""
    init_db()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT r.race_id, r.race_name, r.grade,
                   GROUP_CONCAT(res.horse_id || ':' || res.finish_position || ':' || COALESCE(res.popularity,99)) AS results
            FROM races r
            JOIN results res ON r.race_id = res.race_id
            WHERE r.race_date BETWEEN ? AND ? AND res.finish_position > 0
            GROUP BY r.race_id
            ORDER BY r.race_date
        """, (date_from, date_to)).fetchall()

    elo_wins, market_wins = 0, 0
    elo_top3, market_top3 = 0, 0
    n_races = 0

    for row in rows:
        results_str = row["results"] or ""
        if not results_str: continue
        try:
            race_results = []
            for item in results_str.split(","):
                hid, pos, pop = item.split(":")
                race_results.append((hid, int(pos), int(pop) if pop != "99" else None))
        except Exception:
            continue
        if len(race_results) < 3: continue

        win_horse = next((h for h, p, _ in race_results if p == 1), None)
        top3_horses = {h for h, p, _ in race_results if p <= 3}
        market_fav = next((h for h, p, pop in race_results if pop == 1), None)

        # Elo top horse
        elo_scored = [(h, ratings.get(h, 1500)) for h, _, _ in race_results]
        elo_scored.sort(key=lambda x: -x[1])
        elo_top = elo_scored[0][0]

        if elo_top == win_horse: elo_wins += 1
        if elo_top in top3_horses: elo_top3 += 1
        if market_fav == win_horse: market_wins += 1
        if market_fav in top3_horses: market_top3 += 1
        n_races += 1

    print(f"\n── Elo 予測力検証 ({date_from} - {date_to}, n={n_races}) ──")
    print(f"  Elo top → 1着 率:   {elo_wins/n_races*100:>5.1f}%")
    print(f"  Market 1人気 → 1着 率: {market_wins/n_races*100:>5.1f}%")
    print(f"  Elo top → 3着内 率:  {elo_top3/n_races*100:>5.1f}%")
    print(f"  Market 1人気 → 3着内 率: {market_top3/n_races*100:>5.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="2018-01-01")
    ap.add_argument("--to", dest="date_to", default="2024-12-31")
    ap.add_argument("--verify-from", default="2025-01-01")
    ap.add_argument("--verify-to", default="2025-12-31")
    ap.add_argument("--output", default="docs/analysis/elo_ratings.json")
    args = ap.parse_args()

    # Train period
    ratings, history = build_ratings(args.date_from, args.date_to)

    # Verify
    verify_elo_predictive(ratings, args.verify_from, args.verify_to)

    # Save
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    top_horses = sorted(ratings.items(), key=lambda x: -x[1])[:50]
    out = {
        "train_period": {"from": args.date_from, "to": args.date_to},
        "verify_period": {"from": args.verify_from, "to": args.verify_to},
        "n_horses_rated": len(ratings),
        "top50_horses": [{"horse_id": h, "rating": round(r, 1)} for h, r in top_horses],
        "rating_history_monthly": history[-24:],  # 直近 24 ヶ月
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n📝 JSON 出力: {args.output}")


if __name__ == "__main__":
    main()
