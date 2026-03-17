"""
新しい betting.py 閾値でのバックテスト
fast_train.py のデータインフラを流用 + strategy/betting.py を使用
"""

import sys, os, time
import numpy as np
import pandas as pd
import pickle

sys.path.insert(0, os.path.dirname(__file__))

from fast_train import (
    load_all_data, build_horse_history,
    build_jockey_trainer_stats, build_speed_index_cache,
    compute_features_fast, get_feature_columns
)
from strategy.betting import BettingStrategy
from database import init_db

MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")


def main():
    print("📊 新閾値バックテスト")
    print("=" * 50)

    init_db()
    t0 = time.time()

    # データロード
    races_df, results_df, payouts_df = load_all_data()
    race_info = races_df.set_index("race_id")[
        ["race_date", "venue", "distance", "surface", "track_condition", "horse_count"]
    ].to_dict("index")

    for col in ["race_date", "venue", "distance", "surface", "track_condition", "horse_count"]:
        results_df[col] = results_df["race_id"].map(
            lambda rid, c=col: race_info.get(rid, {}).get(c, "")
        )

    horse_history = build_horse_history(results_df, races_df)
    jockey_stats, trainer_stats, combo_stats = build_jockey_trainer_stats(results_df, races_df)
    si_cache = build_speed_index_cache(results_df, races_df)

    print("🔧 特徴量計算中...")
    all_rows = []
    for i, race in races_df.iterrows():
        race_results = results_df[results_df["race_id"] == race["race_id"]]
        if race_results.empty:
            continue
        rows = compute_features_fast(
            race.to_dict(), race_results.to_dict("records"),
            horse_history, jockey_stats, trainer_stats, combo_stats, si_cache
        )
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    df["race_date"] = df["race_id"].map(
        lambda rid: race_info.get(rid, {}).get("race_date", "")
    )
    feature_cols = get_feature_columns()

    # モデル読み込み
    with open(os.path.join(MODEL_DIR, "model_rank.pkl"), "rb") as f:
        model_rank = pickle.load(f)
    with open(os.path.join(MODEL_DIR, "model_top3.pkl"), "rb") as f:
        model_top3 = pickle.load(f)
    with open(os.path.join(MODEL_DIR, "model_win.pkl"), "rb") as f:
        model_win = pickle.load(f)

    # 2025年テストデータ
    test = df[(df["finish_position"] > 0) &
              (df["race_date"].str.startswith("2025"))].copy()
    X_test = test[feature_cols].fillna(0)
    test["rank_score"] = model_rank.predict(X_test)
    test["pred_win"] = model_win.predict(X_test)
    test["pred_top3"] = model_top3.predict(X_test)

    strategy = BettingStrategy()

    # 各レースでバックテスト
    total_bet = 0
    total_payout = 0
    race_count = 0
    bet_count = 0
    hit_count = 0
    stats_by_type = {}

    for race_id, group in test.groupby("race_id"):
        group = group.sort_values("rank_score", ascending=False).copy()
        rank_exp = np.exp(group["rank_score"] - group["rank_score"].max())
        group["win_prob"] = rank_exp / rank_exp.sum()

        # predictions format for betting.py
        predictions = []
        for _, row in group.iterrows():
            odds = row["odds"]
            if odds <= 0:
                continue
            predictions.append({
                "horse_number": int(row["horse_number"]),
                "pred_win": row["win_prob"],
                "pred_top3": row["pred_top3"],
                "odds_win": odds,
                "odds_place": max(odds * 0.3, 1.1),
                "horse_name": "",
            })

        if not predictions:
            continue

        should_bet, reason = strategy.should_bet_race(predictions)
        if not should_bet:
            continue

        race_count += 1
        bets_result = strategy.generate_bets(predictions, bet_types=["単勝", "複勝"])

        # 的中判定
        win_nums = set(group[group["finish_position"] == 1]["horse_number"].astype(int))
        top3_nums = set(group[group["finish_position"] <= 3]["horse_number"].astype(int))

        for bet in bets_result["bets"]:
            total_bet += bet["amount"]
            bet_count += 1
            bt = bet["type"]

            if bt not in stats_by_type:
                stats_by_type[bt] = {"bet": 0, "payout": 0, "count": 0, "hits": 0}

            stats_by_type[bt]["bet"] += bet["amount"]
            stats_by_type[bt]["count"] += 1

            is_hit = False
            if bt == "単勝":
                is_hit = bet["horse_numbers"][0] in win_nums
                if is_hit:
                    payout = bet["amount"] * bet["odds"]
                    total_payout += payout
                    stats_by_type[bt]["payout"] += payout
            elif bt == "複勝":
                is_hit = bet["horse_numbers"][0] in top3_nums
                if is_hit:
                    payout = bet["amount"] * bet["odds"]
                    total_payout += payout
                    stats_by_type[bt]["payout"] += payout

            if is_hit:
                hit_count += 1
                stats_by_type[bt]["hits"] += 1

    elapsed = time.time() - t0
    roi = (total_payout / total_bet * 100) if total_bet > 0 else 0

    print(f"\n{'='*50}")
    print(f"📊 バックテスト結果 (2025年, betting.py 新閾値)")
    print(f"{'='*50}")
    print(f"  対象レース: {race_count}")
    print(f"  購入点数:   {bet_count}")
    print(f"  的中数:     {hit_count}")
    print(f"  的中率:     {hit_count/bet_count*100:.1f}%" if bet_count else "  的中率:     -")
    print(f"  総賭け金:   ¥{total_bet:,}")
    print(f"  総払戻金:   ¥{total_payout:,.0f}")
    print(f"  損益:       ¥{total_payout - total_bet:,.0f}")
    print(f"  回収率:     {roi:.1f}%")
    print(f"  計算時間:   {elapsed:.1f}秒")

    print(f"\n── 券種別 ──")
    for bt, s in sorted(stats_by_type.items()):
        bt_roi = (s["payout"] / s["bet"] * 100) if s["bet"] > 0 else 0
        bt_hit = (s["hits"] / s["count"] * 100) if s["count"] > 0 else 0
        print(f"  {bt}: {s['count']}件 的中{s['hits']}件({bt_hit:.1f}%) "
              f"投資¥{s['bet']:,} 回収¥{s['payout']:,.0f} ROI={bt_roi:.1f}%")


if __name__ == "__main__":
    main()
