#!/usr/bin/env python3
"""説明できる点数表モデルの学習 (#145)。

  python3 scripts/train_scorecard.py [--train-until 2025-05-10]

学習/評価は必ず race_date でソートした時系列分割にする (#141: race_id ソートは
「年+場コード+開催回+日+R」のため日付順にならず、in-sample が混入する)。
学習境界は models/scorecard.pkl に保存し、以降の検証はこれを読む。
"""
import argparse
import os
import pickle
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ml.scorecard import FEATS, NODATA, REL_BINS, label, MODEL_PATH  # noqa: E402


def build_spec(df_train):
    """絶対値の項目だけ、学習データから5分位の区切りを決める。
    相対項目はレース内順位なので区切りを学習する必要がない。"""
    spec = {}
    for col, _, _, zero_na, is_rel in FEATS:
        if is_rel:
            continue
        v = pd.to_numeric(df_train[col], errors="coerce").fillna(0.0).astype(float)
        if zero_na:
            v = v[v != 0]
        qs = np.unique(np.nanquantile(v, [0.2, 0.4, 0.6, 0.8]))
        spec[col] = [-np.inf] + list(qs) + [np.inf]
    return spec


def fit(lab_tr, y_tr, C=1.0):
    X = pd.get_dummies(lab_tr.astype(str), prefix_sep="=")
    cols = X.columns.tolist()
    m = LogisticRegression(C=C, max_iter=2000, solver="lbfgs")
    m.fit(X.values.astype(np.float64), y_tr)
    coef = pd.Series(m.coef_[0], index=cols)
    # 各項目の中で「平均的な馬」が0点になるよう正規化 → 点数が加減点として読める
    pts = {}
    for col, _, _, _, _ in FEATS:
        sub = [c for c in cols if c.startswith(col + "=")]
        if not sub:
            continue
        w = X[sub].mean().values
        base = float((coef[sub].values * w).sum() / max(w.sum(), 1e-9))
        for c in sub:
            pts[c] = float(coef[c] - base)
    return pts, cols


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-until", default=None,
                    help="この日までを学習に使う (既定: 全データの時系列80%%)")
    ap.add_argument("--out", default=MODEL_PATH)
    ap.add_argument("--cache", default=None,
                    help="特徴量テーブルの pickle (構築を省略して再利用)")
    args = ap.parse_args()

    if args.cache and os.path.exists(args.cache):
        # 特徴量テーブルの構築は約2時間かかる。同じ内容の pickle があれば再利用する。
        # ※ 特徴量定義を変えたらキャッシュは無効。--cache を外して作り直すこと。
        print(f"📦 キャッシュから読込: {args.cache}")
        df = pd.read_pickle(args.cache)
    else:
        from fast_train import build_feature_table
        print("📊 特徴量テーブルを構築中...")
        df = build_feature_table()
    df = df[df["finish_position"] > 0].copy()
    df = df.sort_values(["race_date", "race_id"]).reset_index(drop=True)   # #141
    for col, _, _, _, _ in FEATS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    if "target_top3" not in df.columns:
        df["target_top3"] = (df["finish_position"].between(1, 3)).astype(int)

    if args.train_until:
        cut = args.train_until
    else:
        rids = df[["race_id", "race_date"]].drop_duplicates().sort_values(["race_date", "race_id"])
        cut = str(rids.iloc[int(len(rids) * 0.8) - 1]["race_date"])[:10]
    tr = df[df["race_date"] <= cut]
    ev = df[df["race_date"] > cut]
    print(f"📅 学習 〜{cut} ({tr['race_id'].nunique()}R) / 評価 {cut}以降 ({ev['race_id'].nunique()}R)")
    assert str(tr["race_date"].max())[:10] <= cut < str(ev["race_date"].min())[:10], "時系列分割が壊れている"

    spec = build_spec(tr)
    lab_all = label(df, spec)
    pts, cols = fit(lab_all.loc[tr.index], tr["target_top3"].values)

    model = {"pts": pts, "cols": cols, "spec": spec,
             "train_until": cut,
             "train_races": int(tr["race_id"].nunique()),
             "eval_races": int(ev["race_id"].nunique())}

    # 評価 (学習に使っていない期間だけ)
    from ml.scorecard import score_with_reasons
    sc_ev, _ = score_with_reasons(ev, model)
    ev = ev.assign(_sc=sc_ev)
    auc = roc_auc_score(ev["target_top3"], ev["_sc"])
    r = ev.groupby("race_id")["_sc"].rank(ascending=False, method="first")
    top1 = ev[r == 1]
    cap5 = ev[r <= 5].groupby("race_id")["target_top3"].sum()
    print(f"\n【評価 (学習外 {ev['race_id'].nunique()}レース)】")
    print(f"  1位馬の3着内率 : {top1['target_top3'].mean()*100:.1f}%")
    # #146: build_feature_table() の出力に popularity が無い経路があり、
    # ここで KeyError になって**保存の直前で落ちていた** (キャッシュ経由では通るため
    # 気づきにくい)。表示だけの項目なので、無ければ黙って飛ばす。
    if "popularity" in top1.columns:
        _p = pd.to_numeric(top1["popularity"], errors="coerce").replace(0, np.nan).mean()
        print(f"  1位馬の平均人気 : {_p:.1f}")
    print(f"  上位5頭の捕捉   : {cap5.mean():.2f}頭 / 完全捕捉 {(cap5>=3).mean()*100:.1f}%")
    print(f"  AUC            : {auc:.4f}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "wb") as f:
        pickle.dump(model, f)
    print(f"\n💾 {args.out} に保存 (係数 {len(pts)}個)")


if __name__ == "__main__":
    main()
