"""能力モデル (odds 非依存) の学習 + walk-forward 評価 — #57 / C-1 value betting 路線

背景:
    本番の model_rank は gain の 92% が odds_log = 実質「市場オッズ追従モデル」。
    これは印・表示には有効 (#6: 市場シグナル削除で ROI -7pt の教訓) だが、
    市場と同じ予想をしている限り控除率 (約20%) を超えられない。

設計 (プロの value betting 手法):
    1. 市場特徴量 (odds_log / popularity_norm) を除いた「馬の能力モデル」を学習
    2. 能力ベース勝率 p_model と市場オッズ由来の暗黙勝率 p_market を比較
    3. 乖離の大きい馬 = 「市場が見落としている馬」を検出できるか walk-forward で検証

v1 の結果 (2026-06-10 初回実測):
    素の edge (p_model/p_market) は閾値を上げるほど ROI が悪化 (72%→66%)。
    原因は大穴バイアス: 能力モデルは市場情報が無いぶん確率が flat になり、
    edge 上位が平均オッズ 87-150倍の大穴に吸い寄せられる。大穴は構造的に
    過剰人気 (favorite-longshot bias) なので素の edge は逆指標になる。
    ただしオッズ帯で制御すると信号は本物: edge>=1.5 × 3-10倍 ROI 90.1% /
    10-30倍 86.0% (ベースライン 73.3% に対し +13-17pt、単日支配なし)。

v2 (本版) の改良:
    A. 特徴量テーブルの pickle キャッシュ (30分 → 数秒で再イテレーション)
    B. EV ベース選別: EV = p_model × odds をオッズ帯制限付きで閾値スイープ
    C. 2段スタッキング: [p_model, p_market, log_odds, 頭数] から第2モデルで
       勝率を再推定 — 「能力情報が市場を上回る局面」をモデル自身に学習させる

教訓の遵守:
    - #4: temporal split (評価期間のデータは学習に一切使わない)
    - #26: 単日支配チェック (最良日除外 ROI を併記)
    - #29: 年単位の評価 (2025 / 2026 を分けて報告)
    - 本番 predict.py への組込みは本スクリプトの結果を見て別途判断

使い方:
    python3 scripts/train_ability_model.py             # 学習+評価+保存
    python3 scripts/train_ability_model.py --no-save   # 評価のみ
    (特徴量キャッシュ: /tmp/ability_feature_table.pkl を自動利用。
     DB 更新後にやり直す場合は --rebuild-cache)
"""
import argparse
import json
import os
import pickle
import sys
import time

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fast_train import build_feature_table, get_feature_columns  # noqa: E402

MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "models")
REPORT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "docs", "analysis", "ability_model_eval.json")
CACHE_PATH = "/tmp/ability_feature_table.pkl"

EVAL_START = "2025-01-01"  # これ以降が out-of-sample 評価期間
MARKET_COLS = ("odds_log", "popularity_norm")

BINARY_PARAMS = {
    "objective": "binary", "metric": "binary_logloss",
    "boosting_type": "gbdt", "num_leaves": 63, "learning_rate": 0.05,
    "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 5,
    "lambda_l1": 0.1, "lambda_l2": 0.1, "min_child_samples": 20,
    "verbose": -1, "n_jobs": -1, "seed": 42,
}


def ability_feature_columns():
    return [c for c in get_feature_columns() if c not in MARKET_COLS]


def load_feature_table(rebuild=False):
    """特徴量テーブル (キャッシュ優先)。30分かかる build を pickle で再利用する。"""
    if not rebuild and os.path.exists(CACHE_PATH):
        print(f"📦 特徴量キャッシュ利用: {CACHE_PATH}")
        return pd.read_pickle(CACHE_PATH)
    df = build_feature_table()
    df.to_pickle(CACHE_PATH)
    print(f"💾 特徴量キャッシュ保存: {CACHE_PATH}")
    return df


def split_races(train_df, fractions=(0.8, 0.1, 0.1)):
    """レース単位の時系列 3 分割 (学習 / early-stop val / stacking 用)。"""
    race_ids = train_df["race_id"].unique()
    n = len(race_ids)
    a = int(n * fractions[0])
    b = int(n * (fractions[0] + fractions[1]))
    s1, s2, s3 = set(race_ids[:a]), set(race_ids[a:b]), set(race_ids[b:])
    return (train_df[train_df["race_id"].isin(s1)],
            train_df[train_df["race_id"].isin(s2)],
            train_df[train_df["race_id"].isin(s3)])


def train_win_model(tr, va, feature_cols):
    """単勝 binary モデルを学習 (va で early stopping + isotonic 較正)。"""
    X_tr = tr[feature_cols].fillna(0)
    X_va = va[feature_cols].fillna(0)
    ds_t = lgb.Dataset(X_tr, label=tr["target_win"])
    ds_v = lgb.Dataset(X_va, label=va["target_win"], reference=ds_t)
    model = lgb.train(
        BINARY_PARAMS, ds_t, num_boost_round=500,
        valid_sets=[ds_v],
        callbacks=[lgb.early_stopping(50), lgb.log_evaluation(200)],
    )

    from sklearn.isotonic import IsotonicRegression
    raw_va = model.predict(X_va, num_iteration=model.best_iteration)
    calib = IsotonicRegression(out_of_bounds="clip")
    calib.fit(raw_va, va["target_win"].values)

    try:
        from sklearn.metrics import roc_auc_score
        auc = roc_auc_score(va["target_win"], raw_va)
    except Exception:
        auc = None
    return model, calib, auc


def add_market_cols(df, model, calib, feature_cols):
    """p_model (レース内正規化済) / p_market / edge 列を付与して返す。"""
    out = df[df["odds"] > 1.0].copy()
    X = out[feature_cols].fillna(0)
    raw = model.predict(X, num_iteration=model.best_iteration)
    out["p_raw"] = calib.predict(raw)
    out["p_model"] = out.groupby("race_id")["p_raw"].transform(
        lambda s: s / s.sum() if s.sum() > 0 else s)
    out["imp"] = 1.0 / out["odds"]
    out["p_market"] = out.groupby("race_id")["imp"].transform(lambda s: s / s.sum())
    out["edge"] = out["p_model"] / out["p_market"]
    out["year"] = out["race_date"].str[:4]
    out["hit"] = (out["finish_position"] == 1).astype(int)
    return out


def roi_block(sub):
    n = len(sub)
    if n == 0:
        return {"bets": 0}
    spend = n * 100
    ret = float((sub["hit"] * sub["odds"] * 100).sum())
    daily = sub.assign(r=sub["hit"] * sub["odds"] * 100).groupby("race_date").agg(
        spend=("hit", "size"), ret=("r", "sum"))
    daily["spend"] *= 100
    excl_roi = None
    top_share = None
    if len(daily) > 1 and ret > 0:
        top_day = daily["ret"].idxmax()
        top_share = round(float(daily.loc[top_day, "ret"]) / ret * 100, 1)
        d2 = daily.drop(index=top_day)
        excl_roi = round(float(d2["ret"].sum()) / float(d2["spend"].sum()) * 100, 1)
    return {
        "bets": n, "hits": int(sub["hit"].sum()),
        "hit_pct": round(float(sub["hit"].mean()) * 100, 1),
        "roi_pct": round(ret / spend * 100, 1),
        "avg_odds": round(float(sub["odds"].mean()), 1),
        "best_day_return_share_pct": top_share,
        "roi_excl_best_day_pct": excl_roi,
    }


def yearly(sub):
    return {y: roi_block(g) for y, g in sub.groupby("year")}


def evaluate_all(eval_m, stack_model=None):
    """eval_m: add_market_cols 済みの評価期間 DataFrame。"""
    report = {"baselines": {}, "raw_edge": {}, "ev_band": {}, "stacked": {}}

    report["baselines"]["bet_all"] = roi_block(eval_m)
    fav_idx = eval_m.groupby("race_id")["odds"].idxmin()
    report["baselines"]["bet_favorite"] = roi_block(eval_m.loc[fav_idx])

    # ── v1: 素の edge (記録として保持) ──
    for t in (1.5, 2.0):
        report["raw_edge"][f"edge>={t}"] = roi_block(eval_m[eval_m["edge"] >= t])

    # ── v2-B: EV ベース + オッズ帯制限 ──
    eval_m = eval_m.copy()
    eval_m["ev"] = eval_m["p_model"] * eval_m["odds"]
    for lo, hi, label in ((2, 10, "odds2-10"), (3, 15, "odds3-15"), (2, 30, "odds2-30")):
        band = eval_m[(eval_m["odds"] >= lo) & (eval_m["odds"] < hi)]
        for t in (1.0, 1.2, 1.5):
            sub = band[band["ev"] >= t]
            key = f"{label}_ev>={t}"
            blk = roi_block(sub)
            if blk["bets"] > 0:
                blk["by_year"] = yearly(sub)
            report["ev_band"][key] = blk

    # ── v2-C: stacked 2段モデル ──
    if stack_model is not None:
        Xs = eval_m[["p_model", "p_market", "odds_log_s", "horse_count"]].fillna(0)
        eval_m["p2"] = stack_model.predict_proba(Xs)[:, 1]
        eval_m["ev2"] = eval_m["p2"] * eval_m["odds"]
        for lo, hi, label in ((2, 10, "odds2-10"), (3, 15, "odds3-15"), (2, 30, "odds2-30"), (1, 9999, "all")):
            band = eval_m[(eval_m["odds"] >= lo) & (eval_m["odds"] < hi)]
            for t in (1.0, 1.1, 1.2, 1.4):
                sub = band[band["ev2"] >= t]
                key = f"{label}_ev2>={t}"
                blk = roi_block(sub)
                if blk["bets"] > 0:
                    blk["by_year"] = yearly(sub)
                report["stacked"][key] = blk

    return report


def train_stack_model(stack_m):
    """第2段: [p_model, p_market, log_odds, 頭数] → 勝率。

    能力評価が市場に対してどの局面で情報を持つかをロジスティック回帰で学習。
    (シンプルに保つ — 過学習させない)
    """
    from sklearn.linear_model import LogisticRegression
    X = stack_m[["p_model", "p_market", "odds_log_s", "horse_count"]].fillna(0)
    y = stack_m["target_win"]
    m = LogisticRegression(max_iter=1000)
    m.fit(X, y)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true", help="モデルを保存しない (評価のみ)")
    ap.add_argument("--rebuild-cache", action="store_true", help="特徴量キャッシュを作り直す")
    args = ap.parse_args()

    t0 = time.time()
    feature_cols = ability_feature_columns()
    print(f"🧠 能力モデル学習: 特徴量 {len(feature_cols)} 個 "
          f"(市場特徴量 {list(MARKET_COLS)} を除外)")

    df = load_feature_table(rebuild=args.rebuild_cache)
    confirmed = df[df["finish_position"] > 0].copy()
    confirmed = confirmed.sort_values(["race_date", "race_id"])

    train_df = confirmed[confirmed["race_date"] < EVAL_START]
    eval_df = confirmed[confirmed["race_date"] >= EVAL_START]
    print(f"📊 train: {train_df['race_id'].nunique()}R ({train_df['race_date'].min()}〜) / "
          f"eval: {eval_df['race_id'].nunique()}R ({EVAL_START}〜{eval_df['race_date'].max()})")

    tr, va, st = split_races(train_df)
    model, calib, auc = train_win_model(tr, va, feature_cols)
    print(f"✅ 学習完了 AUC(val)={auc:.4f}" if auc else "✅ 学習完了")

    imp_pairs = list(zip(model.feature_name(),
                         model.feature_importance(importance_type="gain")))
    tot = sum(g for _, g in imp_pairs) or 1
    importance_top = [{"feature": n, "gain_pct": round(g / tot * 100, 1)}
                      for n, g in sorted(imp_pairs, key=lambda x: -x[1])[:10]]
    print("📈 gain top10:", ", ".join(f"{d['feature']}={d['gain_pct']}%" for d in importance_top[:5]), "...")

    # stacking 用スライス (学習に未使用の st) と評価期間に市場列を付与
    stack_m = add_market_cols(st, model, calib, feature_cols)
    eval_m = add_market_cols(eval_df, model, calib, feature_cols)
    for d in (stack_m, eval_m):
        d["odds_log_s"] = np.log(d["odds"].clip(lower=1.01))

    stack_model = train_stack_model(stack_m)
    coef = dict(zip(["p_model", "p_market", "odds_log_s", "horse_count"],
                    [round(float(c), 3) for c in stack_model.coef_[0]]))
    print(f"🔗 stacked 係数: {coef}")

    report = evaluate_all(eval_m, stack_model=stack_model)
    report["meta"] = {
        "eval_start": EVAL_START,
        "train_races": int(train_df["race_id"].nunique()),
        "eval_races": int(eval_df["race_id"].nunique()),
        "auc_val": round(auc, 4) if auc else None,
        "n_features": len(feature_cols),
        "importance_top10": importance_top,
        "stack_coef": coef,
    }

    print("\n━━━ walk-forward 評価 (単勝, 100円/点) ━━━")
    print(f"  baseline 全馬: {report['baselines']['bet_all']}")
    print(f"  baseline 1人気: {report['baselines']['bet_favorite']}")
    print("  -- 素の edge (v1, 記録) --")
    for k, v in report["raw_edge"].items():
        print(f"  {k}: {v}")
    print("  -- EV×オッズ帯 (v2-B) --")
    for k, v in report["ev_band"].items():
        if v["bets"] > 0:
            print(f"  {k}: bets={v['bets']} hit={v['hit_pct']}% ROI={v['roi_pct']}% "
                  f"(除最良日 {v.get('roi_excl_best_day_pct')}%)")
    print("  -- stacked 2段 (v2-C) --")
    for k, v in report["stacked"].items():
        if v["bets"] > 0:
            print(f"  {k}: bets={v['bets']} hit={v['hit_pct']}% ROI={v['roi_pct']}% "
                  f"(除最良日 {v.get('roi_excl_best_day_pct')}%)")

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"📄 レポート保存: {REPORT_PATH}")

    if not args.no_save:
        print("\n🔁 全期間で再学習して保存...")
        tr_f, va_f, _ = split_races(confirmed, fractions=(0.9, 0.1, 0.0))
        model_full, calib_full, _ = train_win_model(tr_f, va_f, feature_cols)
        with open(os.path.join(MODEL_DIR, "model_ability_win.pkl"), "wb") as f:
            pickle.dump(model_full, f)
        with open(os.path.join(MODEL_DIR, "calibrator_ability_win.pkl"), "wb") as f:
            pickle.dump(calib_full, f)
        print("💾 models/model_ability_win.pkl + calibrator_ability_win.pkl 保存")

    print(f"\n⏱️ 総実行時間: {(time.time()-t0)/60:.1f}分")


if __name__ == "__main__":
    main()
