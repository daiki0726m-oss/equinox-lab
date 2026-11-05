#!/usr/bin/env python3
"""週次レポートの「予測時点の市場との比較」(#169)。weekly_scorecard.py から呼ばれる。

なぜ必要か
----------
11/05 の週次反省 (10/31・11/01) と、その後の独立再計算 (9/05〜11/01 の552R) で:
  - 本番の LGB (☆・注・信頼度・投資ゲート・EV を担当) は、予測した時点の市場とほぼ同じ。
    レース内の複勝AUC は LGB 0.7855 / 同時刻市場 0.7842 (差 +0.0012 [−0.0018, +0.0043])、
    上位5頭の捕捉も 2.165 vs 2.158、上位5頭の重なり 4.78/5。柔軟な市場モデル (Harville
    + 多項式) に LGB を足しても交差検証の log-loss は −0.00015 [−0.00045, +0.00016] で改善しない。
  - ☆/注 は同時刻市場の見込みを超えて当たっていない (☆ 実際/期待 0.92 [0.77, 1.09]、
    注 0.85 [0.66, 1.05])。LGB と市場のズレの大きさで分けても当たり方は平ら。
  - 印5頭 (点数表) は予測時の人気上位5頭より3着内を約0.16頭少なく押さえる (#147 の製品判断の代償)。
  - 週次ログの val AUC 0.82 やバックテスト回収率162% は **確定オッズ** で作った数字で、
    本番 (朝の薄いオッズ) の実力ではない。
これらを毎週、同じ物差しで見えるようにする。モデルの価値の根拠には、確定オッズの数字を使わない。

予測時点のオッズの出どころ
--------------------------
1. S 班のスナップショット (docs/data/snapshots/、prediction_snapshot.py) — 予測した瞬間の全頭の値
2. 無ければ git 履歴の予測 JSON (scripts/pred_history.py):
   - 予測時 = その予測 (LGB 出力) が最初に現れた版 (= 予測した run の書き出し)
   - 投稿時 = 投稿時刻より前の最後の版 (= 読者が投稿を見た時にダッシュボードに出ていた版)
どちらを使ったかを必ず出力に書く。
"""
import glob
import json
import os
import sqlite3
import sys
import tempfile
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402
import pred_history as ph  # noqa: E402
from race_utils import is_jump_race, is_ml_out_of_domain  # noqa: E402

DATA = os.path.join(ROOT, "docs", "data")
CAPTURE_MARKS = ("◎", "○", "▲", "△")      # 印5頭 = ◎○▲△△ (#143)。☆/注 は穴枠で入れない
SCORECARD_ERA = "20260905"                  # 点数表で ◎○▲△△ を選び始めた日 (#147)
V3_ERA = "20261031"                         # 点数表 v3 (V2d) の最初の週末 (#168)
DEFAULT_CACHE = os.path.join(tempfile.gettempdir(), "equinox_pred_history")


def _auc(y, s):
    """Mann-Whitney の AUC (同点は 0.5)。y/s は 1次元 array"""
    y = np.asarray(y); s = np.asarray(s, float)
    pos = y == 1; npos = pos.sum(); nneg = len(y) - npos
    if npos == 0 or nneg == 0:
        return None
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s)); ranks[order] = np.arange(1, len(s) + 1)
    # 同点は平均順位
    _, inv, cnt = np.unique(s, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=ranks)
    ranks = (sums / cnt)[inv]
    return (ranks[pos].sum() - npos * (npos + 1) / 2) / (npos * nneg)


def _boot_mean(v, B=4000, seed=169):
    v = np.asarray(v, float)
    if len(v) < 2:
        return (float(v.mean()) if len(v) else 0.0), 0.0, 0.0
    rng = np.random.default_rng(seed)
    bs = [v[rng.integers(0, len(v), len(v))].mean() for _ in range(B)]
    return float(v.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def harville_top3(q):
    """勝率シェア q から Harville 式の3着内確率"""
    q = np.asarray(q, float); n = len(q); p = q.copy()
    for j in range(n):
        for i in range(n):
            if i != j:
                p[i] += q[j] * q[i] / (1 - q[j])
    for j in range(n):
        for k in range(n):
            if k == j:
                continue
            pjk = q[j] * q[k] / (1 - q[j]); rem = 1 - q[j] - q[k]
            if rem <= 0:
                continue
            for i in range(n):
                if i != j and i != k:
                    p[i] += pjk * q[i] / rem
    return p


def _snapshot_records(d8, post_at, posted):
    """S 班のスナップショットから race_id → 版 (予測時点)。無ければ {}"""
    ps = ph.snapshot_module()
    if ps is None:
        return {}
    out = {}
    try:
        if posted and hasattr(ps, "at_posting"):
            out.update(ps.at_posting(d8) or {})
        if hasattr(ps, "latest_before"):
            for rid, rec in (ps.latest_before(d8, post_at) or {}).items():
                out.setdefault(rid, rec)
    except Exception:
        return {}
    return out


def collect(days, conn, hist=None, hist_from=None):
    """開催日ごとに、レース単位の行を作る。

    hist_from (YYYYMMDD): これより前の日は git 履歴を使わない (shallow clone の境界より前は
    「予測が最初に現れた版」を取り違えるため、予測時オッズ不明として扱う)。

    返り値: (races, meta)
      races = [{d8, rid, name, surface, ood, posted, src, marks:{hn:印}, horses:[{hn, y, win,
                odds_pt, pop_pt, odds_post, l3, rs, sc}]}]
      meta  = [{d8, src_counts, post_at}]
    """
    out, meta = [], []
    for d8 in days:
        pm = ph.load_posted_marks(d8, DATA) or {}
        posted = pm.get("races") or {}
        post_at = ph.parse_jst(pm.get("posted_at")) or ph.parse_jst(f"{d8[:4]}-{d8[4:6]}-{d8[6:]} 10:30")
        snap = _snapshot_records(d8, post_at, posted)
        use_git = hist is not None and (hist_from is None or d8 >= hist_from)
        git = ph.predtime_from_git(hist, d8, post_at) if use_git else {}
        if hist is not None:
            hist.forget(d8)      # #169: 累計で読んだ版を全部抱えない (メモリが週ごとに線形に増える)
        _cp = os.path.join(DATA, f"predictions_{d8}.json")
        cur = ph.races_of(json.load(open(_cp, encoding="utf-8")) if os.path.exists(_cp) else {})
        counts = {}
        for rid in sorted(set(git) | set(snap) | set(cur)):
            res = {hn: (fp or 0) for hn, fp in conn.execute(
                "SELECT horse_number, finish_position FROM results WHERE race_id=?", (rid,))}
            if not any(v == 1 for v in res.values()):
                continue
            g = git.get(rid) or {}
            rec = snap.get(rid)
            pre = g.get("pre") or cur.get(rid)
            if pre is None and rec is None:
                continue
            if rec is not None:
                src = "スナップショット"
                hs_pt = {int(h.get("horse_number")): h for h in rec.get("horses") or []}
                lgb = hs_pt
            elif g.get("same") is not None:
                src = "git:予測を書き出した版"
                hs_pt = {int(h["horse_number"]): h for h in g["same"].get("horses") or []}
                lgb = {int(h["horse_number"]): h for h in (pre.get("horses") or [])}
            else:
                src = "現在のJSON (予測時オッズ不明)"
                hs_pt, lgb = {}, {int(h["horse_number"]): h for h in (pre.get("horses") or [])}
            counts[src] = counts.get(src, 0) + 1
            # 投稿時のオッズは git 履歴の「投稿直前の公開版」からだけ取る。履歴が無く現在の JSON で
            # 代用した時は、refresh_odds が発走直前まで上書きした確定寄りのオッズなので「投稿時」と
            # 呼ばない (空にして (a) の投稿時の比較から外す)。
            post_odds = ({int(h["horse_number"]): h for h in (g["pre"].get("horses") or [])}
                         if g.get("pre") is not None else {})
            if rid in posted:
                marks = {e.get("horse_number"): e.get("mark") for e in posted[rid] if e.get("mark")}
            else:
                marks = {int(h["horse_number"]): h.get("mark") for h in (pre or rec).get("horses") or []
                         if h.get("mark")}
            name = (pre or {}).get("race_name") or ""
            if not name:
                row = conn.execute("SELECT race_name FROM races WHERE race_id=?", (rid,)).fetchone()
                name = row[0] if row else ""
            horses = []
            for hn, fp in res.items():
                if not fp or fp <= 0:
                    continue          # 取消・除外・中止は採点しない
                a = hs_pt.get(hn) or {}
                b = lgb.get(hn) or {}
                c = post_odds.get(hn) or {}
                o_pt = a.get("odds") if "odds" in a else a.get("odds_win")
                horses.append(dict(hn=hn, y=int(fp <= 3), win=int(fp == 1),
                                   odds_pt=o_pt or 0, pop_pt=a.get("popularity") or 0,
                                   odds_post=c.get("odds_win") or 0,
                                   l3=b.get("pred_top3_abs_pct"), rs=b.get("rank_score"),
                                   sc=b.get("sc_points")))
            out.append(dict(d8=d8, rid=rid, name=name, surface=(pre or {}).get("surface"),
                            ood=is_ml_out_of_domain(name), posted=rid in posted, src=src,
                            marks=marks, horses=horses))
        meta.append({"d8": d8, "src": counts, "post_at": post_at})
    return out, meta


def _valid(h, key):
    v = h.get(key) or 0
    return 0 < v < 999


def capture_vs_popularity(races, odds_key="odds_pt"):
    """(a) 印5頭 vs 同時点の人気上位5頭 の3着内捕捉。レース単位の差。"""
    rows = []
    for r in races:
        hs = [h for h in r["horses"] if _valid(h, odds_key)]
        if len(hs) < 6 or len(hs) != len(r["horses"]):
            continue      # オッズが欠けた馬がいるレースは人気順が作れない
        five = [hn for hn, m in r["marks"].items() if m in CAPTURE_MARKS]
        if len(five) != 5:
            continue
        top3 = {h["hn"] for h in hs if h["y"]}
        pop5 = [h["hn"] for h in sorted(hs, key=lambda h: (h[odds_key], h["hn"]))[:5]]
        honm = next((hn for hn, m in r["marks"].items() if m == "◎"), None)
        fav = pop5[0]
        rows.append((len(set(five) & top3), len(set(pop5) & top3),
                     int(len(set(five) & top3) == 3), int(len(set(pop5) & top3) == 3),
                     int(honm in top3), int(fav in top3), int(honm == fav)))
    return np.array(rows) if rows else np.zeros((0, 7))


def lgb_vs_market(races, odds_key="odds_pt"):
    """(b) LGB (pred_top3_abs、同点は rank_score で割る) と同時刻市場の比較"""
    wl, wm, pooled_y, pooled_l, pooled_m, cap = [], [], [], [], [], []
    for r in races:
        hs = [h for h in r["horses"] if _valid(h, odds_key) and h.get("l3") is not None
              and h.get("rs") is not None]
        if len(hs) < 4 or len(hs) != len(r["horses"]):
            continue
        y = np.array([h["y"] for h in hs])
        if not (0 < y.sum() < len(y)):
            continue
        inv = np.array([1.0 / h[odds_key] for h in hs]); q = inv / inv.sum()
        hm = harville_top3(q)
        l3 = np.array([h["l3"] + 1e-4 * h["rs"] for h in hs])
        wl.append(_auc(y, l3)); wm.append(_auc(y, hm))
        pooled_y += list(y); pooled_l += [h["l3"] for h in hs]; pooled_m += list(-np.log([h[odds_key] for h in hs]))
        cap.append((y[np.argsort(-l3, kind="mergesort")[:5]].sum(), y[np.argsort(-hm, kind="mergesort")[:5]].sum()))
    if not wl:
        return None
    d = np.array(wl) - np.array(wm)
    cap = np.array(cap)
    return {"n": len(wl), "auc_lgb": float(np.mean(wl)), "auc_mkt": float(np.mean(wm)),
            "diff": _boot_mean(d), "pooled": (_auc(pooled_y, pooled_l), _auc(pooled_y, pooled_m)),
            "cap": (float(cap[:, 0].mean()), float(cap[:, 1].mean())), "cap_diff": _boot_mean(cap[:, 0] - cap[:, 1])}


def surface_switch(races, conn, lin_scale):
    """(c) 初ダート・初芝の馬: 実際の3着内 vs 見込み (点数表の見込み / 予測時市場の見込み)"""
    acc = {}
    for r in races:
        if is_jump_race(r["name"]) or r.get("surface") not in ("芝", "ダート"):
            continue
        d = f"{r['d8'][:4]}-{r['d8'][4:6]}-{r['d8'][6:]}"
        hs = [h for h in r["horses"] if h.get("sc") is not None]
        if len(hs) < 4:
            continue
        sc = np.array([h["sc"] for h in hs], float)
        z = sc / 10 * lin_scale; e = np.exp(z - z.max()); p_sc = 3 * e / e.sum()
        okm = all(_valid(h, "odds_pt") for h in hs)
        if okm:
            inv = np.array([1.0 / h["odds_pt"] for h in hs]); p_m = harville_top3(inv / inv.sum())
        for h, ps_, pm_ in zip(hs, p_sc, (p_m if okm else [None] * len(hs))):
            hid = conn.execute("SELECT horse_id FROM results WHERE race_id=? AND horse_number=?",
                               (r["rid"], h["hn"])).fetchone()
            if not hid:
                continue
            past = conn.execute("""SELECT ra.surface, ra.race_name FROM results x JOIN races ra ON x.race_id=ra.race_id
                                   WHERE x.horse_id=? AND x.finish_position>0 AND ra.race_date<?""",
                                (hid[0], d)).fetchall()
            flat = [s for s, nm in past if s in ("芝", "ダート") and not is_jump_race(nm or "")]
            if not flat:
                kind = "初出走"
            elif r["surface"] not in flat:
                kind = "初ダート" if r["surface"] == "ダート" else "初芝"
            else:
                kind = "その他"
            a = acc.setdefault(kind, {"n": 0, "y": 0, "e_sc": 0.0, "e_m": 0.0, "n_m": 0, "y_m": 0,
                                      "marked": 0, "marked_y": 0, "rows": []})
            a["n"] += 1; a["y"] += h["y"]; a["e_sc"] += ps_
            a["rows"].append((h["y"], ps_))
            if pm_ is not None:
                a["n_m"] += 1; a["y_m"] += h["y"]; a["e_m"] += pm_
            if r["marks"].get(h["hn"]) in CAPTURE_MARKS:
                a["marked"] += 1; a["marked_y"] += h["y"]
    return acc


def gate_rates(days, hist):
    """(e) 週末ごとの投資ゲート遮断率 (投稿直前の公開版の予測 × その朝の閾値)"""
    out = []
    for d8 in days:
        pm = ph.load_posted_marks(d8, DATA) or {}
        cut = ph.parse_jst(pm.get("posted_at")) or ph.parse_jst(f"{d8[:4]}-{d8[4:6]}-{d8[6:]} 12:00")
        doc, src = None, "現在の JSON"
        if hist is not None:
            vs = hist.versions(d8, until=cut)
            if vs:
                hist.maybe_prefetch(d8, vs[-1:])
                doc, src = hist.load(d8, vs[-1]["sha"]), f"{vs[-1]['at']:%m/%d %H:%M} の公開版"
                hist.forget(d8)
        if doc is None:
            p = os.path.join(DATA, f"predictions_{d8}.json")
            doc = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None
        if not doc:
            continue
        bl = hist.file_at("docs/data/race_baseline.json", cut) if hist is not None else None
        bsrc = "当日朝の基準表" if bl else "現在の基準表"
        bl = bl or json.load(open(os.path.join(DATA, "race_baseline.json"), encoding="utf-8"))
        th = ((bl.get("bet_gate") or {}).get("thresholds")) or {"top_prob": 0.12, "top3_sum": 0.30}
        r = ph.gate_block_rates(ph.races_of(doc).values(), th, is_ml_out_of_domain)
        if r.get("n"):
            out.append({"date": f"{d8[:4]}-{d8[4:6]}-{d8[6:]}", "src": src, "baseline": bsrc,
                        "thresholds": th, **r})
    return out


def _fmt_ci(t, nd=3, pct=False):
    m, lo, hi = t
    if pct:
        return f"{m*100:+.1f}pt [{lo*100:+.1f}, {hi*100:+.1f}]"
    return f"{m:+.{nd}f} [{lo:+.{nd}f}, {hi:+.{nd}f}]"


def report(target_days, conn=None, hist=None, since=SCORECARD_ERA, all_days=None, out=print):
    """週次レポートに出す「予測時点の市場との比較」。"""
    import pickle
    conn = conn or sqlite3.connect(os.path.join(ROOT, "keiba.db"))
    if hist is None:
        # CI の checkout は shallow なので、履歴は feature_parity / weekly_report が作る
        # 部分クローン (RACEDAY_HISTORY_REPO) から読む。無ければこのリポジトリ。
        h = ph.History(os.environ.get("RACEDAY_HISTORY_REPO") or ROOT,
                       cache_dir=os.environ.get("PRED_HISTORY_CACHE") or DEFAULT_CACHE)
        hist = h if h.available() else None
    all_days = all_days or sorted(os.path.basename(p)[13:21]
                                  for p in glob.glob(os.path.join(DATA, "posted_marks_2*.json")))
    cum_days = [d for d in all_days if d >= since]
    # #169: shallow な履歴の境界より前 (と境界の翌日まで) は、予測が最初に現れた版を
    # 取り違えるので git を使わない。黙って確定寄りのオッズを「予測時」と呼ばないための線引き。
    hist_from, hist_note = None, ""
    if hist is not None:
        b = hist.shallow_boundary()
        if b is not None:
            hist_from = (b + timedelta(days=2)).strftime("%Y%m%d")
            hist_note = (f"  ⚠️ 予測 JSON の履歴は {b:%m/%d %H:%M} 以降しか無い (shallow) → "
                         f"{hist_from} より前の日は予測時オッズ不明として扱う")
    races, meta = collect(sorted(set(cum_days) | set(target_days)), conn, hist, hist_from=hist_from)
    wk = [r for r in races if r["d8"] in target_days]
    cum = [r for r in races if r["d8"] in cum_days]
    srcs = {}
    for m in meta:
        if m["d8"] in target_days:
            for k, v in m["src"].items():
                srcs[k] = srcs.get(k, 0) + v
    out("\n=== 予測時点の市場との比較 (#169) ===")
    out(f"  予測時オッズの出どころ (今週): " + (" / ".join(f"{k} {v}R" for k, v in srcs.items()) or "なし"))
    if hist is None:
        out("  ⚠️ 予測 JSON の履歴が読めない → 予測時オッズは不明 ((a) 予測時・(b) は出ない)")
    elif hist_note:
        out(hist_note)
    out("  ※ 確定オッズで測った val AUC・バックテスト回収率162% はモデルの価値の根拠に使わない "
        "(本番は朝の薄いオッズで予測している)")

    # (a)
    for lab, rs_ in (("今週", wk), (f"{since[4:6]}/{since[6:]}〜累計", cum)):
        for okey, olab in (("odds_pt", "予測時"), ("odds_post", "投稿時")):
            A = capture_vs_popularity(rs_, okey)
            if not len(A):
                continue
            dd = _boot_mean(A[:, 0] - A[:, 1]); df = _boot_mean(A[:, 2] - A[:, 3])
            out(f"  (a) [{lab}・{olab}の人気] 印5頭 vs 人気上位5頭 ({len(A)}R): 3着内 {A[:,0].mean():.3f} vs "
                f"{A[:,1].mean():.3f}頭 差 {_fmt_ci(dd)} / 3頭とも {A[:,2].mean():.1%} vs {A[:,3].mean():.1%} "
                f"差 {_fmt_ci(df, pct=True)} / ◎3着内 {A[:,4].mean():.1%} vs 1人気 {A[:,5].mean():.1%} "
                f"(◎=1人気 {A[:,6].mean():.0%})")
    out("      ※ 印は人気薄を選ぶぶん捕捉が下がる (#147 の製品判断の代償)。差が拡大していないかを見る")

    # (b)
    for lab, rs_ in (("今週", wk), (f"{since[4:6]}/{since[6:]}〜累計", cum)):
        b = lgb_vs_market(rs_, "odds_pt")
        if not b:
            continue
        out(f"  (b) [{lab}] LGB vs 同時刻の市場 ({b['n']}R、予測時オッズ): レース内複勝AUC "
            f"{b['auc_lgb']:.4f} vs {b['auc_mkt']:.4f} 差 {_fmt_ci(b['diff'], 4)} / 上位5の捕捉 "
            f"{b['cap'][0]:.3f} vs {b['cap'][1]:.3f} 差 {_fmt_ci(b['cap_diff'])} / pooled AUC "
            f"{b['pooled'][0]:.4f} vs {b['pooled'][1]:.4f}")
    out("      ※ 差が0をまたぐ = LGB は同じ時刻の人気とほぼ同じ。☆/注 を「オッズ以外で選んだ妙味」と説明しない根拠")

    # (c)
    try:
        lin = float(pickle.load(open(os.path.join(ROOT, "models", "scorecard.pkl"), "rb")).get("lin_scale") or 0.5)
    except Exception:
        lin = 0.5
    v3 = [r for r in races if r["d8"] >= V3_ERA]
    for lab, rs_ in (("今週", wk), (f"v3 ({V3_ERA[4:6]}/{V3_ERA[6:]}〜) 累計", v3)):
        acc = surface_switch(rs_, conn, lin)
        parts = []
        for k in ("初ダート", "初芝"):
            a = acc.get(k)
            if not a or not a["n"]:
                continue
            ys = np.array([x[0] for x in a["rows"]]); es = np.array([x[1] for x in a["rows"]])
            rng = np.random.default_rng(169)
            bs = []
            for _ in range(4000):
                i = rng.integers(0, len(ys), len(ys)); bs.append(ys[i].sum() / max(es[i].sum(), 1e-9))
            mkt = f" / 予測時市場の見込み {a['e_m']:.1f} ({a['y_m']/a['e_m']:.2f})" if a["n_m"] and a["e_m"] else ""
            parts.append(f"{k} {a['n']}頭: 実際 {a['y']} / 点数表の見込み {a['e_sc']:.1f} "
                         f"(実際/見込み {a['y']/a['e_sc']:.2f} [{np.percentile(bs,2.5):.2f}, {np.percentile(bs,97.5):.2f}])"
                         f"{mkt} / 印5頭に入った {a['marked']}頭中 3着内 {a['marked_y']}")
        if parts:
            out(f"  (c) [{lab}] " + " | ".join(parts))
    out("      ※ #168 で初ダートの見込みは過大と分かっている。数百頭たまるまで判定しない")

    # (e)
    gates = gate_rates(sorted(target_days), hist)
    for g in gates:
        out(f"  (e) {g['date']} 投資ゲート遮断率 平場{g['n']}R ({g['src']}・{g['baseline']} "
            f"top_prob≥{g['thresholds']['top_prob']} / top3_sum≥{g['thresholds']['top3_sum']}): "
            f"top_prob {g['top_prob']:.0%} / top3_sum {g['top3_sum']:.0%} (目標 30% / 25%)")
    msg, off = ph.gate_verdict(gates)
    if msg:
        out(("  ❌ " if off else "      ") + msg)
    return {"week": wk, "cum": cum, "meta": meta, "gates": gates}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", required=True, help="対象日 YYYYMMDD")
    ap.add_argument("--since", default=SCORECARD_ERA)
    ap.add_argument("--db", default=os.path.join(ROOT, "keiba.db"))
    a = ap.parse_args()
    report(a.days, conn=sqlite3.connect(a.db), since=a.since)
