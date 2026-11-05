#!/usr/bin/env python3
"""点数表の本番の印 (v3) と旧版 v2 を、同じレースで対応させて比べる (#169)。

なぜ必要か
----------
v3 (#168: 馬場差を引いたスピード指数 V2d を使う点数表) を入れた最初の週末 (10/31・11/01、48R) で、
v2 で印を作り直しても印5頭の捕捉は 2.125 対 2.125 (差 0.000 [−0.146, +0.146]) だった。
#168 の見込み差 (+0.022頭/R) を判定するには約2,900〜4,800R (60〜100週末) 要る。
「先週より良い/悪い」で v3 を評価せず、同じレースの対応ありの差を毎週積み上げて累積で見る。

やること
--------
- 各レースの推論側特徴量を作り直し (ml.features.FeatureBuilder、DB は手元のコピー)、
  v2 (models/scorecard_v2_ref.pkl = git show 4213ced4^:models/scorecard.pkl) と現行の点数表
  (models/scorecard.pkl) で点数を付け直す。v2 と v3 の特徴量の作り方の違い (V2d の有無) は
  pkl の col_map が再現する: v3 は si_avg/si_latest の区分を si_avg_v/si_latest_v で読み、
  v2 は従来の si_avg/si_latest を読む (同じ特徴量の作り直しから両方が出る)。
- 本番の印は投稿印 (posted_marks、凍結) を優先し、投稿していないレースは公開 JSON の印。
- 結果は docs/data/scorecard_v2_paired.json にレース単位で貯める (1レース1回だけ計算する)。
  特徴量の作り直しは1レース約70秒かかるので、計算済みのレースは飛ばす。
- 同点は丸めない点数 → 馬番で決める (本番の同点を LGB の順で決める欠陥 #169-9 は P 班が直す)。

注意: 特徴量はいまの DB から作り直すので、予測時点の後に補完されたデータ (血統の backfill 等) が
わずかに入る。現行の点数表での作り直しが本番の印とどれだけ一致するかを毎回出し、再現度を確かめる
(10/31・11/01 の作り直しでは印5頭 48/48R・◎ 48/48R で一致した — find-accuracy の検証)。
"""
import argparse
import glob
import json
import os
import pickle
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

DATA = os.path.join(ROOT, "docs", "data")
CACHE = os.path.join(DATA, "scorecard_v2_paired.json")
V2_PKL = os.path.join(ROOT, "models", "scorecard_v2_ref.pkl")
CAPTURE_MARKS = ["◎", "○", "▲", "△", "△"]
JST = timezone(timedelta(hours=9))
MDE_NOTE = "#168 の見込み差 +0.022頭/R を80%検出力で見るには約2,900〜4,800R が要る"


def _load_cache(path=CACHE):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d.get("races"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"races": {}}


def _prod_marks(d8):
    """{race_id: [◎, ○, ▲, △, △] の馬番}。投稿印を優先、無ければ公開 JSON の印。"""
    out = {}
    p = os.path.join(DATA, f"predictions_{d8}.json")
    if os.path.exists(p):
        doc = json.load(open(p, encoding="utf-8"))
        for _v, rs in (doc.get("venues") or {}).items():
            for r in rs or []:
                ms = [(h.get("mark"), h.get("horse_number")) for h in r.get("horses") or [] if h.get("mark")]
                out[str(r["race_id"])] = ms
    pm = os.path.join(DATA, f"posted_marks_{d8}.json")
    if os.path.exists(pm):
        for rid, ents in ((json.load(open(pm, encoding="utf-8")) or {}).get("races") or {}).items():
            out[str(rid)] = [(e.get("mark"), e.get("horse_number")) for e in ents if e.get("mark")]
    res = {}
    for rid, ms in out.items():
        order = []
        for m in ["◎", "○", "▲", "△"]:
            order += [hn for mk, hn in ms if mk == m]
        if len(order) == 5:
            res[rid] = order
    return res


def _top5(df, pts):
    d = df.assign(_p=pts).sort_values(["_p", "horse_number"], ascending=[False, True], kind="mergesort")
    return [int(x) for x in d["horse_number"].tolist()[:5]]


def _features_worker(race_ids):
    from ml.features import FeatureBuilder
    fb = FeatureBuilder()
    out = []
    for rid in race_ids:
        try:
            out.append(fb.build_features_for_race(rid))
        except Exception as e:      # 1レースの失敗で全体を止めない
            print(f"  ⚠️ 特徴量の作り直しに失敗 {rid}: {e}", flush=True)
    return out


def compute(days, conn, features_pkls=None, workers=1, cache_path=CACHE):
    """days の確定済みレースのうち、まだ貯めていないものを計算して cache に足す。"""
    import pandas as pd
    from ml import scorecard as sc
    cache = _load_cache(cache_path)
    have = cache["races"]
    m2 = pickle.load(open(V2_PKL, "rb"))
    m3 = sc.load_model()
    warn = sc.check_si_variant(m3)
    if warn:
        print(f"  ⚠️ 現行の点数表: {warn} → 区分の差し替えを外して代用 (本番の predict と同じ扱い)")
        m3 = sc.without_col_map(m3, warn)
    todo = []
    marks = {}
    for d8 in days:
        pm = _prod_marks(d8)
        d = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        for (rid,) in conn.execute("SELECT race_id FROM races WHERE race_date=? ORDER BY race_id", (d,)):
            if rid in have or rid not in pm:
                continue
            if not conn.execute("SELECT 1 FROM results WHERE race_id=? AND finish_position=1", (rid,)).fetchone():
                continue
            todo.append(rid)
            marks[rid] = (d8, pm[rid])
    if not todo:
        print("  v2 対応比較: 新しく計算するレースなし")
        return cache
    feats = []
    if features_pkls:
        for p in features_pkls:
            f = pd.read_pickle(p)
            feats.append(f[f["race_id"].astype(str).isin(todo)])
    have_f = {str(x) for f in feats for x in f["race_id"].unique()}
    rest = [r for r in todo if r not in have_f]
    if rest:
        print(f"  v2 対応比較: {len(rest)}R の特徴量を作り直す (1レース約70秒、workers={workers})", flush=True)
        if workers > 1:
            from concurrent.futures import ProcessPoolExecutor
            chunks = [rest[i::workers] for i in range(workers)]
            with ProcessPoolExecutor(max_workers=workers) as ex:
                for part in ex.map(_features_worker, chunks):
                    feats += part
        else:
            feats += _features_worker(rest)
    feat = pd.concat([f for f in feats if len(f)], ignore_index=True) if feats else None
    if feat is None or not len(feat):
        print("  ⚠️ v2 対応比較: 特徴量が作れなかった")
        return cache
    miss2 = [c for c in sc.required_columns(m2) if c not in feat.columns]
    miss3 = [c for c in sc.required_columns(m3) if c not in feat.columns]
    if miss2 or miss3:
        print(f"  ⚠️ v2 対応比較: 必要な列が無い v2={miss2} v3={miss3} — 計算しない")
        return cache
    feat = feat.sort_values(["race_id", "horse_number"]).reset_index(drop=True)
    p2, _ = sc.score_with_reasons(feat, m2)
    p3, _ = sc.score_with_reasons(feat, m3)
    feat["_p2"], feat["_p3"] = p2, p3
    now = datetime.now(JST).isoformat(timespec="seconds")
    for rid, g in feat.groupby(feat["race_id"].astype(str)):
        if rid not in marks:
            continue
        d8, prod = marks[rid]
        fin = {hn: fp for hn, fp in conn.execute(
            "SELECT horse_number, finish_position FROM results WHERE race_id=?", (rid,))}
        top3 = sorted(hn for hn, fp in fin.items() if fp and 0 < fp <= 3)
        win = next((hn for hn, fp in fin.items() if fp == 1), None)
        have[rid] = {"date": d8, "prod": prod, "v2": _top5(g, g["_p2"]), "cur": _top5(g, g["_p3"]),
                     "top3": top3, "win": win, "v2_version": m2.get("version"),
                     "cur_version": m3.get("version"), "computed_at": now}
    cache["v2_model"] = {"path": "models/scorecard_v2_ref.pkl", "source": "git show 4213ced4^:models/scorecard.pkl",
                         "version": m2.get("version")}
    cache["updated_at"] = now
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  v2 対応比較: {len(have)}R を貯めた ({cache_path})")
    return cache


def _metrics(e, key):
    hs = e[key]
    t3 = set(e["top3"])
    got = len(set(hs) & t3)
    return got, int(got == 3), int(hs[0] == e["win"]), int(hs[0] in t3)


def report(days, cache=None, out=print):
    cache = cache or _load_cache()
    races = cache.get("races") or {}
    if not races:
        out("  (d) v3 と v2 の対応比較: 貯めたレースなし (python3 scripts/scorecard_v2_paired.py で計算)")
        return
    rng = np.random.default_rng(169)
    for lab, sel in (("今週", [e for e in races.values() if e["date"] in days]),
                     ("累計", list(races.values()))):
        if not sel:
            continue
        P = np.array([_metrics(e, "prod") for e in sel], float)
        V = np.array([_metrics(e, "v2") for e in sel], float)
        C = np.array([_metrics(e, "cur") for e in sel], float)
        same5 = np.mean([set(e["cur"]) == set(e["prod"]) for e in sel])
        sameh = np.mean([e["cur"][0] == e["prod"][0] for e in sel])
        parts = []
        for i, nm in enumerate(("3着内の捕捉", "3頭とも", "◎1着", "◎3着内")):
            d = P[:, i] - V[:, i]
            bs = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(4000)]
            lo, hi = np.percentile(bs, [2.5, 97.5])
            if i == 0:
                parts.append(f"{nm} {P[:,0].mean():.3f} vs {V[:,0].mean():.3f}頭 差 {d.mean():+.3f} [{lo:+.3f}, {hi:+.3f}]")
            else:
                parts.append(f"{nm} {P[:,i].mean():.1%} vs {V[:,i].mean():.1%} 差 {100*d.mean():+.1f}pt [{100*lo:+.1f}, {100*hi:+.1f}]")
        wins = int(((P[:, 0] - V[:, 0]) > 0).sum()); loss = int(((P[:, 0] - V[:, 0]) < 0).sum())
        out(f"  (d) [{lab}] 本番 (v3) vs v2 作り直し ({len(sel)}R、同じレースで対応): " + " / ".join(parts)
            + f" / 本番が良い {wins}R・悪い {loss}R")
        out(f"      再現度: 現行の点数表で作り直した印5頭が本番と同じ {same5:.0%}・◎同じ {sameh:.0%}"
            f" (作り直しの捕捉 {C[:,0].mean():.3f})")
    out(f"      ※ {MDE_NOTE}。累計が届くまで v3 の良し悪しは結論しない")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", required=True, help="対象日 YYYYMMDD")
    ap.add_argument("--db", default=os.path.join(ROOT, "keiba.db"))
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--features-pkl", nargs="*", help="作り直し済みの特徴量 pickle (あれば再利用)")
    ap.add_argument("--report-only", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(a.db)
    if not a.report_only:
        pk = []
        for g in a.features_pkl or []:
            pk += sorted(glob.glob(g))
        compute(a.days, conn, features_pkls=pk or None, workers=a.workers)
    report(a.days)


if __name__ == "__main__":
    main()
