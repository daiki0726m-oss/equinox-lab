#!/usr/bin/env python3
"""説明できる点数表モデルの学習 (#145)。

  python3 scripts/train_scorecard.py [--train-until 2025-05-10]

学習/評価は必ず race_date でソートした時系列分割にする (#141: race_id ソートは
「年+場コード+開催回+日+R」のため日付順にならず、in-sample が混入する)。
学習境界は models/scorecard.pkl に保存し、以降の検証はこれを読む。

## --mode lin (v2: 連続項目の当てはめ)
既存の区分点 (--base の pkl の pts/spec) は**そのまま**使い、
  - --drop の区分項目 (既定: 近5走の平均着順・自己ベスト着差) を外し
  - 連続項目 (ml/scorecard.LIN_FEATS: 格を考慮した近5走の着順 / 勝ち馬とのタイム差) の
    レース内 z × 係数と、JRA初出走 (has_past_runs=0) の減点を足す
係数は「残した区分点の合計・各 z・初出走」の4つを説明変数にした
**レース内ソフトマックス (目標 = 3着内なら 1/3)** で当てはめ、区分点の係数 (全体の傾き) で
割って区分点と同じ単位に揃える (= 点数のスケール ×10 は従来どおり。傾きで割っても
レース内の順位は当てはめた式と同じ)。

  python3 scripts/train_scorecard.py --mode lin --cache feat.pkl  (以下1行に続けて)
      --lin-fit-from 2025-05-11 --lin-fit-until 2025-12-31
      --eval-from 2026-01-01 --eval-until 2026-10-18 --out /tmp/sc.pkl

当てはめ期間は --base の train_until より後にすること (区分点の学習期間と重なると
傾きが in-sample で膨らむ)。モデルに lin_fit_from / lin_fit_until を記録し、
scripts/oos_window.py はこの境界も見て評価窓を決める (#157)。

## --mode f2 (v3, #168: 馬場差補正スピード指数 V2d + 芝ダ替わり)
二段で当てはめる:
  窓1 (--pts-fit-from〜--pts-fit-until): 区分点 (pts) をロジスティック回帰で学習し直す。
      --col-map で区分項目の値を読む列を差し替える (既定 si_avg→si_avg_v, si_latest→si_latest_v)。
      点数のキーは "si_avg=1位" のまま (ml/scorecard の後方互換)。--drop の項目は外す。
  窓2 (--lin-fit-from〜--lin-fit-until): 連続項目 (form_cls5/tb_min5) と JRA初出走と
      芝ダ替わり (ml/scorecard.FLAG_FEATS) をレース内ソフトマックスで当てはめる。
      芝ダ替わりは事前に決めた3候補 a=初替わり(0/1) / b=近5走の他馬場の割合 / c=a+b から
      **窓2 の中の対数尤度で** 選ぶ (--sf-select aic=対数尤度−係数の数 が既定。ll にすると
      c が a,b を含むので必ず c になる)。評価期間は選択に使わない。
  特徴量テーブルは si_avg_v 等を持つこと (fast_train.build_feature_table か
  scripts/build_si_variant.py --augment-cache で作る)。テーブルを作った基準表の指紋
  (df.attrs["si_variant_table_hash"]) が --si-table と違えば止まる。
  モデルに pts_fit_from/until・lin_fit_from/until・si_variant (基準表の指紋・カットオフ・c) を
  記録し、scripts/oos_window.py はその最も遅い日を境界にする。

  区分点のロジスティック回帰は収束させる (LR_TOL=1e-8、収束しなければ例外。#168 レビューで
  既定 tol=1e-4 の未収束が判明 — 区分点が最大約1.4点ずれ、芝ダ替わりの判定が逆転していた)。

  # 2026-10 の出荷候補 (事前登録の判定で芝ダ替わりは入れない = --sf-candidates none)
  python3 scripts/train_scorecard.py --mode f2 --cache feat_sv.pkl \
      --pts-fit-from 2020-01-01 --pts-fit-until 2025-05-10 \
      --lin-fit-from 2025-05-11 --lin-fit-until 2026-10-25 \
      --sf-candidates none --out models/scorecard.pkl
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


def build_spec(df_train, col_map=None):
    """絶対値の項目だけ、学習データから5分位の区切りを決める。
    相対項目はレース内順位なので区切りを学習する必要がない。
    col_map: 項目の値を読む列の差し替え (model["col_map"])。"""
    spec = {}
    for col, _, _, zero_na, is_rel in FEATS:
        if is_rel:
            continue
        v = pd.to_numeric(df_train[(col_map or {}).get(col, col)], errors="coerce").fillna(0.0).astype(float)
        if zero_na:
            v = v[v != 0]
        qs = np.unique(np.nanquantile(v, [0.2, 0.4, 0.6, 0.8]))
        spec[col] = [-np.inf] + list(qs) + [np.inf]
    return spec


# 区分点のロジスティック回帰の収束条件 (#168 レビュー)。sklearn の既定 tol=1e-4 は約30反復で
# 止まり、出荷の窓1 (17,793R) で目的関数が最適より +1.38 高いまま・係数が最大 0.15 (区分点で
# 最大約1.4点) ずれていた。tol=1e-8 なら tol=1e-10 / newton-cg と同じ解 (約400反復、窓1で約20秒)。
# 収束しなければ例外にする (黙って途中の値を出荷しない)。
LR_TOL = 1e-8
LR_MAX_ITER = 10000


def fit(lab_tr, y_tr, C=1.0, tol=LR_TOL, max_iter=LR_MAX_ITER, info=None):
    """区分のダミーのロジスティック回帰。info (dict) を渡すと solver・tol・反復数を書き込む。"""
    import warnings
    from sklearn.exceptions import ConvergenceWarning
    X = pd.get_dummies(lab_tr.astype(str), prefix_sep="=")
    cols = X.columns.tolist()
    m = LogisticRegression(C=C, max_iter=max_iter, tol=tol, solver="lbfgs")
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        try:
            m.fit(X.values.astype(np.float64), y_tr)
        except ConvergenceWarning as e:
            raise RuntimeError(f"区分点のロジスティック回帰が収束しない (tol={tol}, max_iter={max_iter}): {e}")
    if info is not None:
        info.update({"solver": "lbfgs", "C": float(C), "tol": float(tol), "max_iter": int(max_iter),
                     "n_iter": int(np.max(m.n_iter_)), "rows": int(len(y_tr))})
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


# ══════════════════════════════════════════════════════════════════════
# --mode lin
# ══════════════════════════════════════════════════════════════════════
MIN_RUNNERS = 6      # 5頭以下は「印5頭で全部捕捉」が自明なので当てはめ・評価から除く
DEFAULT_DROP = ("avg_finish_5r", "margin_best")


def fit_race_softmax(X, y, race_ids, l2=0.0, return_ll=False):
    """レース内ソフトマックス (目標 = 3着内なら 1/3、同着で4頭なら 1/4) の係数。
    「レースの中で誰が来るか」だけを見るので、レース間の水準差の影響を受けない。
    X: (n, k) — 単位はそのまま (×10 しない)。戻り値: 係数 (k,)。
    return_ll=True なら (係数, 当てはめた対数尤度) を返す (l2=0 の時の対数尤度)。"""
    from scipy.optimize import minimize
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    rid = pd.factorize(pd.Series(race_ids))[0]
    ny = np.bincount(rid, weights=y)
    t = y / np.where(ny[rid] > 0, ny[rid], 1)
    keep = ny[rid] > 0
    X, t = X[keep], t[keep]
    rid2 = pd.factorize(pd.Series(rid[keep]))[0]

    def f(b):
        s = X @ b
        mx = np.zeros(rid2.max() + 1)
        np.maximum.at(mx, rid2, s)
        e = np.exp(s - mx[rid2])
        Z = np.bincount(rid2, weights=e)
        p = e / Z[rid2]
        ll = (t * (s - mx[rid2] - np.log(Z[rid2]))).sum()
        tr = np.bincount(rid2, weights=t)
        g = X.T @ t - X.T @ (p * tr[rid2])
        return -ll + l2 * (b @ b), -g + 2 * l2 * b

    r = minimize(f, np.ones(X.shape[1]), jac=True, method="L-BFGS-B")
    if not r.success:
        print(f"  ⚠️ 当てはめが収束しなかった: {r.message}")
    if return_ll:
        return r.x, float(-(r.fun - l2 * (r.x @ r.x)))
    return r.x


def race_softmax_ll(score_logodds, y, race_ids):
    """点数 (log-odds 単位 = ×10 前) のレース内ソフトマックスの対数尤度 (fit_race_softmax と同じ目的関数)。"""
    s = np.asarray(score_logodds, dtype=float)
    y = np.asarray(y, dtype=float)
    rid = pd.factorize(pd.Series(race_ids))[0]
    ny = np.bincount(rid, weights=y)
    keep = ny[rid] > 0
    s, y = s[keep], y[keep]
    rid = pd.factorize(pd.Series(rid[keep]))[0]
    ny = np.bincount(rid, weights=y)
    t = y / ny[rid]
    mx = np.full(rid.max() + 1, -np.inf)
    np.maximum.at(mx, rid, s)
    lse = np.log(np.bincount(rid, weights=np.exp(s - mx[rid]))) + mx
    return float((t * (s - lse[rid])).sum())


def _race_names(race_ids):
    """race_id → race_name (障害の判定用)。特徴量テーブルに race_name が無いので DB から引く。"""
    from database import get_db
    ids = sorted(set(race_ids))
    out = {}
    with get_db() as conn:
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            q = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT race_id, race_name FROM races WHERE race_id IN ({q})", chunk):
                out[r[0]] = r[1] or ""
    return out


def prep_lin_frame(df):
    """当てはめ・評価に使う行: 確定走 (着順>0)、障害を除く、確定走6頭以上のレース。"""
    from race_utils import is_jump_race
    d = df[df["finish_position"] > 0].copy()
    if "race_name" not in d.columns:
        names = _race_names(d["race_id"].tolist())
        d["race_name"] = d["race_id"].map(names).fillna("")
    d = d[~d["race_name"].map(is_jump_race)]
    n = d.groupby("race_id")["horse_number"].transform("size")
    d = d[n >= MIN_RUNNERS].copy()
    d = d.sort_values(["race_date", "race_id", "horse_number"]).reset_index(drop=True)
    d["y"] = d["finish_position"].between(1, 3).astype(int)
    for col, *_ in FEATS:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0.0)
    return d


def lin_design(d, base, drop, lin_cols=None, flag_cols=()):
    """説明変数 [残した区分点の合計, 各連続項目の z, 初出走, 各 flag 項目の生の値] を作る。
    lin_cols: 使う連続項目 (既定: ml/scorecard.LIN_FEATS の全部)。
    flag_cols: 生の値で足す項目 (ml/scorecard.FLAG_FEATS、#168 の芝ダ替わり)。NaN は 0。
    区分の値は base["col_map"] の列から読む (#168)。"""
    from ml.scorecard import LIN_FEATS, DEBUT_COL, race_z
    use = [(c, dr) for c, _, dr in LIN_FEATS if lin_cols is None or c in lin_cols]
    lab = label(d, base["spec"], skip=set(drop), col_map=base.get("col_map"))
    keep_pts = np.zeros(len(d))
    for col, *_ in FEATS:
        if col in drop:
            continue
        keep_pts += np.array([base["pts"].get(col + "=" + str(x), 0.0) for x in lab[col].values])
    rid = d["race_id"].to_numpy()
    cols = ["base_pts"] + [c for c, _ in use] + ["debut"] + list(flag_cols)
    X = np.column_stack([keep_pts]
                        + [race_z(d[c].to_numpy(), rid, dr) for c, dr in use]
                        + [(pd.to_numeric(d[DEBUT_COL], errors="coerce").fillna(1) == 0)
                           .astype(float).to_numpy()]
                        + [pd.to_numeric(d[c], errors="coerce").fillna(0.0).to_numpy(float)
                           for c in flag_cols])
    return X, cols


def per_race(d, score):
    """各レースの ◎3着内 / 印5頭の捕捉数 / 完全捕捉 (同点は馬番の小さい方)。"""
    x = pd.DataFrame({"race_id": d["race_id"].values, "hn": d["horse_number"].values,
                      "y": d["y"].values, "s": np.asarray(score, dtype=float)})
    x = x.sort_values(["race_id", "s", "hn"], ascending=[True, False, True])
    x["rk"] = x.groupby("race_id").cumcount() + 1
    out = pd.DataFrame({"top1": x[x.rk == 1].set_index("race_id")["y"],
                        "cap5": x[x.rk <= 5].groupby("race_id")["y"].sum()})
    out["full"] = (out["cap5"] >= 3).astype(float)
    return out


def compare_scores(d, s_old, s_new, B=2000, seed=0):
    """同じレース同士の差 (新 − 旧) と、レース単位ブートストラップの 95% 区間。"""
    a, b = per_race(d, s_old), per_race(d, s_new)
    b = b.loc[a.index]
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(a), (B, len(a)))
    out = {"races": int(len(a))}
    for k in ("top1", "cap5", "full"):
        diff = (b[k] - a[k]).to_numpy(float)
        m = diff[idx].mean(1)
        out[k] = {"old": float(a[k].mean()), "new": float(b[k].mean()), "diff": float(diff.mean()),
                  "ci95": [float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))]}
    return out


def _fmt(r):
    t, c, f = r["top1"], r["cap5"], r["full"]
    return (f"◎3着内 {t['old']*100:.2f}%→{t['new']*100:.2f}% ({t['diff']*100:+.2f}pt "
            f"[{t['ci95'][0]*100:+.2f},{t['ci95'][1]*100:+.2f}]) / "
            f"印5頭の平均捕捉 {c['old']:.4f}→{c['new']:.4f} ({c['diff']:+.4f} "
            f"[{c['ci95'][0]:+.4f},{c['ci95'][1]:+.4f}]) / "
            f"完全捕捉 {f['old']*100:.2f}%→{f['new']*100:.2f}% ({f['diff']*100:+.2f}pt "
            f"[{f['ci95'][0]*100:+.2f},{f['ci95'][1]*100:+.2f}])")


def main_lin(args):
    import pickle as _pk
    from ml.scorecard import load_model, score_with_reasons, LIN_FEATS
    base = load_model(args.base)
    if not base:
        raise SystemExit(f"--base の点数表が読めない: {args.base}")
    drop = [c for c in (args.drop.split(",") if args.drop else []) if c]
    lin_cols = [c for c in (args.lin.split(",") if args.lin else []) if c]
    bad_lin = [c for c in lin_cols if c not in {c2 for c2, *_ in LIN_FEATS}]
    if bad_lin or not lin_cols:
        raise SystemExit(f"--lin に未知の項目 or 空: {bad_lin or args.lin} "
                         f"(使えるのは {[c for c, *_ in LIN_FEATS]})")
    unknown = [c for c in drop if c not in {c2 for c2, *_ in FEATS}]
    if unknown:
        raise SystemExit(f"--drop に未知の項目: {unknown}")
    lo, hi = args.lin_fit_from, args.lin_fit_until
    if not (lo and hi):
        raise SystemExit("--mode lin には --lin-fit-from と --lin-fit-until が必要")
    if str(base.get("train_until") or "") >= lo:
        print(f"  ⚠️ 当てはめ開始 {lo} が区分点の学習境界 {base.get('train_until')} 以前 "
              f"— 傾きが in-sample で膨らむ")

    if args.cache and os.path.exists(args.cache):
        print(f"📦 キャッシュから読込: {args.cache}")
        df = pd.read_pickle(args.cache)
    else:
        from fast_train import build_feature_table
        start = min(x for x in (lo, args.eval_from) if x)
        print(f"📊 特徴量テーブルを構築中 ({start}〜)...")
        df = build_feature_table(start_date=start)
        if args.cache:
            df.to_pickle(args.cache)
    need = [c for c, *_ in LIN_FEATS] + ["has_past_runs"]
    miss = [c for c in need if c not in df.columns]
    if miss:
        raise SystemExit(f"特徴量テーブルに {miss} が無い — 新しいコードで作り直すこと "
                         f"(古いキャッシュを使っていないか)")
    d = prep_lin_frame(df)
    fit = d[(d["race_date"] >= lo) & (d["race_date"] <= hi)].reset_index(drop=True)
    print(f"📅 当てはめ {lo}〜{hi}: {fit['race_id'].nunique()}レース / {len(fit)}頭 "
          f"(障害・確定5頭以下を除く)")
    X, cols = lin_design(fit, base, drop, lin_cols)
    b = fit_race_softmax(X, fit["y"].to_numpy(), fit["race_id"].to_numpy())
    b0 = float(b[0])
    if b0 <= 0:
        raise SystemExit(f"区分点の傾きが {b0:.3f} (<=0) — 当てはめが壊れている")
    lin = {c: float(b[i] / b0) for i, c in enumerate(cols) if c in lin_cols}
    debut = float(b[cols.index("debut")] / b0)
    print("  係数 (ソフトマックス): " + ", ".join(f"{c}={v:+.4f}" for c, v in zip(cols, b)))
    print("  区分点の単位に換算 (×10 = 点): "
          + ", ".join(f"{nm} 1SDあたり {lin[c]*10:+.1f}点"
                      for c, nm, _ in LIN_FEATS if c in lin)
          + f", JRA初出走 {debut*10:+.1f}点")

    model = dict(base)
    model.update({
        "version": "scorecard_v2_lin",
        "drop": drop,
        "lin": lin,
        "debut_penalty": debut,
        "lin_scale": b0,
        "lin_raw_coef": {c: float(v) for c, v in zip(cols, b)},
        "lin_fit_from": lo, "lin_fit_until": hi,
        "lin_fit_races": int(fit["race_id"].nunique()), "lin_fit_rows": int(len(fit)),
        "lin_def": {"form_class_coef": 0.06, "timegap_cap_sec": 3.0, "window": 5,
                    "win_time": "1着馬のタイム (ml.features.winner_times)。不明な走は除外",
                    "class_unknown": "その走の格が不明→クラス差0 / 今回の格が不明→全走補正なし",
                    "class_from_name": "grade 空なら race_name から (GI)/(3勝クラス) 等を読む",
                    "z": "race-wise, ddof=1, NaN→0", "min_runners_fit": MIN_RUNNERS},
    })

    # 当てはめ期間での自己点検 (in-sample。参考値)
    s_old = score_with_reasons(fit, base)[0]
    s_new = score_with_reasons(fit, model)[0]
    print(f"\n【当てはめ期間 (in-sample, 参考)】{_fmt(compare_scores(fit, s_old, s_new))}")

    report = {"fit": {"from": lo, "until": hi, "races": int(fit['race_id'].nunique())},
              "coef_raw": model["lin_raw_coef"], "lin": lin, "debut_penalty": debut}
    if args.eval_from:
        ev = d[(d["race_date"] >= args.eval_from)
               & (d["race_date"] <= (args.eval_until or "9999-12-31"))].reset_index(drop=True)
        if hi >= args.eval_from:
            print(f"  ⚠️ 評価期間 {args.eval_from}〜 が当てはめ期間 (〜{hi}) と重なる — in-sample")
        s_old = score_with_reasons(ev, base)[0]
        s_new = score_with_reasons(ev, model)[0]
        r = compare_scores(ev, s_old, s_new)
        print(f"\n【評価 {args.eval_from}〜{args.eval_until or ''} ({r['races']}レース, 学習外)】")
        print("  " + _fmt(r))
        report["eval"] = r
    if args.report:
        import json as _js
        with open(args.report, "w", encoding="utf-8") as f:
            _js.dump(report, f, ensure_ascii=False, indent=1)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "wb") as f:
        _pk.dump(model, f)
    print(f"\n💾 {args.out} に保存 (区分点 {len(model['pts'])}個 + 連続項目 {len(lin)}個 + 初出走)")


# ══════════════════════════════════════════════════════════════════════
# --mode f2 (v3, #168): 区分点を窓1、連続項目・JRA初出走・芝ダ替わりを窓2 で当てはめる二段
# ══════════════════════════════════════════════════════════════════════
# 芝ダ替わりの候補 (事前に決めた3つだけ。評価期間を見て選ばない)
SF_CANDIDATES = {
    "a": ("sf_first_switch",),                       # 初替わり (0/1)
    "b": ("sf_other_share5",),                       # 近5走の他馬場の割合
    "c": ("sf_first_switch", "sf_other_share5"),     # (a)+(b)
}
DEFAULT_COL_MAP = "si_avg=si_avg_v,si_latest=si_latest_v"


def parse_col_map(text):
    """'si_avg=si_avg_v,si_latest=si_latest_v' → dict。空なら {}。"""
    out = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SystemExit(f"--col-map の書式は 項目=列: {part}")
        k, v = (x.strip() for x in part.split("=", 1))
        if k not in {c for c, *_ in FEATS}:
            raise SystemExit(f"--col-map の項目 {k} が FEATS に無い")
        out[k] = v
    return out


def coerce_frame(d, col_map=None, extra=()):
    """区分の差し替え列・追加列を数値にする (区分項目と同じく NaN→0)。"""
    for c in list((col_map or {}).values()) + list(extra):
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0.0)
    return d


def fit_pts_stage(d, drop=(), col_map=None, C=1.0):
    """区分点 (pts) の当てはめ: 窓の行で、drop を除く区分項目のダミーのロジスティック回帰
    (目標 = 3着内)。各項目の平均的な馬が0点。col_map の列から値を読む。"""
    spec = build_spec(d, col_map)
    lab = label(d, spec, skip=set(drop), col_map=col_map)
    info = {}
    pts, cols = fit(lab, d["y"].to_numpy(), C=C, info=info)
    return {"pts": pts, "cols": cols, "spec": spec, "drop": list(drop),
            "col_map": dict(col_map or {}), "pts_solver": info}


def fit_lin_stage(d, base, drop, lin_cols, flag_cols=()):
    """連続項目・JRA初出走・flag 項目の当てはめ: [区分点の合計, z…, 初出走, flag…] の
    レース内ソフトマックス。区分点の係数で割って点数の単位に揃える。
    戻り値: (model, 対数尤度, 当てはめた係数の数)"""
    X, cols = lin_design(d, base, drop, lin_cols, flag_cols)
    b, ll = fit_race_softmax(X, d["y"].to_numpy(), d["race_id"].to_numpy(), return_ll=True)
    b0 = float(b[0])
    if b0 <= 0:
        raise SystemExit(f"区分点の傾きが {b0:.3f} (<=0) — 当てはめが壊れている")
    m = dict(base)
    m.update({
        "lin": {c: float(b[i] / b0) for i, c in enumerate(cols) if c in lin_cols},
        "debut_penalty": float(b[cols.index("debut")] / b0),
        "flags": {c: float(b[cols.index(c)] / b0) for c in flag_cols},
        "lin_scale": b0,
        "lin_raw_coef": {c: float(v) for c, v in zip(cols, b)},
    })
    if not m["flags"]:
        m.pop("flags")
    return m, ll, len(cols)


def window_rows(d, lo, hi):
    return d[(d["race_date"] >= lo) & (d["race_date"] <= hi)].reset_index(drop=True)


def main_f2(args):
    import pickle as _pk
    import json as _js
    from ml.scorecard import load_model, score_with_reasons, LIN_FEATS, FLAG_KIND
    from ml import speed_variant as _sv
    col_map = parse_col_map(args.col_map)
    drop = [c for c in (args.drop.split(",") if args.drop else []) if c]
    lin_cols = [c for c in (args.lin.split(",") if args.lin else []) if c]
    if [c for c in lin_cols if c not in {c2 for c2, *_ in LIN_FEATS}] or not lin_cols:
        raise SystemExit(f"--lin に未知の項目 or 空: {args.lin}")
    cands = [c.strip() for c in (args.sf_candidates or "").split(",") if c.strip()]
    bad = [c for c in cands if c not in SF_CANDIDATES and c != "none"]
    if bad:
        raise SystemExit(f"--sf-candidates に未知の候補 {bad} (使えるのは a,b,c,none)")
    p_lo, p_hi = args.pts_fit_from, args.pts_fit_until
    l_lo, l_hi = args.lin_fit_from, args.lin_fit_until
    if not (p_lo and p_hi and l_lo and l_hi):
        raise SystemExit("--mode f2 には --pts-fit-from/--pts-fit-until/--lin-fit-from/--lin-fit-until が必要")
    if l_lo <= p_hi:
        print(f"  ⚠️ 連続項目の窓 {l_lo}〜 が区分点の窓 (〜{p_hi}) と重なる — 傾きが in-sample で膨らむ")

    # 基準表 (V2d)。col_map が si_*_v を読むなら必須
    table = _sv.load_table(args.si_table)
    uses_v = any(v in _sv.SV_COLS for v in col_map.values())
    if uses_v and table is None:
        raise SystemExit(f"col_map が V2d の列を読むのに基準表が無い: {args.si_table or _sv.TABLE_PATH}")
    if table is not None and uses_v and str(table["cutoff"]) > p_hi:
        print(f"  ⚠️ 基準表のカットオフ {table['cutoff']} が区分点の窓の終わり {p_hi} より後 "
              f"— μ̂・κ̂・c が区分点の窓の外のデータを見ている")

    if args.cache and os.path.exists(args.cache):
        print(f"📦 キャッシュから読込: {args.cache}")
        df = pd.read_pickle(args.cache)
    else:
        from fast_train import build_feature_table
        start = min(x for x in (p_lo, l_lo, args.eval_from) if x)
        print(f"📊 特徴量テーブルを構築中 ({start}〜)...")
        df = build_feature_table(start_date=start)
        if args.cache:
            df.to_pickle(args.cache)
    if uses_v:
        h = df.attrs.get("si_variant_table_hash")
        if h != table["table_hash"]:
            msg = (f"特徴量テーブルの si_avg_v は基準表 {h} で作られている (今の基準表 {table['table_hash']})"
                   f" — fast_train.augment_scorecard_columns か build_feature_table で作り直すこと")
            if args.allow_table_mismatch:
                print("  ⚠️ " + msg)
            else:
                raise SystemExit(msg)
    flag_all = sorted({c for k in cands if k != "none" for c in SF_CANDIDATES[k]})
    need = list(col_map.values()) + lin_cols + ["has_past_runs"] + flag_all
    miss = [c for c in need if c not in df.columns]
    if miss:
        raise SystemExit(f"特徴量テーブルに {miss} が無い — 新しいコードで作り直すこと "
                         f"(scripts/build_si_variant.py --augment-cache)")
    unknown_flags = [c for c in flag_all if c not in FLAG_KIND]
    if unknown_flags:
        raise SystemExit(f"ml/scorecard.FLAG_FEATS に無い項目: {unknown_flags}")

    d = coerce_frame(prep_lin_frame(df), col_map)
    d1 = window_rows(d, p_lo, p_hi)
    d2 = window_rows(d, l_lo, l_hi)
    print(f"📅 区分点 {p_lo}〜{p_hi}: {d1['race_id'].nunique()}レース / {len(d1)}頭 | "
          f"連続項目 {l_lo}〜{l_hi}: {d2['race_id'].nunique()}レース / {len(d2)}頭 "
          f"(障害・確定5頭以下を除く)")
    print(f"   col_map={col_map} drop={drop} lin={lin_cols}")
    stage1 = fit_pts_stage(d1, drop, col_map)
    si = {k: round(v * 10, 2) for k, v in stage1["pts"].items() if k.split("=")[0] in col_map}
    print(f"  区分点 {len(stage1['pts'])}個 (指数の点×10: {si})")

    fits = {}
    for k in (["none"] if "none" in cands else []) + [c for c in cands if c != "none"]:
        flags = () if k == "none" else SF_CANDIDATES[k]
        m, ll, nk = fit_lin_stage(d2, stage1, drop, lin_cols, flags)
        fits[k] = (m, ll, nk, len(flags))
        print(f"  候補 {k:4s} {list(flags)}: 対数尤度 {ll:.2f} (係数 {nk}個) "
              f"flags(点) {({c: round(v*10, 2) for c, v in (m.get('flags') or {}).items()})}")
    sel_pool = [k for k in cands if k != "none"] or ["none"]
    if args.sf_select == "ll":
        chosen = max(sel_pool, key=lambda k: fits[k][1])
    else:
        # AIC 相当: 対数尤度 − 足した係数の数。(c) は (a)(b) を含むので、生の対数尤度では必ず (c) が勝つ
        chosen = max(sel_pool, key=lambda k: fits[k][1] - fits[k][3])
    model, ll, nk, _ = fits[chosen]
    print(f"  → 選択: {chosen} (規則 {args.sf_select})")

    model.update({
        "version": "scorecard_v3_f2",
        "train_until": p_hi,
        "pts_fit_from": p_lo, "pts_fit_until": p_hi,
        "train_races": int(d1["race_id"].nunique()),
        "lin_fit_from": l_lo, "lin_fit_until": l_hi,
        "lin_fit_races": int(d2["race_id"].nunique()), "lin_fit_rows": int(len(d2)),
        "sf_candidate": chosen,
        "sf_select": {"rule": args.sf_select, "window": [l_lo, l_hi],
                      "ll": {k: v[1] for k, v in fits.items()},
                      "n_coef": {k: v[2] for k, v in fits.items()}},
        "lin_def": {"form_class_coef": 0.06, "timegap_cap_sec": 3.0, "window": 5,
                    "z": "race-wise, ddof=1, NaN→0", "min_runners_fit": MIN_RUNNERS,
                    "flags": "係数 × 生の値 (NaN→0)、ml/speed_variant.surface_switch_features"},
    })
    if table is not None and uses_v:
        model["si_variant"] = {k: table.get(k) for k in
                               ("version", "table_hash", "cutoff", "fit_from", "dirt_shift_c",
                                "created_at", "age_rule", "require_complete")}

    # 自己点検: 保存する点数 (×10) を温度 lin_scale で戻すと、当てはめた対数尤度と一致するはず
    s_new = score_with_reasons(d2, model)[0]
    ll_chk = race_softmax_ll(s_new / 10 * model["lin_scale"], d2["y"], d2["race_id"])
    print(f"\n【連続項目の窓 (in-sample, 参考)】 対数尤度 {ll_chk:.2f} (当てはめ時 {ll:.2f})")
    if abs(ll_chk - ll) > 1e-3 * max(1.0, abs(ll)):
        raise SystemExit("保存する点数が当てはめた式と一致しない — score_with_reasons と lin_design の食い違い")
    report = {"chosen": chosen, "sf_ll": model["sf_select"]["ll"], "lin": model["lin"],
              "debut_penalty": model["debut_penalty"], "flags": model.get("flags"),
              "si_variant": model.get("si_variant")}
    base = load_model(args.base) if args.base else None
    if args.eval_from:
        ev = window_rows(d, args.eval_from, args.eval_until or "9999-12-31")
        if (args.eval_from <= l_hi) or (args.eval_from <= p_hi):
            print(f"  ⚠️ 評価期間 {args.eval_from}〜 が当てはめ窓と重なる — in-sample")
        s_new = score_with_reasons(ev, model)[0]
        report["eval"] = {}
        if base:
            r = compare_scores(ev, score_with_reasons(ev, base)[0], s_new)
            print(f"\n【評価 {args.eval_from}〜{args.eval_until or ''} ({r['races']}レース) 新 vs --base】")
            print("  " + _fmt(r))
            report["eval"]["vs_base"] = r
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            _js.dump(report, f, ensure_ascii=False, indent=1, default=float)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "wb") as f:
        _pk.dump(model, f)
    print(f"\n💾 {args.out} に保存 (区分点 {len(model['pts'])}個 + 連続項目 {len(model['lin'])}個 + "
          f"初出走 + 芝ダ替わり {list((model.get('flags') or {}).keys())})")
    return model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("full", "lin", "f2"), default="full",
                    help="full=区分点を学習し直す (従来) / lin=区分点は据え置きで連続項目を当てはめる / "
                         "f2=区分点を窓1・連続項目と芝ダ替わりを窓2 で当てはめる二段 (#168)")
    ap.add_argument("--train-until", default=None,
                    help="この日までを学習に使う (既定: 全データの時系列80%%)")
    ap.add_argument("--out", default=MODEL_PATH)
    ap.add_argument("--cache", default=None,
                    help="特徴量テーブルの pickle (構築を省略して再利用)")
    ap.add_argument("--db", default=None, help="使う DB のパス (既定: keiba.db)")
    # --mode lin
    ap.add_argument("--base", default=MODEL_PATH, help="[lin] 区分点を引き継ぐ点数表")
    ap.add_argument("--drop", default=",".join(DEFAULT_DROP), help="[lin] 外す区分項目 (カンマ区切り)")
    ap.add_argument("--lin", default="form_cls5,tb_min5",
                    help="[lin] 足す連続項目 (カンマ区切り)。例: タイム差だけなら "
                         "--lin tb_min5 --drop margin_best (近5走の平均着順の区分点は残す)")
    ap.add_argument("--lin-fit-from", default=None, help="[lin] 当てはめ期間の初日 (YYYY-MM-DD)")
    ap.add_argument("--lin-fit-until", default=None, help="[lin] 当てはめ期間の最終日")
    ap.add_argument("--eval-from", default=None, help="[lin] 学習外の評価期間の初日 (任意)")
    ap.add_argument("--eval-until", default=None, help="[lin] 評価期間の最終日")
    ap.add_argument("--report", default=None, help="[lin/f2] 係数と評価を JSON で書き出す")
    # --mode f2 (#168)
    ap.add_argument("--col-map", default=DEFAULT_COL_MAP,
                    help="[f2] 区分項目の値を読む列の差し替え (空文字で差し替えなし)")
    ap.add_argument("--pts-fit-from", default=None, help="[f2] 区分点の窓の初日")
    ap.add_argument("--pts-fit-until", default=None, help="[f2] 区分点の窓の最終日")
    ap.add_argument("--sf-candidates", default="a,b,c",
                    help="[f2] 芝ダ替わりの候補 (a=初替わり / b=近5走の他馬場の割合 / c=a+b / none=なし)")
    ap.add_argument("--sf-select", choices=("aic", "ll"), default="aic",
                    help="[f2] 候補の選び方 (連続項目の窓の中だけ)。ll=対数尤度最大 (c が a,b を含むので"
                         "常に c になる) / aic=対数尤度−係数の数")
    ap.add_argument("--si-table", default=None, help="[f2] V2d の基準表 (既定 models/si_variant_table.json)")
    ap.add_argument("--allow-table-mismatch", action="store_true",
                    help="[f2] 特徴量テーブルの si_avg_v が別の基準表で作られていても続ける (非推奨)")
    args = ap.parse_args()
    if args.db:
        import database
        database.DB_PATH = args.db
    if args.mode == "lin":
        return main_lin(args)
    if args.mode == "f2":
        if args.base == MODEL_PATH and not os.path.exists(MODEL_PATH):
            args.base = None
        return main_f2(args)

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
