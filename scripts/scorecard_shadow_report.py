#!/usr/bin/env python3
"""回収型の点数表 (#175) と 3着内の点数表を、同じレースの回収率で並べ続ける。

2026-11-18 に印 (◎○▲△△) を回収型 (models/scorecard_roi.pkl) に切り替えた。予想のたびに
使わなかった方の点数表でも印を選び「影の印」(mark_shadow) として残している (predict.py)。
このスクリプトは、確定したレースについて

  - 回収型の印 (roi) と 3着内の印 (base) のそれぞれから組む7つの買い方
    (単勝◎ / 複勝◎ / ワイド◎-4頭 / 馬連◎-4頭 / 三連複◎軸6点 / 三連複5頭BOX / 三連単◎1着12点)
  - 各買い方を1レース100円に揃えた等額の「7種平均」(切り替えを決めた検証と同じ物差し)

の回収率を出し、レース単位のブートストラップで差の誤差の幅を付ける。どちらが印に使われて
いたかに関係なく「roi − base」で並べるので、base に戻した後も比較は続く。

戻す基準 (切り替え前に決めたもの。CLAUDE.md #175):
  - 確定レースが 300 未満: 判定しない (数週では差の誤差の幅が ±15pt 以上ある)
  - 300 以上で 7種平均の差 (roi − base) の **99.5%** 上限が 0 未満: 🔻 戻す (回収型の方が明らかに悪い)
    → 終了コード 10 (weekly_retrain が Issue にする)。印を戻すのは人が config の flag を base にする
  - 1,700 以上 (約半年) で 99.5% 下限が 0 より上: ✅ 回収型の効果を確認
  - それ以外: 継続 (差は誤差の範囲)。表示する幅は 95%
  99.5% にした理由 (#175 レビュー): 1レースあたりの差は大当たりで大きく右に歪み (歪度 7.9)、毎週見直す
  ので、95% だと真の差が0でも1年で約23% (16週で約16%) 誤って「戻す」が出る。99.5% では 7.2% / 5.4%、
  真の差 −8pt なら1年で 76% 検出 (fx182 のシミュレーション)。差を頭打ちにして判定しない (戻す側に偏る)。

材料: docs/data/predictions_YYYYMMDD.json (全レース、mark / mark_shadow / sc_model) と
keiba.db の payouts (確定払戻)。投稿したレースだけの数字は posted_marks_YYYYMMDD.json の
"shadow" (投稿時点で凍結) から別に出す。

使い方: python3 scripts/scorecard_shadow_report.py [--output docs/data/scorecard_shadow.json]
  終了コード 0 = 継続 / 判定保留 / 材料なし / 戻した後、10 = 戻す基準に当たった、
            11 = 影の印が欠けている (比較の材料が貯まっていない)、3 = 例外で落ちた
  (Python 自体が返す 1/2 と重ならない番号にしてある — 落ちた時に「戻す基準」と取り違えない)
"""
import argparse
import glob
import json
import os
import random
import re
import sys
from itertools import combinations

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from weekly_scorecard import db, payouts_of, _key   # noqa: E402  同じ払戻の照合 (#95 の数値ソート)
REFUND = ("scratched", "excluded")    # 走っていない = その馬を含む買い目は返還 (export の unplaced_status)

SYMS = ["◎", "○", "▲", "△", "△"]
BASKET = ["tan1", "fuku1", "wide4", "umaren4", "trio_axis6", "trio_box10", "trifecta12"]
LABEL = {"tan1": "単勝◎", "fuku1": "複勝◎", "wide4": "ワイド◎-4頭", "umaren4": "馬連◎-4頭",
         "trio_axis6": "三連複◎軸6点", "trio_box10": "三連複5頭BOX", "trifecta12": "三連単◎1着12点"}
MIN_JUDGE = 300
MIN_CONFIRM = 1700


def _combos(name, m):
    a = m[0]
    if name == "tan1":
        return "単勝", [[a]]
    if name == "fuku1":
        return "複勝", [[a]]
    if name == "wide4":
        return "ワイド", [[a, x] for x in m[1:5]]
    if name == "umaren4":
        return "馬連", [[a, x] for x in m[1:5]]
    if name == "trio_axis6":
        return "三連複", [[a] + list(c) for c in combinations(m[1:5], 2)]
    if name == "trio_box10":
        return "三連複", [list(c) for c in combinations(m[:5], 3)]
    if name == "trifecta12":
        return "三連単", [[a, x, y] for x in m[1:5] for y in m[1:5] if x != y]
    raise KeyError(name)


def race_returns(pay, m, gone=frozenset()):
    """各買い方の払戻 (1レース100円に揃えた額)。m = [◎,○,▲,△,△] の馬番。
    gone = 予想の後に取消・除外になった馬 (その馬を含む組み合わせは返還 = 100円戻る)。"""
    out = {}
    for s in BASKET:
        bt, cs = _combos(s, m)
        out[s] = sum(100 if gone.intersection(c) else pay.get((bt, _key(bt, c)), 0) for c in cs) / len(cs)
    out["basket"] = sum(out[s] for s in BASKET) / len(BASKET)
    return out


def _order(entries):
    """[(mark, horse_number)] → 印の順の馬番 (△2頭は馬番順)。5頭そろわなければ None。"""
    hs = []
    for sym in ("◎", "○", "▲"):
        x = [h for mk, h in entries if mk == sym]
        if len(x) != 1:
            return None
        hs.append(int(x[0]))
    tri = sorted(int(h) for mk, h in entries if mk == "△")
    if len(tri) != 2:
        return None
    return hs + tri


COVERAGE = {}     # {"all"/"posted": (印がそろったレース, 影の印もそろったレース)}


def collect_all_races():
    """predictions JSON から {race_id: (date, sc_model, live_marks, shadow_marks)}。"""
    pat = re.compile(r"^predictions_(\d{8})\.json$")
    out = {}
    n_live = set()
    for p in sorted(glob.glob(os.path.join(ROOT, "docs", "data", "predictions_*.json"))):
        mm = pat.match(os.path.basename(p))
        if not mm:
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for _v, rs in (d.get("venues") or {}).items():
            for r in rs:
                hs = r.get("horses") or []
                model = next((h.get("sc_model") for h in hs if h.get("sc_model")), "")
                if model not in ("roi", "base"):
                    continue
                live = _order([(h.get("mark"), h.get("horse_number")) for h in hs])
                shadow = _order([(h.get("mark_shadow"), h.get("horse_number")) for h in hs])
                if live:
                    n_live.add(r["race_id"])
                if live and shadow:
                    out[r["race_id"]] = (mm.group(1), model, live, shadow)
    COVERAGE["all"] = (len(n_live), len(out))
    return out


def collect_posted_races():
    """posted_marks の投稿時点の印 (races) と影の印 (shadow) から同じ形を作る。"""
    pat = re.compile(r"^posted_marks_(\d{8})\.json$")
    out = {}
    for p in sorted(glob.glob(os.path.join(ROOT, "docs", "data", "posted_marks_*.json"))):
        mm = pat.match(os.path.basename(p))
        if not mm:
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for rid, sh in (d.get("shadow") or {}).items():
            model = sh.get("sc_model")
            if model not in ("roi", "base"):
                continue
            live = _order([(x.get("mark"), x.get("horse_number")) for x in (d.get("races") or {}).get(rid, [])])
            shadow = _order([(x.get("mark"), x.get("horse_number")) for x in sh.get("marks") or []])
            if live and shadow:
                out[rid] = (mm.group(1), model, live, shadow)
    return out


def score(races):
    """確定したレースだけ採点。roi 側と base 側の返りを並べた行のリスト。"""
    import sqlite3
    from export_predictions import unplaced_status_map
    rows = []
    with db() as c:
        c.row_factory = sqlite3.Row
        for rid, (day, model, live, shadow) in sorted(races.items()):
            pay = {(r[0], r[1]): r[2] for r in c.execute(
                "SELECT bet_type, combination, payout_amount FROM payouts WHERE race_id=?", (str(rid),))}
            if not any(bt == "単勝" for bt, _ in pay):
                continue          # 未確定・払戻なし
            gone = frozenset(hn for hn, st in unplaced_status_map(c, rid).items() if st in REFUND)
            roi_m, base_m = (live, shadow) if model == "roi" else (shadow, live)
            rows.append({"race_id": rid, "day": day, "live": model,
                         "roi": race_returns(pay, roi_m, gone), "base": race_returns(pay, base_m, gone),
                         "same_axis": roi_m[0] == base_m[0], "refund": bool(gone & set(roi_m + base_m))})
    return rows


def boot_delta(rows, key, n_boot=4000, seed=175):
    """(平均の差, 95%下限, 95%上限, 99.5%下限, 99.5%上限)。レース単位のブートストラップ。"""
    d = [r["roi"][key] - r["base"][key] for r in rows]
    n = len(d)
    mean = sum(d) / n
    rnd = random.Random(seed)
    bs = sorted(sum(d[rnd.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return (mean, bs[int(0.025 * n_boot)], bs[int(0.975 * n_boot) - 1],
            bs[int(0.0025 * n_boot)], bs[int(0.9975 * n_boot) - 1])


def summarize(rows):
    if not rows:
        return None
    n = len(rows)
    res = {"races": n, "days": len({r["day"] for r in rows}),
           "from": min(r["day"] for r in rows), "to": max(r["day"] for r in rows),
           "same_axis_share": round(sum(r["same_axis"] for r in rows) / n, 4), "by": {}}
    for k in ["basket"] + BASKET:
        roi = sum(r["roi"][k] for r in rows) / n
        base = sum(r["base"][k] for r in rows) / n
        dm, lo, hi, lo9, hi9 = boot_delta(rows, k) if n >= 20 else (roi - base, None, None, None, None)
        res["by"][k] = {"roi": round(roi, 1), "base": round(base, 1), "delta": round(dm, 2),
                        "ci": [round(lo, 2), round(hi, 2)] if lo is not None else None,
                        "ci995": [round(lo9, 2), round(hi9, 2)] if lo9 is not None else None}
    res["refunded_races"] = sum(1 for r in rows if r.get("refund"))
    # 1日支配 (#26): 差の合計のうち最大の日が占める割合
    by_day = {}
    for r in rows:
        by_day[r["day"]] = by_day.get(r["day"], 0) + r["roi"]["basket"] - r["base"]["basket"]
    tot = sum(by_day.values())
    if by_day and tot:
        top = max(by_day, key=lambda k: abs(by_day[k]))
        res["top_day"] = {"day": top, "share_of_delta": round(by_day[top] / tot, 3)}
    return res


def verdict(s, live=None, coverage=None):
    """戻す基準の判定。live = いま印に使われている点数表 (None なら config から)。"""
    if live is None:
        try:
            from ml.scorecard import live_model_name
            live = live_model_name()
        except Exception:
            live = "roi"
    if coverage and coverage[0] and coverage[1] < 0.9 * coverage[0]:
        return "degraded", (f"⚠️ 影の印が欠けている: 印がそろった {coverage[0]}R のうち影の印もそろったのは "
                            f"{coverage[1]}R — predict の影の点数表 (models/scorecard.pkl / scorecard_roi.pkl) を確認")
    if not s:
        return "no_data", "確定した比較レースがまだ無い"
    b = s["by"]["basket"]
    n = s["races"]
    if n < MIN_JUDGE or not b["ci"]:
        return "pending", f"確定 {n}R (判定は {MIN_JUDGE}R から)。差 {b['delta']:+.1f}pt は参考"
    lo, hi = b["ci"]
    lo9, hi9 = b["ci995"]
    if hi9 < 0:
        if live == "base":
            return "rolled_back", (f"回収型は引き続き劣後 (差 {b['delta']:+.1f}pt、99.5%の幅 [{lo9:+.1f},{hi9:+.1f}]、"
                                   f"{n}R)。印は base で運用中 — 対応不要")
        return "rollback", (f"🔻 戻す基準に該当: 7種平均の差 {b['delta']:+.1f}pt の 99.5% の幅 [{lo9:+.1f},{hi9:+.1f}] "
                            f"の上限が0未満 (回収型が旧点数表より明らかに悪い) — config/prediction_flags.yml の "
                            f"MARKS_SCORECARD_MODEL を base に戻すか検討")
    if n >= MIN_CONFIRM and lo9 > 0:
        return "confirmed", f"✅ 回収型の効果を確認: 差 {b['delta']:+.1f}pt、99.5%の幅 [{lo9:+.1f},{hi9:+.1f}] ({n}R)"
    return "continue", f"継続: 差 {b['delta']:+.1f}pt [{lo:+.1f},{hi:+.1f}] ({n}R) は誤差の範囲"


def _md(title, s):
    if not s:
        return f"### {title}\n確定した比較レースなし\n"
    lines = [f"### {title} ({s['from']}〜{s['to']}、{s['days']}日・{s['races']}R、◎が同じ {s['same_axis_share']*100:.0f}%)",
             "| 買い方 | 回収型 | 旧点数表 | 差 (95%の幅) |", "|---|---|---|---|"]
    for k in ["basket"] + BASKET:
        v = s["by"][k]
        ci = f" [{v['ci'][0]:+.1f}, {v['ci'][1]:+.1f}]" if v["ci"] else ""
        lines.append(f"| {'7種平均' if k == 'basket' else LABEL[k]} | {v['roi']:.1f}% | {v['base']:.1f}% | "
                     f"{v['delta']:+.1f}{ci} |")
    if s.get("top_day"):
        lines.append(f"\n差が最も大きく動いた日: {s['top_day']['day']} (差の合計の {s['top_day']['share_of_delta']*100:.0f}%)")
    if s.get("refunded_races"):
        lines.append(f"取消・除外で返還があったレース: {s['refunded_races']}R (返還は100円戻りとして計算)")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=None, help="JSON の書き出し先 (例 docs/data/scorecard_shadow.json)")
    a = ap.parse_args()
    all_rows = score(collect_all_races())
    posted_rows = score(collect_posted_races())
    s_all, s_posted = summarize(all_rows), summarize(posted_rows)
    cov = COVERAGE.get("all")
    v, msg = verdict(s_all, coverage=cov)
    print("## 回収型の点数表 vs 旧点数表 (#175、同じレースの回収率)\n")
    print(msg + "\n")
    if cov:
        print(f"影の印がそろったレース: {cov[1]} / 印がそろったレース {cov[0]}\n")
    print(_md("全レース (予想した全レース)", s_all))
    print(_md("投稿したレース (投稿時点で凍結した印)", s_posted))
    if a.output:
        os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
        with open(a.output, "w", encoding="utf-8") as f:
            json.dump({"verdict": v, "message": msg,
                       "rule": {"min_judge": MIN_JUDGE, "min_confirm": MIN_CONFIRM,
                                "rollback": "7種平均の差の99.5%上限<0 (300R以上)",
                                "confirm": "99.5%下限>0 (1,700R以上)"},
                       "coverage": {"live": cov[0], "paired": cov[1]} if cov else None,
                       "all": s_all, "posted": s_posted}, f, ensure_ascii=False, indent=1)
    return {"rollback": 10, "degraded": 11}.get(v, 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        print("⚠️ 比較スクリプトが例外で落ちた (戻す基準の判定ではない)")
        traceback.print_exc(file=sys.stdout)
        sys.exit(3)
