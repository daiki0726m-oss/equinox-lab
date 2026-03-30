"""
高速モデル学習スクリプト
- 全データを一括でメモリにロード（個別クエリを排除）
- プリコンパイル済み特徴量をキャッシュ
- 学習 → バックテストまで一気に実行
"""

import sys
import os
import time
import numpy as np
import pandas as pd
import lightgbm as lgb
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from database import init_db, get_db

MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")


def load_all_data():
    """全データをメモリに一括ロード"""
    print("📦 全データをメモリにロード中...")
    t0 = time.time()

    with get_db() as conn:
        races_df = pd.read_sql("SELECT * FROM races WHERE race_date IS NOT NULL ORDER BY race_date", conn)
        results_df = pd.read_sql("""
            SELECT r.*, h.horse_name, h.sire, h.damsire, h.birth_year
            FROM results r
            LEFT JOIN horses h ON r.horse_id = h.horse_id
            ORDER BY r.race_id, r.horse_number
        """, conn)
        payouts_df = pd.read_sql("SELECT * FROM payouts", conn)

    elapsed = time.time() - t0
    print(f"  ✅ {len(races_df)}レース, {len(results_df)}件のデータをロード ({elapsed:.1f}秒)")
    return races_df, results_df, payouts_df


def build_horse_history(results_df, races_df):
    """馬ごとの過去レース履歴をプリコンパイル"""
    print("🔧 馬の履歴データを構築中...")
    t0 = time.time()

    # race_id → race_date マッピング
    race_dates = dict(zip(races_df["race_id"], races_df["race_date"]))

    # 結果に日付を追加
    res = results_df.copy()
    res["race_date"] = res["race_id"].map(race_dates)
    res = res.sort_values(["horse_id", "race_date"])

    # 馬ごとのデータをグループ化
    history = {}
    for horse_id, group in res.groupby("horse_id"):
        history[horse_id] = group.to_dict("records")

    elapsed = time.time() - t0
    print(f"  ✅ {len(history)}頭の履歴を構築 ({elapsed:.1f}秒)")
    return history


def build_jockey_trainer_stats(results_df, races_df):
    """騎手・調教師の成績統計をプリコンパイル"""
    print("🔧 騎手・調教師統計を構築中...")
    t0 = time.time()

    needed_cols = ["venue", "distance", "surface", "track_condition"]
    if all(c in results_df.columns for c in needed_cols):
        res = results_df.copy()
    else:
        res = results_df.merge(
            races_df[["race_id"] + needed_cols],
            on="race_id", how="left"
        )
    confirmed = res[res["finish_position"] > 0].copy()

    # 騎手の全体成績
    jockey_stats = {}
    for jid, g in confirmed.groupby("jockey_id"):
        total = len(g)
        if total < 5:
            jockey_stats[jid] = {"win_rate": 0, "top3_rate": 0, "total": total}
            continue
        wins = (g["finish_position"] == 1).sum()
        top3 = (g["finish_position"] <= 3).sum()
        jockey_stats[jid] = {
            "win_rate": wins / total,
            "top3_rate": top3 / total,
            "total": total,
        }

    # 調教師の全体成績
    trainer_stats = {}
    for tid, g in confirmed.groupby("trainer_id"):
        total = len(g)
        if total < 5:
            trainer_stats[tid] = {"top3_rate": 0, "total": total}
            continue
        top3 = (g["finish_position"] <= 3).sum()
        trainer_stats[tid] = {
            "top3_rate": top3 / total,
            "total": total,
        }

    # 騎手×調教師コンビ
    combo_stats = {}
    for (jid, tid), g in confirmed.groupby(["jockey_id", "trainer_id"]):
        total = len(g)
        if total < 3:
            continue
        top3 = (g["finish_position"] <= 3).sum()
        combo_stats[(jid, tid)] = {"top3_rate": top3 / total, "total": total}

    elapsed = time.time() - t0
    print(f"  ✅ 騎手{len(jockey_stats)}, 調教師{len(trainer_stats)}, "
          f"コンビ{len(combo_stats)} ({elapsed:.1f}秒)")
    return jockey_stats, trainer_stats, combo_stats


def build_speed_index_cache(results_df, races_df):
    """スピード指数をプリコンパイル"""
    print("🔧 スピード指数を計算中...")
    t0 = time.time()

    # results_dfにはmain()でrace_infoカラムが追加済みの場合がある
    needed_cols = ["venue", "distance", "surface", "track_condition"]
    if all(c in results_df.columns for c in needed_cols):
        res = results_df.copy()
    else:
        res = results_df.merge(
            races_df[["race_id"] + needed_cols],
            on="race_id", how="left"
        )
    confirmed = res[(res["finish_position"] > 0) & (res["finish_time_seconds"] > 0)].copy()

    # 基準タイム: venue-distance-surface-condition の上位5着平均
    top5 = confirmed[confirmed["finish_position"] <= 5]
    base_times = top5.groupby(["venue", "distance", "surface", "track_condition"])[
        "finish_time_seconds"
    ].mean().to_dict()

    # フォールバック: venue-distance-surface
    base_times_fallback = top5.groupby(["venue", "distance", "surface"])[
        "finish_time_seconds"
    ].mean().to_dict()

    # 距離ファクター
    def get_dist_factor(d):
        if d < 1200: return 15.0
        elif d < 1600: return 12.0
        elif d < 2000: return 10.0
        elif d < 2500: return 8.5
        else: return 7.0

    # 馬場補正
    track_adj = {"良": 0, "稍重": -1.5, "重": -3.0, "不良": -5.0}

    # 各出走のSI計算
    si_cache = {}
    for _, row in confirmed.iterrows():
        key = (row["venue"], row["distance"], row["surface"], row["track_condition"])
        bt = base_times.get(key)
        if bt is None:
            bt = base_times_fallback.get((row["venue"], row["distance"], row["surface"]))
        if bt is None or bt <= 0:
            continue

        time_diff = bt - row["finish_time_seconds"]
        df = get_dist_factor(row["distance"])
        ta = track_adj.get(row["track_condition"], 0)
        si = 80 + (time_diff * df) + ta

        if row["horse_id"] not in si_cache:
            si_cache[row["horse_id"]] = []
        si_cache[row["horse_id"]].append({
            "si": round(si, 1),
            "race_date": row.get("race_date", ""),
        })

    # 日付順にソート
    for hid in si_cache:
        si_cache[hid].sort(key=lambda x: x["race_date"], reverse=True)

    elapsed = time.time() - t0
    print(f"  ✅ {len(si_cache)}頭のSIを計算 ({elapsed:.1f}秒)")
    return si_cache


def compute_features_fast(race, race_results, horse_history, jockey_stats,
                          trainer_stats, combo_stats, si_cache):
    """1レース分の特徴量を高速計算"""
    rows = []
    race_date = race["race_date"]
    hc = race["horse_count"] or len(race_results)

    for r in race_results:
        f = {}
        horse_id = r["horse_id"]
        jockey_id = r.get("jockey_id", "")
        trainer_id = r.get("trainer_id", "")

        # --- 過去レースをフィルタ（未来リーク防止）---
        past_races = []
        if horse_id in horse_history:
            past_races = [h for h in horse_history[horse_id]
                         if h.get("race_date", "") < race_date
                         and h.get("finish_position", 0) > 0]

        # === SI系 (6) ===
        si_list = []
        if horse_id in si_cache:
            si_list = [s["si"] for s in si_cache[horse_id]
                      if s["race_date"] < race_date][:5]

        f["si_avg"] = np.mean(si_list) if si_list else 0
        f["si_max"] = max(si_list) if si_list else 0
        f["si_min"] = min(si_list) if si_list else 0
        f["si_std"] = np.std(si_list) if len(si_list) > 1 else 0
        f["si_latest"] = si_list[0] if si_list else 0
        f["si_count"] = len(si_list)

        # === 血統系 (3) ===
        f["pedigree_score"] = 50
        f["sire_top3_rate"] = 0
        f["sire_sample_size"] = 0

        # === 騎手・調教師系 (5) ===
        js = jockey_stats.get(jockey_id, {})
        ts = trainer_stats.get(trainer_id, {})
        cs = combo_stats.get((jockey_id, trainer_id), {})

        jt_score = 50
        if js.get("total", 0) >= 30:
            jt_score += (js["top3_rate"] * 100 - 25) * 0.3
        if ts.get("total", 0) >= 20:
            jt_score += (ts["top3_rate"] * 100 - 25) * 0.15
        jt_score = max(0, min(100, jt_score))

        f["jt_score"] = jt_score
        f["jockey_cond_top3"] = js.get("top3_rate", 0) * 100
        f["jockey_cond_win"] = js.get("win_rate", 0) * 100
        f["trainer_cond_top3"] = ts.get("top3_rate", 0) * 100
        f["combo_top3"] = cs.get("top3_rate", 0) * 100

        # === 馬場バイアス系 (2) ===
        f["bias_score"] = 50
        hn = r.get("horse_number", 1)
        f["post_position_ratio"] = hn / max(hc, 1)

        # === ペース系 (3) ===
        # 過去レースの通過順から脚質推定
        front_count = 0
        pos_ratios = []
        last_3fs = []
        for pr in past_races[:5]:
            po = pr.get("passing_order", "")
            if po:
                try:
                    positions = [int(p) for p in po.replace("-", ",").split(",") if p.strip().isdigit()]
                    if positions:
                        ratio = positions[0] / max(pr.get("horse_count", 14) or 14, 1)
                        pos_ratios.append(ratio)
                        if ratio <= 0.35:
                            front_count += 1
                except ValueError:
                    pass
            if pr.get("last_3f", 0) and pr["last_3f"] > 0:
                last_3fs.append(pr["last_3f"])

        n_past = min(len(past_races[:5]), 1)  # avoid div by 0
        f["front_rate"] = front_count / max(len(past_races[:5]), 1)
        f["avg_pos_ratio"] = np.mean(pos_ratios) if pos_ratios else 0.5
        f["avg_last_3f"] = np.mean(last_3fs) if last_3fs else 0

        # === 馬情報 (7) ===
        f["horse_count"] = hc
        f["weight"] = (r.get("weight", 0) or 0) / 500
        f["weight_change"] = (r.get("weight_change", 0) or 0) / 20
        f["impost"] = r.get("impost", 0) or 0
        dist = race.get("distance", 1600) or 1600
        f["distance_cat"] = (0 if dist < 1400 else 1 if dist < 1800 else
                             2 if dist < 2200 else 3 if dist < 2800 else 4)
        f["surface_turf"] = 1 if race.get("surface") == "芝" else 0

        # 休養日数
        if past_races:
            try:
                from datetime import datetime
                d1 = datetime.strptime(race_date, "%Y-%m-%d")
                d2 = datetime.strptime(past_races[0]["race_date"], "%Y-%m-%d")
                rest = (d1 - d2).days
                f["rest_days"] = min(rest, 365) / 365
            except (ValueError, TypeError):
                f["rest_days"] = 0.5
        else:
            f["rest_days"] = 0.5

        # === 過去成績系 (5) ===
        positions = [pr["finish_position"] for pr in past_races[:10]]
        last5 = positions[:5]
        total_past = len(positions) if positions else 1

        f["avg_finish_5r"] = (sum(last5) / len(last5) / 18) if last5 else 0
        f["win_rate_10r"] = sum(1 for p in positions if p == 1) / total_past if positions else 0
        f["top3_rate_10r"] = sum(1 for p in positions if p <= 3) / total_past if positions else 0

        trend = 0
        if len(last5) >= 3:
            trend = (last5[0] - last5[2]) / 2
        f["finish_trend"] = np.clip(trend / 10, -1, 1)
        f["race_experience"] = min(len(positions), 10) / 10

        # === コンテキスト系 (5) ===
        if past_races:
            prev = past_races[0]
            prev_dist = prev.get("distance", dist) or dist
            f["distance_diff"] = (dist - prev_dist) / 400
            f["jockey_change"] = 1 if prev.get("jockey_id") != jockey_id else 0
        else:
            f["distance_diff"] = 0
            f["jockey_change"] = 0

        f["last_3f_best"] = (min(last_3fs) / 40) if last_3fs else 0

        # 同コース成績
        surface = race.get("surface", "")
        venue = race.get("venue", "")
        course_past = [pr for pr in past_races
                      if pr.get("venue") == venue
                      and pr.get("surface") == surface
                      and abs((pr.get("distance", 0) or 0) - dist) <= 200]
        if course_past:
            f["course_top3_rate"] = sum(1 for p in course_past if p["finish_position"] <= 3) / len(course_past)
        else:
            f["course_top3_rate"] = 0

        # 馬体重推移
        weights = [pr.get("weight", 0) for pr in past_races[:3] if pr.get("weight", 0) > 0]
        f["weight_trend"] = ((weights[0] - weights[-1]) / 20) if len(weights) >= 2 else 0

        # === 天候・馬場系 (5) ===
        tc = race.get("track_condition", "") or ""
        weather = race.get("weather", "") or ""
        tc_map = {"良": 0, "稍": 1, "稍重": 1, "重": 2, "不": 3, "不良": 3}
        wt_map = {"晴": 0, "曇": 1, "小雨": 2, "雨": 2, "小雪": 3, "雪": 3}
        f["track_cond_code"] = tc_map.get(tc, 0)
        f["weather_code"] = wt_map.get(weather, 0)
        f["is_heavy_track"] = 1 if tc in ("重", "不", "不良") else 0

        # 該当馬の重馬場時パフォーマンス
        wet_races = [pr for pr in past_races if pr.get("track_condition", "") in ("重", "不", "不良")]
        if wet_races:
            f["horse_wet_win_rate"] = sum(1 for p in wet_races if p["finish_position"] == 1) / len(wet_races)
            f["horse_wet_top3_rate"] = sum(1 for p in wet_races if p["finish_position"] <= 3) / len(wet_races)
        else:
            f["horse_wet_win_rate"] = 0
            f["horse_wet_top3_rate"] = 0

        # === 年齢系 (2) ===
        birth_year = r.get("birth_year", 0) or 0
        if birth_year and race_date:
            try:
                race_year = int(race_date[:4])
                f["horse_age"] = race_year - birth_year
                f["is_peak_age"] = 1 if 3 <= (race_year - birth_year) <= 5 else 0
            except (ValueError, TypeError):
                f["horse_age"] = 0
                f["is_peak_age"] = 0
        else:
            f["horse_age"] = 0
            f["is_peak_age"] = 0

        # === 枠順コース別成績 (2) ===
        same_post_past = [pr for pr in past_races
                          if pr.get("venue") == venue
                          and pr.get("surface") == surface
                          and abs((pr.get("horse_number", 0) or 0) - hn) <= 2]
        if same_post_past:
            f["post_win_rate_course"] = sum(1 for p in same_post_past if p["finish_position"] == 1) / len(same_post_past)
            f["post_top3_rate_course"] = sum(1 for p in same_post_past if p["finish_position"] <= 3) / len(same_post_past)
        else:
            f["post_win_rate_course"] = 0
            f["post_top3_rate_course"] = 0

        # === 年齢×クラス (1) ===
        age = f["horse_age"]
        age_class_past = [pr for pr in past_races
                          if abs((pr.get("distance", 0) or 0) - dist) <= 400]
        if age_class_past and age > 0:
            f["age_class_top3_rate"] = sum(1 for p in age_class_past if p["finish_position"] <= 3) / len(age_class_past)
        else:
            f["age_class_top3_rate"] = 0

        # === 距離別成績 (2) ===
        dist_past = [pr for pr in past_races
                     if abs((pr.get("distance", 0) or 0) - dist) <= 200]
        if dist_past:
            f["dist_win_rate"] = sum(1 for p in dist_past if p["finish_position"] == 1) / len(dist_past)
            f["dist_top3_rate"] = sum(1 for p in dist_past if p["finish_position"] <= 3) / len(dist_past)
        else:
            f["dist_win_rate"] = 0
            f["dist_top3_rate"] = 0

        # === ターゲット ===
        fp = r.get("finish_position", 0) or 0
        f["target_win"] = 1 if fp == 1 else 0
        f["target_top3"] = 1 if 1 <= fp <= 3 else 0
        f["finish_position"] = fp
        f["relevance"] = max(0, hc - fp + 1) if fp > 0 else 0
        f["race_id"] = race["race_id"]
        f["horse_id"] = horse_id
        f["horse_number"] = hn
        f["odds"] = r.get("odds", 0) or 0

        rows.append(f)

    return rows


def get_feature_columns():
    """特徴量カラム一覧 — ml/features.py と完全一致させること"""
    return [
        "si_avg", "si_max", "si_min", "si_std", "si_latest", "si_count",
        "pedigree_score", "sire_top3_rate", "sire_sample_size",
        "jt_score", "jockey_cond_top3", "jockey_cond_win",
        "trainer_cond_top3", "combo_top3",
        "bias_score", "post_position_ratio",
        "front_rate", "avg_pos_ratio", "avg_last_3f",
        "horse_count", "weight", "weight_change",
        "impost", "distance_cat", "surface_turf", "rest_days",
        "avg_finish_5r", "win_rate_10r", "top3_rate_10r",
        "finish_trend", "race_experience",
        "distance_diff", "jockey_change", "course_top3_rate",
        "last_3f_best", "weight_trend",
        "track_cond_code", "weather_code", "is_heavy_track",
        "horse_wet_win_rate", "horse_wet_top3_rate",
        "horse_age", "is_peak_age",
        "post_win_rate_course", "post_top3_rate_course",
        "age_class_top3_rate",
        "dist_win_rate", "dist_top3_rate",
    ]


def train_models(df, feature_cols, num_boost_round=500, early_stopping_rounds=50):
    """3モデルを学習"""
    import pickle

    RANK_PARAMS = {
        "objective": "lambdarank", "metric": "ndcg",
        "ndcg_eval_at": [1, 3, 5], "boosting_type": "gbdt",
        "num_leaves": 63, "learning_rate": 0.05,
        "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 5,
        "lambda_l1": 0.1, "lambda_l2": 0.1, "min_child_samples": 20,
        "verbose": -1, "n_jobs": -1, "seed": 42,
    }
    BINARY_PARAMS = {
        "objective": "binary", "metric": "binary_logloss",
        "boosting_type": "gbdt", "num_leaves": 63, "learning_rate": 0.05,
        "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 5,
        "lambda_l1": 0.1, "lambda_l2": 0.1, "min_child_samples": 20,
        "verbose": -1, "n_jobs": -1, "seed": 42,
    }

    confirmed = df[df["finish_position"] > 0].copy()
    confirmed = confirmed.sort_values("race_id")
    print(f"\n📊 学習データ: {len(confirmed)}行, {confirmed['race_id'].nunique()}レース")

    X = confirmed[feature_cols].fillna(0)
    y_top3 = confirmed["target_top3"]
    y_win = confirmed["target_win"]
    y_rank = confirmed["relevance"]

    # レースグループ
    group = confirmed.groupby("race_id").size().values

    # 時系列分割 (80% train / 20% val)
    race_ids = confirmed["race_id"].unique()
    split_idx = int(len(race_ids) * 0.8)
    train_races = set(race_ids[:split_idx])
    val_races = set(race_ids[split_idx:])

    train_mask = confirmed["race_id"].isin(train_races)
    val_mask = confirmed["race_id"].isin(val_races)

    X_train, X_val = X[train_mask], X[val_mask]
    train_group = confirmed[train_mask].groupby("race_id").size().values
    val_group = confirmed[val_mask].groupby("race_id").size().values

    print(f"  Train: {train_mask.sum()} / Val: {val_mask.sum()}")

    # === LambdaRank ===
    print("\n🔄 LambdaRank 学習中...")
    ds_rank_t = lgb.Dataset(X_train, label=y_rank[train_mask], group=train_group)
    ds_rank_v = lgb.Dataset(X_val, label=y_rank[val_mask], group=val_group, reference=ds_rank_t)
    model_rank = lgb.train(
        RANK_PARAMS, ds_rank_t, num_boost_round=num_boost_round,
        valid_sets=[ds_rank_v],
        callbacks=[lgb.early_stopping(early_stopping_rounds), lgb.log_evaluation(50)],
    )
    print(f"  Best iteration: {model_rank.best_iteration}")

    # === Binary top3 ===
    print("\n🔄 複勝モデル学習中...")
    ds_t3_t = lgb.Dataset(X_train, label=y_top3[train_mask])
    ds_t3_v = lgb.Dataset(X_val, label=y_top3[val_mask], reference=ds_t3_t)
    model_top3 = lgb.train(
        BINARY_PARAMS, ds_t3_t, num_boost_round=num_boost_round,
        valid_sets=[ds_t3_v],
        callbacks=[lgb.early_stopping(early_stopping_rounds), lgb.log_evaluation(50)],
    )

    from sklearn.metrics import roc_auc_score
    pred_t3 = model_top3.predict(X_val)
    auc_t3 = roc_auc_score(y_top3[val_mask], pred_t3)
    print(f"  AUC(複勝): {auc_t3:.4f}")

    # === Binary win ===
    print("\n🔄 単勝モデル学習中...")
    ds_w_t = lgb.Dataset(X_train, label=y_win[train_mask])
    ds_w_v = lgb.Dataset(X_val, label=y_win[val_mask], reference=ds_w_t)
    model_win = lgb.train(
        BINARY_PARAMS, ds_w_t, num_boost_round=num_boost_round,
        valid_sets=[ds_w_v],
        callbacks=[lgb.early_stopping(early_stopping_rounds), lgb.log_evaluation(50)],
    )

    pred_w = model_win.predict(X_val)
    auc_w = roc_auc_score(y_win[val_mask], pred_w)
    print(f"  AUC(単勝): {auc_w:.4f}")

    # 特徴量重要度
    importance = pd.DataFrame({
        "feature": feature_cols,
        "rank_imp": model_rank.feature_importance(importance_type="gain"),
        "top3_imp": model_top3.feature_importance(importance_type="gain"),
        "win_imp": model_win.feature_importance(importance_type="gain"),
    }).sort_values("rank_imp", ascending=False)
    print("\n📊 特徴量重要度 (Top 15):")
    print(importance.head(15).to_string(index=False))

    # 保存
    os.makedirs(MODEL_DIR, exist_ok=True)
    with open(os.path.join(MODEL_DIR, "model_rank.pkl"), "wb") as f:
        pickle.dump(model_rank, f)
    with open(os.path.join(MODEL_DIR, "model_top3.pkl"), "wb") as f:
        pickle.dump(model_top3, f)
    with open(os.path.join(MODEL_DIR, "model_win.pkl"), "wb") as f:
        pickle.dump(model_win, f)
    print(f"\n💾 モデル保存完了: {MODEL_DIR}")

    return model_rank, model_top3, model_win, auc_t3, auc_w


def run_backtest(df, models, feature_cols, year=2025):
    """バックテスト"""
    model_rank, model_top3, model_win = models
    test = df[(df["finish_position"] > 0) &
              (df["race_date"].str.startswith(str(year)))].copy()

    if test.empty:
        print(f"❌ {year}年のデータがありません")
        return

    print(f"\n📊 バックテスト ({year}年): {test['race_id'].nunique()}レース, {len(test)}出走")

    X_test = test[feature_cols].fillna(0)
    test["rank_score"] = model_rank.predict(X_test)
    test["pred_win"] = model_win.predict(X_test)
    test["pred_top3"] = model_top3.predict(X_test)

    total_bet = 0
    total_payout = 0
    bet_count = 0
    hit_count = 0
    details = {"単勝": {"bet": 0, "payout": 0, "count": 0, "hit": 0},
               "複勝": {"bet": 0, "payout": 0, "count": 0, "hit": 0}}

    for race_id, group in test.groupby("race_id"):
        group = group.sort_values("rank_score", ascending=False)

        # ランキングスコアを正規化
        rank_exp = np.exp(group["rank_score"] - group["rank_score"].max())
        group = group.copy()
        group["win_prob"] = rank_exp / rank_exp.sum()

        hc = len(group)

        for _, row in group.iterrows():
            wp = row["win_prob"]
            odds = row["odds"]

            if odds <= 0:
                continue

            # 単勝: 期待値 > 1.2 && 予測勝率 > 8%
            ev_win = wp * odds
            if ev_win >= 1.2 and wp >= 0.08:
                amount = 100
                total_bet += amount
                bet_count += 1
                details["単勝"]["bet"] += amount
                details["単勝"]["count"] += 1

                if row["finish_position"] == 1:
                    payout = amount * odds
                    total_payout += payout
                    hit_count += 1
                    details["単勝"]["payout"] += payout
                    details["単勝"]["hit"] += 1

            # 複勝: 期待値 > 1.1 && 予測top3確率 > 20%
            t3_prob = row["pred_top3"]
            place_odds = max(odds * 0.3, 1.1)
            ev_place = t3_prob * place_odds
            if ev_place >= 1.1 and t3_prob >= 0.20:
                amount = 100
                total_bet += amount
                bet_count += 1
                details["複勝"]["bet"] += amount
                details["複勝"]["count"] += 1

                if row["finish_position"] <= 3:
                    payout = amount * place_odds
                    total_payout += payout
                    hit_count += 1
                    details["複勝"]["payout"] += payout
                    details["複勝"]["hit"] += 1

    roi = (total_payout / total_bet * 100) if total_bet > 0 else 0
    profit = total_payout - total_bet

    print(f"\n{'='*50}")
    print(f"📊 バックテスト結果 ({year}年)")
    print(f"{'='*50}")
    print(f"  購入点数:  {bet_count}")
    print(f"  的中数:    {hit_count}")
    print(f"  的中率:    {hit_count/bet_count*100:.1f}%" if bet_count else "  的中率:    -")
    print(f"  総賭け金:  ¥{total_bet:,}")
    print(f"  総払戻金:  ¥{total_payout:,.0f}")
    print(f"  損益:      ¥{profit:,.0f}")
    print(f"  回収率:    {roi:.1f}%")
    print(f"{'='*50}")

    for bt, d in details.items():
        if d["count"] > 0:
            bt_roi = d["payout"] / d["bet"] * 100 if d["bet"] > 0 else 0
            bt_hr = d["hit"] / d["count"] * 100
            print(f"  [{bt}] {d['count']}点 / 的中{d['hit']}({bt_hr:.1f}%) / "
                  f"ROI {bt_roi:.1f}%")

    if roi >= 100:
        print(f"\n  🎉 プラス収支達成!")
    else:
        print(f"\n  📉 マイナス収支")


def main():
    init_db()
    t_start = time.time()

    # Step 1: データロード
    races_df, results_df, payouts_df = load_all_data()

    # race_date をresults_dfに追加（horse_historyビルド用）
    race_info = races_df.set_index("race_id")[["race_date", "venue", "distance",
                                                "surface", "track_condition",
                                                "weather", "horse_count"]].to_dict("index")

    for col in ["race_date", "venue", "distance", "surface", "track_condition", "weather", "horse_count"]:
        results_df[col] = results_df["race_id"].map(
            lambda rid, c=col: race_info.get(rid, {}).get(c, "")
        )

    # Step 2: プリコンパイル
    horse_history = build_horse_history(results_df, races_df)
    jockey_stats, trainer_stats, combo_stats = build_jockey_trainer_stats(results_df, races_df)
    si_cache = build_speed_index_cache(results_df, races_df)

    # Step 3: 特徴量一括計算
    print("\n🔧 全レースの特徴量を一括計算中...")
    t0 = time.time()
    all_rows = []
    total_races = len(races_df)

    for i, race in races_df.iterrows():
        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            speed = (i + 1) / elapsed
            remaining = (total_races - i - 1) / speed if speed > 0 else 0
            print(f"  [{i+1}/{total_races}] {elapsed:.0f}秒経過, 残り約{remaining:.0f}秒")

        race_results = results_df[results_df["race_id"] == race["race_id"]]
        if race_results.empty:
            continue

        race_dict = race.to_dict()
        results_list = race_results.to_dict("records")

        rows = compute_features_fast(
            race_dict, results_list, horse_history,
            jockey_stats, trainer_stats, combo_stats, si_cache
        )
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    df["race_date"] = df["race_id"].map(lambda rid: race_info.get(rid, {}).get("race_date", ""))

    elapsed = time.time() - t0
    print(f"  ✅ 特徴量計算完了: {len(df)}行 ({elapsed:.1f}秒)")

    # Step 4: 学習
    feature_cols = get_feature_columns()
    models = train_models(df, feature_cols)
    model_rank, model_top3, model_win, auc_t3, auc_w = models

    # Step 5: バックテスト
    run_backtest(df, (model_rank, model_top3, model_win), feature_cols, year=2025)

    total_elapsed = time.time() - t_start
    print(f"\n⏱️ 総実行時間: {total_elapsed:.0f}秒 ({total_elapsed/60:.1f}分)")


if __name__ == "__main__":
    main()
