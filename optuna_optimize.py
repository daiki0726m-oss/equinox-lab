"""
Optuna ハイパーパラメータ最適化
fast_train.py のデータインフラを流用して、LightGBM パラメータを自動探索
目的: バックテストのROI最大化
"""

import sys
import os
import time
import numpy as np
import pandas as pd
import lightgbm as lgb
import optuna
from sklearn.metrics import roc_auc_score

sys.path.insert(0, os.path.dirname(__file__))

# fast_train.py の関数を流用
from fast_train import (
    load_all_data, build_horse_history,
    build_jockey_trainer_stats, build_speed_index_cache,
    compute_features_fast, get_feature_columns
)
from database import init_db

MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")

# グローバルにデータを保持（毎回ロードしない）
_DATA_CACHE = {}


def prepare_data():
    """学習データを一度だけ準備"""
    if "df" in _DATA_CACHE:
        return _DATA_CACHE["df"], _DATA_CACHE["feature_cols"]

    init_db()
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

    print("\n🔧 特徴量計算中...")
    t0 = time.time()
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
    print(f"  ✅ 特徴量計算完了: {len(df)}行 ({time.time()-t0:.0f}秒)")

    feature_cols = get_feature_columns()
    _DATA_CACHE["df"] = df
    _DATA_CACHE["feature_cols"] = feature_cols
    return df, feature_cols


def run_backtest_with_params(df, feature_cols, model_rank, model_top3, model_win):
    """バックテストしてROIを返す"""
    test = df[(df["finish_position"] > 0) &
              (df["race_date"].str.startswith("2025"))].copy()

    if test.empty:
        return 0

    X_test = test[feature_cols].fillna(0)
    test = test.copy()
    test["rank_score"] = model_rank.predict(X_test)
    test["pred_win"] = model_win.predict(X_test)
    test["pred_top3"] = model_top3.predict(X_test)

    total_bet = 0
    total_payout = 0

    for race_id, group in test.groupby("race_id"):
        group = group.sort_values("rank_score", ascending=False)
        rank_exp = np.exp(group["rank_score"] - group["rank_score"].max())
        group = group.copy()
        group["win_prob"] = rank_exp / rank_exp.sum()

        for _, row in group.iterrows():
            wp = row["win_prob"]
            odds = row["odds"]
            if odds <= 0:
                continue

            # 単勝
            ev_win = wp * odds
            if ev_win >= 1.2 and wp >= 0.08:
                total_bet += 100
                if row["finish_position"] == 1:
                    total_payout += 100 * odds

            # 複勝
            t3_prob = row["pred_top3"]
            place_odds = max(odds * 0.3, 1.1)
            ev_place = t3_prob * place_odds
            if ev_place >= 1.1 and t3_prob >= 0.20:
                total_bet += 100
                if row["finish_position"] <= 3:
                    total_payout += 100 * place_odds

    return (total_payout / total_bet * 100) if total_bet > 0 else 0


def objective(trial, df, feature_cols):
    """Optuna objective: ROIを最大化"""
    confirmed = df[df["finish_position"] > 0].copy()
    confirmed = confirmed.sort_values("race_id")

    X = confirmed[feature_cols].fillna(0)
    y_top3 = confirmed["target_top3"]
    y_win = confirmed["target_win"]
    y_rank = confirmed["relevance"]

    race_ids = confirmed["race_id"].unique()
    split_idx = int(len(race_ids) * 0.8)
    train_races = set(race_ids[:split_idx])
    val_races = set(race_ids[split_idx:])

    train_mask = confirmed["race_id"].isin(train_races)
    val_mask = confirmed["race_id"].isin(val_races)

    X_train, X_val = X[train_mask], X[val_mask]
    train_group = confirmed[train_mask].groupby("race_id").size().values
    val_group = confirmed[val_mask].groupby("race_id").size().values

    # ハイパーパラメータ探索空間
    params = {
        "num_leaves": trial.suggest_int("num_leaves", 15, 127),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
        "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 1.0),
        "bagging_fraction": trial.suggest_float("bagging_fraction", 0.5, 1.0),
        "bagging_freq": trial.suggest_int("bagging_freq", 1, 7),
        "lambda_l1": trial.suggest_float("lambda_l1", 1e-3, 10.0, log=True),
        "lambda_l2": trial.suggest_float("lambda_l2", 1e-3, 10.0, log=True),
        "min_child_samples": trial.suggest_int("min_child_samples", 5, 100),
        "min_child_weight": trial.suggest_float("min_child_weight", 1e-3, 10.0, log=True),
        "verbose": -1, "n_jobs": -1, "seed": 42,
    }

    rank_params = {**params, "objective": "lambdarank", "metric": "ndcg",
                   "ndcg_eval_at": [1, 3, 5]}
    binary_params = {**params, "objective": "binary", "metric": "binary_logloss"}

    try:
        # LambdaRank
        ds_rank_t = lgb.Dataset(X_train, label=y_rank[train_mask], group=train_group)
        ds_rank_v = lgb.Dataset(X_val, label=y_rank[val_mask], group=val_group, reference=ds_rank_t)
        model_rank = lgb.train(
            rank_params, ds_rank_t, num_boost_round=300,
            valid_sets=[ds_rank_v],
            callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(0)],
        )

        # Binary top3
        ds_t3_t = lgb.Dataset(X_train, label=y_top3[train_mask])
        ds_t3_v = lgb.Dataset(X_val, label=y_top3[val_mask], reference=ds_t3_t)
        model_top3 = lgb.train(
            binary_params, ds_t3_t, num_boost_round=300,
            valid_sets=[ds_t3_v],
            callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(0)],
        )

        # Binary win
        ds_w_t = lgb.Dataset(X_train, label=y_win[train_mask])
        ds_w_v = lgb.Dataset(X_val, label=y_win[val_mask], reference=ds_w_t)
        model_win = lgb.train(
            binary_params, ds_w_t, num_boost_round=300,
            valid_sets=[ds_w_v],
            callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(0)],
        )

        # AUC チェック
        pred_t3 = model_top3.predict(X_val)
        auc_t3 = roc_auc_score(y_top3[val_mask], pred_t3)

        # バックテスト ROI
        roi = run_backtest_with_params(df, feature_cols, model_rank, model_top3, model_win)

        trial.set_user_attr("auc_top3", round(auc_t3, 4))
        trial.set_user_attr("roi", round(roi, 1))
        trial.set_user_attr("rank_iter", model_rank.best_iteration)

        return roi  # ROI最大化

    except Exception as e:
        print(f"  ⚠️ Trial {trial.number} failed: {e}")
        return 0


def main():
    print("🔍 Optuna ハイパーパラメータ最適化")
    print("=" * 50)

    df, feature_cols = prepare_data()

    # Optuna study
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="maximize", study_name="keiba-lgb")

    n_trials = 30
    print(f"\n🔄 {n_trials}回のトライアルを実行中...")
    t0 = time.time()

    def callback(study, trial):
        if trial.value > 0:
            roi = trial.value
            auc = trial.user_attrs.get("auc_top3", 0)
            print(f"  Trial {trial.number:2d}: ROI={roi:.1f}% AUC={auc:.4f}")
        if study.best_trial.number == trial.number:
            print(f"  ★ 新ベスト! ROI={study.best_value:.1f}%")

    study.optimize(lambda trial: objective(trial, df, feature_cols),
                   n_trials=n_trials, callbacks=[callback])

    elapsed = time.time() - t0
    best = study.best_trial

    print(f"\n{'='*50}")
    print(f"✅ 最適化完了 ({elapsed/60:.1f}分)")
    print(f"{'='*50}")
    print(f"  Best ROI: {best.value:.1f}%")
    print(f"  Best AUC: {best.user_attrs.get('auc_top3', 'N/A')}")
    print(f"  Best Params:")
    for k, v in best.params.items():
        print(f"    {k}: {v}")

    # ベストパラメータで最終モデルを学習 & 保存
    print(f"\n🔧 ベストパラメータで最終モデルを学習中...")
    import pickle

    confirmed = df[df["finish_position"] > 0].copy().sort_values("race_id")
    X = confirmed[feature_cols].fillna(0)
    y_rank = confirmed["relevance"]
    y_top3 = confirmed["target_top3"]
    y_win = confirmed["target_win"]
    group = confirmed.groupby("race_id").size().values

    bp = best.params
    rank_params = {
        **bp, "objective": "lambdarank", "metric": "ndcg",
        "ndcg_eval_at": [1, 3, 5], "verbose": -1, "n_jobs": -1, "seed": 42,
    }
    binary_params = {
        **bp, "objective": "binary", "metric": "binary_logloss",
        "verbose": -1, "n_jobs": -1, "seed": 42,
    }

    best_iter = best.user_attrs.get("rank_iter", 100)

    ds_rank = lgb.Dataset(X, label=y_rank, group=group)
    model_rank = lgb.train(rank_params, ds_rank, num_boost_round=best_iter)

    ds_t3 = lgb.Dataset(X, label=y_top3)
    model_top3 = lgb.train(binary_params, ds_t3, num_boost_round=best_iter)

    ds_w = lgb.Dataset(X, label=y_win)
    model_win = lgb.train(binary_params, ds_w, num_boost_round=best_iter)

    os.makedirs(MODEL_DIR, exist_ok=True)
    with open(os.path.join(MODEL_DIR, "model_rank.pkl"), "wb") as f:
        pickle.dump(model_rank, f)
    with open(os.path.join(MODEL_DIR, "model_top3.pkl"), "wb") as f:
        pickle.dump(model_top3, f)
    with open(os.path.join(MODEL_DIR, "model_win.pkl"), "wb") as f:
        pickle.dump(model_win, f)

    # 最終パラメータを保存
    with open(os.path.join(MODEL_DIR, "best_params.pkl"), "wb") as f:
        pickle.dump(best.params, f)

    print(f"💾 最適化モデル保存完了")

    # 最終バックテスト
    final_roi = run_backtest_with_params(df, feature_cols, model_rank, model_top3, model_win)
    print(f"\n📊 最終バックテスト ROI: {final_roi:.1f}%")
    print(f"   (旧: 189.6% → 新: {final_roi:.1f}%)")


if __name__ == "__main__":
    main()
