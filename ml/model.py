"""
LightGBM 競馬予測モデル v2
LambdaRank ランキング学習 + binary classification のハイブリッド
"""

import os
import pickle
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score
from ml.features import FeatureBuilder


MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models")


class KeibaModel:
    """
    LightGBMベースの競馬予測モデル v2

    3モデル構成:
    - model_rank: LambdaRank (レース内ランキング最適化)
    - model_top3: Binary (3着以内に入るか)
    - model_win:  Binary (1着になるか)
    """

    RANK_PARAMS = {
        "objective": "lambdarank",
        "metric": "ndcg",
        "ndcg_eval_at": [1, 3, 5],
        "boosting_type": "gbdt",
        "num_leaves": 63,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "lambda_l1": 0.1,
        "lambda_l2": 0.1,
        "min_child_samples": 20,
        "verbose": -1,
        "n_jobs": -1,
        "seed": 42,
    }

    BINARY_PARAMS = {
        "objective": "binary",
        "metric": "binary_logloss",
        "boosting_type": "gbdt",
        "num_leaves": 63,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "lambda_l1": 0.1,
        "lambda_l2": 0.1,
        "min_child_samples": 20,
        "verbose": -1,
        "n_jobs": -1,
        "seed": 42,
    }

    def __init__(self):
        self.model_rank = None   # ランキングモデル
        self.model_top3 = None   # 複勝モデル
        self.model_win = None    # 単勝モデル
        self.feature_columns = FeatureBuilder.get_feature_columns()
        self.feature_builder = FeatureBuilder()

    def _make_group(self, df):
        """レース単位のグループサイズを計算 (LambdaRank用)"""
        return df.groupby("race_id").size().values

    def train(self, df=None, num_boost_round=500, early_stopping_rounds=50):
        """モデルを学習"""
        if df is None:
            print("📊 学習データを構築中...")
            df = self.feature_builder.build_training_data()

        if df.empty:
            print("❌ 学習データがありません。先にデータを収集してください。")
            return None

        # 確定結果のあるデータのみ
        df = df[df["finish_position"] > 0].copy()
        print(f"📊 学習データ: {len(df)}行, {df['race_id'].nunique()}レース")

        X = df[self.feature_columns].fillna(0)
        y_top3 = df["target_top3"]
        y_win = df["target_win"]
        y_rank = df["relevance"]

        # レースIDでソート（LambdaRank用）
        sort_idx = df.groupby("race_id").ngroup().values
        sort_order = np.argsort(sort_idx, kind="stable")
        X = X.iloc[sort_order]
        y_top3 = y_top3.iloc[sort_order]
        y_win = y_win.iloc[sort_order]
        y_rank = y_rank.iloc[sort_order]
        df_sorted = df.iloc[sort_order]

        # 時系列分割による交差検証
        race_ids = df_sorted["race_id"].unique()
        n_races = len(race_ids)
        n_splits = 5
        fold_size = n_races // (n_splits + 1)

        scores_rank = []
        scores_top3 = []
        scores_win = []
        best_iterations = {"rank": [], "top3": [], "win": []}

        print("\n🔄 交差検証中...")
        for fold in range(n_splits):
            train_end = fold_size * (fold + 2)
            val_start = train_end
            val_end = min(val_start + fold_size, n_races)

            if val_end <= val_start:
                continue

            train_races = set(race_ids[:train_end])
            val_races = set(race_ids[val_start:val_end])

            train_mask = df_sorted["race_id"].isin(train_races)
            val_mask = df_sorted["race_id"].isin(val_races)

            X_train, X_val = X[train_mask], X[val_mask]
            df_train = df_sorted[train_mask]
            df_val = df_sorted[val_mask]

            # === LambdaRank ===
            train_group = self._make_group(df_train)
            val_group = self._make_group(df_val)

            train_rank = lgb.Dataset(X_train, label=y_rank[train_mask], group=train_group)
            val_rank = lgb.Dataset(X_val, label=y_rank[val_mask], group=val_group, reference=train_rank)

            model_r = lgb.train(
                self.RANK_PARAMS, train_rank,
                num_boost_round=num_boost_round,
                valid_sets=[val_rank],
                callbacks=[lgb.early_stopping(early_stopping_rounds), lgb.log_evaluation(0)],
            )
            best_iterations["rank"].append(model_r.best_iteration)

            # === Binary (top3) ===
            train_t3 = lgb.Dataset(X_train, label=y_top3[train_mask])
            val_t3 = lgb.Dataset(X_val, label=y_top3[val_mask], reference=train_t3)

            model_t3 = lgb.train(
                self.BINARY_PARAMS, train_t3,
                num_boost_round=num_boost_round,
                valid_sets=[val_t3],
                callbacks=[lgb.early_stopping(early_stopping_rounds), lgb.log_evaluation(0)],
            )
            best_iterations["top3"].append(model_t3.best_iteration)

            pred_t3 = model_t3.predict(X_val)
            auc_t3 = roc_auc_score(y_top3[val_mask], pred_t3)
            scores_top3.append(auc_t3)

            # === Binary (win) ===
            train_w = lgb.Dataset(X_train, label=y_win[train_mask])
            val_w = lgb.Dataset(X_val, label=y_win[val_mask], reference=train_w)

            model_w = lgb.train(
                self.BINARY_PARAMS, train_w,
                num_boost_round=num_boost_round,
                valid_sets=[val_w],
                callbacks=[lgb.early_stopping(early_stopping_rounds), lgb.log_evaluation(0)],
            )
            best_iterations["win"].append(model_w.best_iteration)

            pred_w = model_w.predict(X_val)
            auc_w = roc_auc_score(y_win[val_mask], pred_w)
            scores_win.append(auc_w)

            print(f"  Fold {fold+1}: AUC(複勝)={auc_t3:.4f}, AUC(単勝)={auc_w:.4f}, "
                  f"LambdaRank iterations={model_r.best_iteration}")

        print(f"\n📊 平均AUC(複勝): {np.mean(scores_top3):.4f} ± {np.std(scores_top3):.4f}")
        print(f"📊 平均AUC(単勝): {np.mean(scores_win):.4f} ± {np.std(scores_win):.4f}")

        # 全データで最終モデルを学習 (best_iterationを使用)
        print("\n🏗️ 最終モデルを学習中...")

        group_all = self._make_group(df_sorted)

        best_rank_iter = int(np.mean(best_iterations["rank"])) if best_iterations["rank"] else num_boost_round
        best_t3_iter = int(np.mean(best_iterations["top3"])) if best_iterations["top3"] else num_boost_round
        best_w_iter = int(np.mean(best_iterations["win"])) if best_iterations["win"] else num_boost_round

        print(f"  Best iterations: rank={best_rank_iter}, top3={best_t3_iter}, win={best_w_iter}")

        train_all_rank = lgb.Dataset(X, label=y_rank, group=group_all)
        self.model_rank = lgb.train(
            self.RANK_PARAMS, train_all_rank, num_boost_round=best_rank_iter
        )

        train_all_t3 = lgb.Dataset(X, label=y_top3)
        self.model_top3 = lgb.train(
            self.BINARY_PARAMS, train_all_t3, num_boost_round=best_t3_iter
        )

        train_all_w = lgb.Dataset(X, label=y_win)
        self.model_win = lgb.train(
            self.BINARY_PARAMS, train_all_w, num_boost_round=best_w_iter
        )

        # 特徴量重要度
        importance = pd.DataFrame({
            "feature": self.feature_columns,
            "importance_rank": self.model_rank.feature_importance(importance_type="gain"),
            "importance_top3": self.model_top3.feature_importance(importance_type="gain"),
            "importance_win": self.model_win.feature_importance(importance_type="gain"),
        }).sort_values("importance_rank", ascending=False)

        print("\n📊 特徴量重要度 (Top 15, Ranking Model):")
        print(importance.head(15).to_string(index=False))

        self.save()

        return {
            "auc_top3": np.mean(scores_top3),
            "auc_win": np.mean(scores_win),
            "importance": importance,
        }

    def predict(self, features_df):
        """予測を実行"""
        if self.model_top3 is None or self.model_win is None:
            self.load()

        if self.model_top3 is None:
            raise ValueError("モデルが学習されていません。先に train() を実行してください。")

        X = features_df[self.feature_columns].fillna(0)

        pred_top3 = self.model_top3.predict(X)
        pred_win = self.model_win.predict(X)

        # LambdaRankモデルがある場合はランキング予測
        if self.model_rank is not None:
            pred_rank = self.model_rank.predict(X)
        else:
            pred_rank = pred_win  # フォールバック

        return {
            "rank_score": pred_rank,
            "prob_top3": pred_top3,
            "prob_win": pred_win,
        }

    def predict_race(self, race_id):
        """レース全頭の予測"""
        df = self.feature_builder.build_features_for_race(race_id)
        if df.empty:
            return pd.DataFrame()

        preds = self.predict(df)
        df["rank_score"] = preds["rank_score"]
        df["pred_top3"] = preds["prob_top3"]
        df["pred_win"] = preds["prob_win"]

        # ランキングスコアを正規化 (softmax)
        rank_exp = np.exp(df["rank_score"] - df["rank_score"].max())
        df["pred_win_norm"] = rank_exp / rank_exp.sum()
        df["pred_top3_norm"] = df["pred_top3"] / df["pred_top3"].sum() * 3  # 3頭入着

        return df.sort_values("rank_score", ascending=False)

    def save(self, path=None):
        """モデルを保存"""
        os.makedirs(MODEL_DIR, exist_ok=True)
        path = path or MODEL_DIR

        with open(os.path.join(path, "model_rank.pkl"), "wb") as f:
            pickle.dump(self.model_rank, f)
        with open(os.path.join(path, "model_top3.pkl"), "wb") as f:
            pickle.dump(self.model_top3, f)
        with open(os.path.join(path, "model_win.pkl"), "wb") as f:
            pickle.dump(self.model_win, f)

        print(f"💾 モデル保存完了: {path}")

    def load(self, path=None):
        """モデルを読み込み"""
        path = path or MODEL_DIR

        rank_path = os.path.join(path, "model_rank.pkl")
        top3_path = os.path.join(path, "model_top3.pkl")
        win_path = os.path.join(path, "model_win.pkl")

        if os.path.exists(top3_path) and os.path.exists(win_path):
            if os.path.exists(rank_path):
                with open(rank_path, "rb") as f:
                    self.model_rank = pickle.load(f)
            with open(top3_path, "rb") as f:
                self.model_top3 = pickle.load(f)
            with open(win_path, "rb") as f:
                self.model_win = pickle.load(f)
            print("📂 モデル読み込み完了")
        else:
            print("⚠️ 保存済みモデルが見つかりません")
