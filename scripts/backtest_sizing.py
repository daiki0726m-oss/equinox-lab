#!/usr/bin/env python3
"""自信度比例 sizing backtest (#75): selection は市場ベース(現行)のまま、
「捕捉自信度」バンド別に 三連複◎軸 / 6頭ボックス の ROI を測り、
黒字化するバンド(=厚く賭けるべきレース)を特定する。

ユーザー指示: 控除無視・投資→回収。flat単勝でなく、自信度で sizing を変える。

捕捉自信度 = ML本命(top1) softmax 勝率 (#74 で 捕捉率と強く単調: 20%→41% / 40%→92%)。
キャッシュ ~/.cctmp/top3_scored.pkl を使う (無ければ backtest_top3_coverage.py --rebuild)。

実行: python3 scripts/backtest_sizing.py
"""
from __future__ import annotations
import json, os, pickle, sqlite3, sys, time
from collections import defaultdict
from itertools import combinations
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CACHE = Path(os.path.expanduser("~/.cctmp/top3_scored.pkl"))


def load_payouts():
    """三連複 + ワイド の払戻を {(race_id,bet_type,frozenset): payout/100円}。"""
    c = sqlite3.connect(ROOT / "keiba.db")
    trio, wide = {}, {}
    for rid, comb, pay in c.execute(
        "SELECT race_id, combination, payout_amount FROM payouts WHERE bet_type='三連複'"):
        nums = frozenset(int(x) for x in str(comb).split("-") if x.strip().isdigit())
        if len(nums) == 3:
            trio[(rid, nums)] = pay
    for rid, comb, pay in c.execute(
        "SELECT race_id, combination, payout_amount FROM payouts WHERE bet_type='ワイド'"):
        nums = frozenset(int(x) for x in str(comb).split("-") if x.strip().isdigit())
        if len(nums) == 2:
            wide[(rid, nums)] = pay
    c.close()
    return trio, wide


def main():
    t0 = time.time()
    if not CACHE.exists():
        print("❌ キャッシュ無し → 先に scripts/backtest_top3_coverage.py を実行")
        sys.exit(1)
    scored = pickle.load(open(CACHE, "rb"))
    trio, wide = load_payouts()
    print(f"📦 {scored['race_id'].nunique()}レース / 三連複payout {len(trio)}件")

    # バケット定義 (ML本命勝率)。境界は #74 の捕捉率カーブに基づく。
    bands = [(0, .15), (.15, .20), (.20, .25), (.25, .30), (.30, .40), (.40, 1.01)]
    def band_of(p):
        for lo, hi in bands:
            if lo <= p < hi:
                return f"{int(lo*100)}-{int(hi*100)}%"
        return "?"

    # 集計: {bettype: {band: {spend,ret,hit,n}}}, 単日: {bettype: {band: {date:{spend,ret}}}}
    agg = defaultdict(lambda: defaultdict(lambda: {"spend": 0, "ret": 0, "hit": 0, "n": 0}))
    day = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {"spend": 0, "ret": 0})))
    AMT = 100  # 1点100円 (flat。sizing は後段でバンド別倍率を掛けて評価)

    for rid, g in scored.groupby("race_id"):
        if len(g) < 6:
            continue
        date = str(g["race_date"].iloc[0])
        g = g.sort_values("rank_score", ascending=False)
        rs = g["rank_score"].values
        ex = np.exp(rs - rs.max()); p = ex / ex.sum()
        conf = float(p[0])
        bd = band_of(conf)
        marks = [int(h) for h in g.head(6)["horse_number"]]
        axis = marks[0]
        others = marks[1:6]
        actual = frozenset(int(r.horse_number) for r in g.itertuples() if r.finish_position in (1, 2, 3))
        if len(actual) < 3:
            continue

        # 三連複◎軸 5頭流し (10点): (axis, a, b)
        for a, b in combinations(others, 2):
            line = frozenset([axis, a, b])
            agg["trio_axis"][bd]["spend"] += AMT
            day["trio_axis"][bd][date]["spend"] += AMT
            if line == actual:
                pay = trio.get((rid, line), 0)
                agg["trio_axis"][bd]["ret"] += pay; agg["trio_axis"][bd]["hit"] += 1
                day["trio_axis"][bd][date]["ret"] += pay
        agg["trio_axis"][bd]["n"] += 1

        # 三連複 6頭ボックス (20点)
        for combo in combinations(marks, 3):
            line = frozenset(combo)
            agg["trio_box"][bd]["spend"] += AMT
            day["trio_box"][bd][date]["spend"] += AMT
            if line == actual:
                pay = trio.get((rid, line), 0)
                agg["trio_box"][bd]["ret"] += pay; agg["trio_box"][bd]["hit"] += 1
                day["trio_box"][bd][date]["ret"] += pay
        agg["trio_box"][bd]["n"] += 1

        # ワイド◎軸 流し (5点): axis-each
        for o in others:
            line = frozenset([axis, o])
            agg["wide_axis"][bd]["spend"] += AMT
            day["wide_axis"][bd][date]["spend"] += AMT
            if line <= actual:
                pay = wide.get((rid, line), 0)
                agg["wide_axis"][bd]["ret"] += pay; agg["wide_axis"][bd]["hit"] += 1
                day["wide_axis"][bd][date]["ret"] += pay
        agg["wide_axis"][bd]["n"] += 1

    def roi(d):
        return 100 * d["ret"] / d["spend"] if d["spend"] else 0

    out = {}
    for bt in ("trio_axis", "trio_box", "wide_axis"):
        print("\n" + "=" * 76)
        label = {"trio_axis": "三連複◎軸5頭流し(10点)", "trio_box": "三連複6頭ボックス(20点)",
                 "wide_axis": "ワイド◎軸流し(5点)"}[bt]
        print(f"【{label}】 捕捉自信度(ML本命勝率)バンド別 ROI")
        print(f"{'バンド':10s} {'レース':>6s} {'的中率':>6s} {'ROI':>6s} {'平均配当':>7s} {'単日除外':>7s}")
        out[bt] = {}
        for lo, hi in bands:
            bd = f"{int(lo*100)}-{int(hi*100)}%"
            d = agg[bt][bd]
            if not d["spend"]:
                continue
            hr = 100 * d["hit"] / d["n"] if d["n"] else 0
            avg_pay = d["ret"] / d["hit"] if d["hit"] else 0
            # 単日支配除外
            dd = day[bt][bd]
            best = max(dd, key=lambda x: dd[x]["ret"] - dd[x]["spend"]) if dd else None
            exs = d["spend"] - (dd[best]["spend"] if best else 0)
            exr = d["ret"] - (dd[best]["ret"] if best else 0)
            ex_roi = 100 * exr / exs if exs else 0
            flag = " 🟢" if roi(d) >= 100 and ex_roi >= 100 else ""
            print(f"{bd:10s} {d['n']:>6d} {hr:>5.0f}% {roi(d):>5.0f}% {avg_pay:>6.0f}円 {ex_roi:>6.0f}%{flag}")
            out[bt][bd] = {"races": d["n"], "hit_rate": round(hr, 1), "roi": round(roi(d), 1),
                           "avg_payout": round(avg_pay), "roi_excl_best_day": round(ex_roi, 1)}
    Path(ROOT / "docs/analysis/backtest_sizing.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n💾 docs/analysis/backtest_sizing.json  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
