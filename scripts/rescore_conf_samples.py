#!/usr/bin/env python3
"""過去のレースを指定の点数表で選び直した◎の (◎表示勝率, 騎手信頼) を作る (#175)。

信頼度の閾値は「今の印ロジックが出す◎表示勝率の分布」の分位で引く (#151、閾値と分布はペア)。
印の点数表を切り替えた直後 (#175: base → roi) は、新しい点数表の◎で付いた予測がまだ無い。
そこで直近の予測キャッシュ (最大400R、build_race_baseline と同じ選び方) の各レースについて、
特徴量テーブル (fast_train.build_feature_table と同じ列。--cache) の行を指定の点数表で採点し直し、
本番と同じ決め方 (取消を除いた馬の中で丸めない点数が最大、同点は馬番の小さい方) で◎を選ぶ。
その馬の pred_win_display_pct (予測時点の LightGBM の表示勝率。点数表に依らない) と
jockey_trust をキャッシュから取り、models/scorecard_<名前>_conf_samples.json に書く。
build_race_baseline は、新しい点数表で付いた本物の予測が400件に満たない間これで補う。

忠実さの点検: 同じ手順を base (いま配信していた点数表) で行い、選び直した◎がキャッシュの◎
(実際に配信した◎) と一致する割合を出す (95% 未満なら止まる)。点検するのは base の補正窓の
最終日より後のレースだけ (それより前は別の版の点数表で配信していた。2026-11-18 の実測:
10/31〜11/15 は 228/228 一致、10/10〜10/25 は v2 の点数表の時代で約7割)。

使い方:
  python3 scripts/rescore_conf_samples.py --model roi --cache feat.pkl [--db keiba.db]
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def pick_axis(rows, model):
    """rows: そのレースの採点対象 (取消を除いた) 特徴量の行。◎の馬番。"""
    from ml.scorecard import score_with_reasons
    pts = np.asarray(score_with_reasons(rows, model)[0], dtype=float)
    order = sorted(range(len(rows)), key=lambda i: (-pts[i], int(rows["horse_number"].iloc[i])))
    return int(rows["horse_number"].iloc[order[0]])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="roi", choices=("roi", "base"))
    ap.add_argument("--cache", required=True, help="特徴量テーブルの pickle (si_avg_v 等を含む)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    import database
    if a.db:
        database.DB_PATH = a.db
    from build_race_baseline import PRE_START_SQL
    from race_utils import is_ml_out_of_domain
    from ml.scorecard import (load_named_model, check_si_variant, without_col_map, FEATS)
    import train_scorecard as ts

    models = {}
    for nm in {a.model, "base"}:
        m = load_named_model(nm)
        if m is None:
            raise SystemExit(f"点数表 {nm} が無い")
        msg = check_si_variant(m)
        if msg:
            raise SystemExit(f"点数表 {nm}: {msg}")
        models[nm] = m

    df = pd.read_pickle(a.cache)
    with database.get_db() as conn:
        conn.row_factory = __import__("sqlite3").Row
        rows = conn.execute(f"""
            SELECT pc.race_id, pc.predictions_json, g.race_name, g.race_date
            FROM predictions_cache pc JOIN races g ON pc.race_id = g.race_id
            WHERE {PRE_START_SQL} AND pc.predictions_json LIKE '%"sc_points"%'
            ORDER BY g.race_date DESC LIMIT {a.n * 2}
        """).fetchall()
    df = df[df["race_id"].isin({r["race_id"] for r in rows})].copy()
    df = ts.coerce_frame(df, models["base"].get("col_map"))
    for col, *_ in FEATS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    by_race = {rid: g for rid, g in df.groupby("race_id")}

    out, agree, n_chk, miss = [], 0, 0, 0
    for r in rows:
        if len(out) >= a.n:
            break
        if is_ml_out_of_domain(r["race_name"] or ""):
            continue
        preds = json.loads(r["predictions_json"] or "[]")
        if not any((p.get("sc_points") or 0) for p in preds):
            continue
        g = by_race.get(r["race_id"])
        if g is None:
            miss += 1
            continue
        ok = {int(p["horse_number"]) for p in preds if not p.get("scratched")}
        g = g[g["horse_number"].astype(int).isin(ok)].sort_values("horse_number").reset_index(drop=True)
        if len(g) < 2:
            miss += 1
            continue
        by_hn = {int(p["horse_number"]): p for p in preds}
        cached_axis = next((int(p["horse_number"]) for p in preds if p.get("mark") == "◎"), None)
        # 点検は「今の base の点数表で配信したレース」だけ (それより前は別の版の点数表で
        # 配信していた。base の補正窓の最終日より後のレースは今の base でしかありえない)
        if (cached_axis is not None and not any(p.get("sc_model") for p in preds)
                and str(r["race_date"]) > str(models["base"].get("lin_fit_until") or "")):
            n_chk += 1
            agree += int(pick_axis(g, models["base"]) == cached_axis)
        hn = pick_axis(g, models[a.model])
        p = by_hn.get(hn) or {}
        if not p.get("pred_win_display_pct"):
            continue
        out.append({"race_id": r["race_id"], "race_date": r["race_date"], "horse_number": hn,
                    "w": float(p["pred_win_display_pct"]), "jt": p.get("jockey_trust", 0) or 0,
                    "same_as_cached": hn == cached_axis})
    rate = agree / n_chk if n_chk else 0.0
    same = sum(o["same_as_cached"] for o in out) / len(out) if out else 0.0
    print(f"レース {len(out)}件 (特徴量の無いレース {miss}件)。忠実さ: base で選び直した◎ = 配信した◎ "
          f"{agree}/{n_chk} ({rate*100:.1f}%)。{a.model} の◎が配信した◎と同じ {same*100:.1f}%")
    if n_chk and rate < 0.95:
        raise SystemExit("選び直しが配信した◎と食い違いすぎる — 特徴量テーブルと本番の特徴量がずれている")
    path = a.out or os.path.join(ROOT, "models", f"scorecard_{a.model}_conf_samples.json")
    ws = sorted(o["w"] for o in out)
    with open(path, "w", encoding="utf-8") as f:
        import hashlib
        from ml.scorecard import MODEL_PATHS
        with open(MODEL_PATHS[a.model], "rb") as _f:
            _sha = hashlib.sha1(_f.read()).hexdigest()
        # #175 レビュー: どの版の点数表で選び直したか (build_race_baseline は今の pkl と同じ版の時だけ使う)
        json.dump({"model": a.model, "model_sha1": _sha, "model_version": models[a.model].get("version"),
                   "lin_fit_until": models[a.model].get("lin_fit_until"),
                   "n": len(out), "fidelity_base_axis_match": round(rate, 4),
                   "same_axis_as_cached": round(same, 4),
                   "period": [min(o["race_date"] for o in out), max(o["race_date"] for o in out)] if out else None,
                   "w_median": ws[len(ws) // 2] if ws else None,
                   "samples": out}, f, ensure_ascii=False, indent=0)
    print(f"💾 {path}")


if __name__ == "__main__":
    main()
