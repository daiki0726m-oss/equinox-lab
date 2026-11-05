#!/usr/bin/env python3
"""#169 OOS A/B: LGB の si_* の馬場補正表 legacy (稍・不は補正なし) vs canonical (稍-1.5/不-5.0)。

  python3 scripts/ab_si_track_adj.py OUT_DIR [DB]
  (OUT_DIR に特徴量表のキャッシュと ab_result.json を書く。models/ には何も書かない。
   特徴量表の作成に1時間前後かかる。結果は analyzers/speed_index.py の #169 の注記)

学習: race_date < B (scripts/oos_window.get_oos_start(include_scorecard=False))。その中を時系列 80/20 で
      early stopping 用に分ける (fast_train.train_models と同じ手順・同じパラメータ)。
評価: race_date >= B の確定レース。レース単位 bootstrap で (canonical - legacy) の差の区間を出す。
seed を 2 つ回して seed 間のブレ (ノイズ床) と比べる。モデルは保存しない (models/ を触らない)。
"""
import json
import os
import pickle
import sys
import time

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, WT)
sys.path.insert(0, os.path.join(WT, "scripts"))
os.chdir(WT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import lightgbm as lgb  # noqa: E402
from sklearn.metrics import roc_auc_score, log_loss  # noqa: E402

import database  # noqa: E402
if len(sys.argv) > 2:
    database.DB_PATH = sys.argv[2]      # 収集テストと競合しないよう DB の別コピーを読む
import fast_train as ft  # noqa: E402
from analyzers.speed_index import si_track_adjust  # noqa: E402
import oos_window  # noqa: E402

SI_COLS = ["si_avg", "si_max", "si_min", "si_std", "si_latest", "si_count"]
t0 = time.time()

feat_pkl = os.path.join(OUT, "feat_legacy.pkl")
if os.path.exists(feat_pkl):
    df = pd.read_pickle(feat_pkl)
    print(f"[cache] feature table {len(df)} rows", flush=True)
else:
    df = ft.build_feature_table()
    df.to_pickle(feat_pkl)
    print(f"[built] feature table {len(df)} rows ({time.time()-t0:.0f}s)", flush=True)

canon_pkl = os.path.join(OUT, "si_canonical.pkl")
if os.path.exists(canon_pkl):
    can = pd.read_pickle(canon_pkl)
else:
    races_df, results_df, _ = ft.load_all_data()
    info = races_df.set_index("race_id")[["race_date", "venue", "distance", "surface",
                                           "track_condition", "weather", "horse_count"]].to_dict("index")
    for col in ["race_date", "venue", "distance", "surface", "track_condition", "weather", "horse_count"]:
        results_df[col] = results_df["race_id"].map(lambda rid, c=col: info.get(rid, {}).get(c, ""))
    si_cache = ft.build_speed_index_cache(results_df, races_df)
    adj_c = si_track_adjust("canonical")
    adj_l = si_track_adjust("legacy")
    recs = []
    n_wet = 0
    for rid, hid, rd in zip(df["race_id"], df["horse_id"], df["race_date"]):
        runs = [s for s in si_cache.get(hid, []) if s["race_date"] < rd][:5] if hid in si_cache else []
        lc = [si_cache.si(s, rd, adj_c) for s in runs]
        ll = [si_cache.si(s, rd, adj_l) for s in runs]
        if any(s["tc"] in ("稍", "不") for s in runs):
            n_wet += 1
        def agg(x):
            return (np.mean(x) if x else 0, max(x) if x else 0, min(x) if x else 0,
                    np.std(x) if len(x) > 1 else 0, x[0] if x else 0, len(x))
        recs.append(agg(lc) + agg(ll))
    can = pd.DataFrame(recs, columns=[c + "_c" for c in SI_COLS] + [c + "_l" for c in SI_COLS],
                       index=df.index)
    can.attrs["n_rows_with_wet_run"] = n_wet
    can.to_pickle(canon_pkl)
    print(f"[built] canonical si ({time.time()-t0:.0f}s) rows with 稍/不 in last5: {n_wet}/{len(df)}",
          flush=True)

# 再計算した legacy が特徴量表の si_* と一致するか (再計算の正しさの検算)
mism = {c: int((np.abs(df[c].astype(float) - can[c + "_l"].astype(float)) > 1e-6).sum()) for c in SI_COLS}
print("legacy 再計算との不一致行:", mism, flush=True)
diff_rows = int((np.abs(df["si_avg"].astype(float) - can["si_avg_c"].astype(float)) > 1e-9).sum())
print(f"canonical で si_avg が変わる行: {diff_rows}/{len(df)}", flush=True)

B = oos_window.get_oos_start(include_scorecard=False)
feature_cols = ft.get_feature_columns()
conf = df[df["finish_position"] > 0].copy()
conf = conf.sort_values(["race_date", "race_id"])
tr = conf[conf["race_date"] < B]
te = conf[conf["race_date"] >= B]
print(f"B={B} train races={tr.race_id.nunique()} eval races={te.race_id.nunique()} "
      f"eval dates {te.race_date.min()}..{te.race_date.max()}", flush=True)

RANK_PARAMS = {"objective": "lambdarank", "metric": "ndcg", "ndcg_eval_at": [1, 3, 5],
               "boosting_type": "gbdt", "num_leaves": 63, "learning_rate": 0.05,
               "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 5,
               "lambda_l1": 0.1, "lambda_l2": 0.1, "min_child_samples": 20,
               "verbose": -1, "n_jobs": -1}
BINARY_PARAMS = {"objective": "binary", "metric": "binary_logloss", "boosting_type": "gbdt",
                 "num_leaves": 63, "learning_rate": 0.05, "feature_fraction": 0.8,
                 "bagging_fraction": 0.8, "bagging_freq": 5, "lambda_l1": 0.1, "lambda_l2": 0.1,
                 "min_child_samples": 20, "verbose": -1, "n_jobs": -1}


def X_of(d, arm):
    X = d[feature_cols].fillna(0).copy()
    if arm == "canonical":
        for c in SI_COLS:
            X[c] = can.loc[d.index, c + "_c"].values
    return X


def train(arm, seed):
    rids = tr["race_id"].unique()
    k = int(len(rids) * 0.8)
    tm = tr["race_id"].isin(set(rids[:k]))
    vm = ~tm
    X = X_of(tr, arm)
    Xt, Xv = X[tm], X[vm]
    gt = tr[tm].groupby("race_id", sort=False).size().values
    gv = tr[vm].groupby("race_id", sort=False).size().values
    rp = dict(RANK_PARAMS, seed=seed)
    bp = dict(BINARY_PARAMS, seed=seed)
    dr = lgb.Dataset(Xt, label=tr.loc[tm, "relevance"], group=gt)
    drv = lgb.Dataset(Xv, label=tr.loc[vm, "relevance"], group=gv, reference=dr)
    m_rank = lgb.train(rp, dr, 500, valid_sets=[drv],
                       callbacks=[lgb.early_stopping(50, first_metric_only=True, verbose=False)])
    if m_rank.num_trees() < 20:
        m_rank = lgb.train(rp, dr, 250, valid_sets=[drv])
    out = {"rank": m_rank}
    for name, tgt in (("top3", "target_top3"), ("win", "target_win")):
        d1 = lgb.Dataset(Xt, label=tr.loc[tm, tgt])
        d2 = lgb.Dataset(Xv, label=tr.loc[vm, tgt], reference=d1)
        out[name] = lgb.train(bp, d1, 500, valid_sets=[d2],
                              callbacks=[lgb.early_stopping(50, verbose=False)])
    return out


te_rid = te["race_id"].values
y3 = te["target_top3"].values
yw = te["target_win"].values
races = pd.Index(pd.unique(te_rid))
race_pos = {r: np.where(te_rid == r)[0] for r in races}


def per_race_metrics(p3, pw, pr):
    """レースごとの値: 複勝モデル1位の3着内 / 上位5頭の捕捉数 / 単勝モデル1位の勝ち / rank1位の勝ち"""
    rows = []
    for r in races:
        ix = race_pos[r]
        o3 = ix[np.argsort(-p3[ix], kind="mergesort")]
        ow = ix[np.argmax(pw[ix])]
        orr = ix[np.argmax(pr[ix])]
        rows.append((y3[o3[0]], y3[o3[:5]].sum(), yw[ow], yw[orr]))
    return np.array(rows, dtype=float)


res = {"B": B, "eval_races": int(len(races)), "eval_rows": int(len(te)), "arms": {}}
preds = {}
for seed in (42, 7):
    for arm in ("legacy", "canonical"):
        t1 = time.time()
        ms = train(arm, seed)
        X = X_of(te, arm)
        p3, pw, pr = ms["top3"].predict(X), ms["win"].predict(X), ms["rank"].predict(X)
        preds[(arm, seed)] = (p3, pw, pr)
        pm = per_race_metrics(p3, pw, pr)
        r = {"auc_top3": roc_auc_score(y3, p3), "auc_win": roc_auc_score(yw, pw),
             "logloss_top3": log_loss(y3, np.clip(p3, 1e-6, 1 - 1e-6)),
             "top1_top3_rate": pm[:, 0].mean(), "top5_capture": pm[:, 1].mean(),
             "winmodel_top1_win": pm[:, 2].mean(), "rank_top1_win": pm[:, 3].mean(),
             "trees": {k: int(v.num_trees()) for k, v in ms.items()}}
        res["arms"][f"{arm}_s{seed}"] = r
        print(arm, seed, {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()},
              f"({time.time()-t1:.0f}s)", flush=True)

# レース単位 bootstrap (canonical - legacy、同じ seed で対応させる)
rng = np.random.default_rng(169)
B_N = 500
boot = {}
for seed in (42, 7):
    pl, pc = preds[("legacy", seed)], preds[("canonical", seed)]
    ml, mc = per_race_metrics(*pl), per_race_metrics(*pc)
    d_race = mc - ml          # レースごとの差 (top1_top3, top5_capture, win top1, rank top1)
    auc_d = []
    m_d = []
    for _ in range(B_N):
        samp = rng.integers(0, len(races), len(races))
        ix = np.concatenate([race_pos[races[i]] for i in samp])
        auc_d.append((roc_auc_score(y3[ix], pc[0][ix]) - roc_auc_score(y3[ix], pl[0][ix]),
                      roc_auc_score(yw[ix], pc[1][ix]) - roc_auc_score(yw[ix], pl[1][ix])))
        m_d.append(d_race[samp].mean(axis=0))
    auc_d, m_d = np.array(auc_d), np.array(m_d)
    names_a = ["auc_top3", "auc_win"]
    names_m = ["top1_top3_rate", "top5_capture", "winmodel_top1_win", "rank_top1_win"]
    bs = {}
    for j, n in enumerate(names_a):
        pt = res["arms"][f"canonical_s{seed}"][n] - res["arms"][f"legacy_s{seed}"][n]
        bs[n] = {"diff": pt, "ci95": [float(np.percentile(auc_d[:, j], 2.5)),
                                      float(np.percentile(auc_d[:, j], 97.5))]}
    for j, n in enumerate(names_m):
        bs[n] = {"diff": float(d_race[:, j].mean()),
                 "ci95": [float(np.percentile(m_d[:, j], 2.5)), float(np.percentile(m_d[:, j], 97.5))]}
    boot[f"s{seed}"] = bs
res["bootstrap_canonical_minus_legacy"] = boot
# seed 間のブレ (legacy 同士) = ノイズ床
res["seed_noise_legacy"] = {k: res["arms"]["legacy_s42"][k] - res["arms"]["legacy_s7"][k]
                            for k in ["auc_top3", "auc_win", "top1_top3_rate", "top5_capture",
                                      "winmodel_top1_win", "rank_top1_win"]}
res["si_rows_changed"] = diff_rows
res["legacy_recompute_mismatch"] = mism
res["elapsed_s"] = time.time() - t0
json.dump(res, open(os.path.join(OUT, "ab_result.json"), "w"), ensure_ascii=False, indent=1,
          default=float)
print(json.dumps(res, ensure_ascii=False, indent=1, default=float), flush=True)
