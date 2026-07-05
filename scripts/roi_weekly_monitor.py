#!/usr/bin/env python3
"""週次 ROI ローリングモニタ — 異常配当週検出 + trend 可視化

#26 教訓 (単日支配バイアス) を週次集計に拡張。短期 backtest の絶対値 ROI が
当てにならないので、週次の trend を追跡して以下を出力:

  - 週ごとの ROI (信頼度別 × 券種別)
  - 直近 4 週の rolling 平均
  - 異常週フラグ (ROI > 200% or < 30%)
  - 損益累計

出力:
  - JSON: docs/data/roi_weekly.json (機械可読、ダッシュボード用)
  - stdout: 人間可読サマリ

Usage:
    python3 scripts/roi_weekly_monitor.py [--weeks 12] [--db keiba.db]
    python3 scripts/roi_weekly_monitor.py --weeks 8 --output docs/data/roi_weekly.json
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import combinations


def get_week_start(date_str):
    """日付 (YYYY-MM-DD) → 月曜始まりの週 ID (YYYY-MM-DD)"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    monday = d - timedelta(days=d.weekday())
    return monday.strftime("%Y-%m-%d")


def collect_weekly_stats(conn, weeks_back):
    """過去 N 週分の予測 cache + results を週単位で集計。

    戻り値: (by_week, segments)
      segments = 監視対象セグメントの累積成績 (#95 2026-07-02 で追加):
        - trio_focus_band: ◎2.0-2.9倍 × S/A/B の三連複◎軸 (実装した利益層の追跡)
        - W1/W2: WHITELIST_SEGMENTS の単勝成績 (「n>=50 かつ ROI<80%」で降格 flag)
        - chu_mark: 注印の複勝率 (妙味 longshot の健全性)
    """
    today = datetime.now().date()
    start_date = (today - timedelta(weeks=weeks_back)).strftime("%Y-%m-%d")

    cur = conn.cursor()
    cur.execute("""
        SELECT pc.race_id, r.race_date, pc.confidence, pc.predictions_json,
               r.race_name, r.venue, r.surface, r.distance
        FROM predictions_cache pc
        JOIN races r ON pc.race_id = r.race_id
        WHERE r.race_date >= ?
        ORDER BY r.race_date
    """, (start_date,))
    races = cur.fetchall()

    # 週 × 信頼度 × 券種 別の集計
    by_week = defaultdict(lambda: defaultdict(
        lambda: defaultdict(lambda: {"spend": 0, "return": 0, "hits": 0, "races": 0})
    ))
    segments = {
        "trio_focus_band": {"spend": 0, "return": 0, "hits": 0, "races": 0},
        "W1_S_2勝_中距離_単勝": {"spend": 0, "return": 0, "hits": 0, "races": 0},
        "W2_函館ダート_単勝": {"spend": 0, "return": 0, "hits": 0, "races": 0},
        "chu_mark_複勝": {"n": 0, "top3_hits": 0},
        # #100: 観察のみ (賭けない)。C2 20-50帯 (seed依存で78-102%) と実測132% (n=81)
        # だった 7-20帯 の注単勝を追跡し、n>=300 で再判定する
        "chu_単勝_7-20倍": {"spend": 0, "return": 0, "hits": 0, "races": 0},
        "chu_単勝_20-50倍": {"spend": 0, "return": 0, "hits": 0, "races": 0},
    }

    for race_id, race_date, conf, preds_json, race_name, venue, surface, distance in races:
        try:
            preds = json.loads(preds_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not preds:
            continue
        sorted_p = sorted(preds, key=lambda x: -x.get("pred_win_pct", 0))
        # 軸 = 表示上の ◎、相手 = 印 (○▲△×注) を優先 (#95: 本番の honor 買い目と
        # 同じ構造をシミュレートする。旧実装の pred_win 順相手は実際の買い目と乖離)。
        axis_p = next((p for p in preds if p.get("mark") == "◎"), None)
        _mark_pri = {'○': 1, '▲': 2, '△': 3, '×': 4, '注': 5}
        mark_partners = sorted(
            (p for p in preds if p.get("mark") in _mark_pri),
            key=lambda p: _mark_pri[p["mark"]])
        if axis_p is not None and axis_p.get("horse_number") and len(mark_partners) >= 2:
            axis_hn = axis_p.get("horse_number")
            partner_hns = [p.get("horse_number") for p in mark_partners
                           if p.get("horse_number") and p.get("horse_number") != axis_hn][:5]
            top6 = [axis_hn] + partner_hns
        elif axis_p is not None and axis_p.get("horse_number"):
            others_p = [p for p in sorted_p
                        if p.get("horse_number") != axis_p.get("horse_number")][:5]
            top6 = [axis_p.get("horse_number")] + [p.get("horse_number") for p in others_p]
        else:
            axis_p = sorted_p[0] if sorted_p else None
            top6 = [p.get("horse_number") for p in sorted_p[:6]]
        if not all(top6[:5]):
            continue

        cur.execute("""
            SELECT horse_number, finish_position FROM results
            WHERE race_id=? AND finish_position>0 ORDER BY finish_position
        """, (race_id,))
        results = cur.fetchall()
        if not results:
            continue

        win_h = next((h for h, p in results if p == 1), None)
        p2_h = next((h for h, p in results if p == 2), None)
        p3_h = next((h for h, p in results if p == 3), None)
        top3_set = {win_h, p2_h, p3_h}

        cur.execute("""
            SELECT bet_type, combination, payout_amount FROM payouts WHERE race_id=?
        """, (race_id,))
        payouts = defaultdict(dict)
        for bt, combo, amt in cur.fetchall():
            payouts[bt][combo] = amt

        week = get_week_start(race_date)
        top1 = top6[0]
        others = [h for h in top6[1:6] if h]

        # 馬連流し (5点)
        bk = by_week[week][conf]["馬連"]
        bk["races"] += 1
        for o in others:
            bk["spend"] += 100
            if {top1, o} == {win_h, p2_h}:
                key = "-".join(str(x) for x in sorted([top1, o]))
                amt = payouts["馬連"].get(key, 0)
                bk["return"] += amt
                if amt > 0:
                    bk["hits"] += 1

        # ワイド流し (5点)
        bk = by_week[week][conf]["ワイド"]
        bk["races"] += 1
        for o in others:
            bk["spend"] += 100
            if top1 in top3_set and o in top3_set:
                key = "-".join(str(x) for x in sorted([top1, o]))
                amt = payouts["ワイド"].get(key, 0)
                bk["return"] += amt
                if amt > 0:
                    bk["hits"] += 1

        # 三連複 ◎軸 (10点)
        bk = by_week[week][conf]["三連複"]
        bk["races"] += 1
        trio_spend = trio_return = 0
        for a, b in combinations(others, 2):
            bk["spend"] += 100
            trio_spend += 100
            if {top1, a, b} == top3_set:
                key = "-".join(str(x) for x in sorted([top1, a, b]))
                amt = payouts["三連複"].get(key, 0)
                bk["return"] += amt
                trio_return += amt
                if amt > 0:
                    bk["hits"] += 1

        # ── セグメント監視 (#95 2026-07-02) ──
        axis_odds = (axis_p.get("odds_win") or 0) if axis_p else 0

        # (1) trio-focus band: ◎2.0-2.9倍 × S/A/B → 三連複◎軸の追跡
        # #95 レビュー追補: 本番 band (betting.trio_focus_band) と同じく
        # 未勝利/新馬/障害/ジャンプは除外 (損失層ガードの迂回防止と母集団整合)
        _rn = race_name or ""
        _band_ok = not any(k in _rn for k in ("未勝利", "新馬", "障害", "ジャンプ"))
        if conf in ("S", "A", "B") and 2.0 <= axis_odds < 3.0 and _band_ok:
            seg = segments["trio_focus_band"]
            seg["races"] += 1
            seg["spend"] += trio_spend
            seg["return"] += trio_return
            if trio_return > 0:
                seg["hits"] += 1

        # (2) WHITELIST segments: 単勝◎ 100円 (rolling で生死を監視)
        rn = race_name or ""
        w1 = (conf == "S" and "2勝" in rn and 1800 <= (distance or 0) <= 2199)
        w2 = ((venue or "") == "函館" and (surface or "") == "ダート")
        for seg_key, hit_flag in (("W1_S_2勝_中距離_単勝", w1), ("W2_函館ダート_単勝", w2)):
            if not hit_flag:
                continue
            seg = segments[seg_key]
            seg["races"] += 1
            seg["spend"] += 100
            amt = payouts["単勝"].get(str(top1), 0) if top1 == win_h else 0
            seg["return"] += amt
            if amt > 0:
                seg["hits"] += 1

        # (3) 注印の複勝率 (妙味 longshot 健全性。#100 OOS検証: 複勝~71%/単勝~78% が実力値)
        chu = next((p for p in preds if p.get("mark") == "注"), None)
        if chu and chu.get("horse_number"):
            segments["chu_mark_複勝"]["n"] += 1
            if chu["horse_number"] in top3_set:
                segments["chu_mark_複勝"]["top3_hits"] += 1
            # #100: 注の単勝オッズ帯別トラッキング (観察のみ)
            chu_odds = chu.get("odds_win") or 0
            band_key = None
            if 7 <= chu_odds < 20:
                band_key = "chu_単勝_7-20倍"
            elif 20 <= chu_odds < 50:
                band_key = "chu_単勝_20-50倍"
            if band_key:
                seg = segments[band_key]
                seg["races"] += 1
                seg["spend"] += 100
                amt = payouts["単勝"].get(str(chu["horse_number"]), 0) if chu["horse_number"] == win_h else 0
                seg["return"] += amt
                if amt > 0:
                    seg["hits"] += 1

    return dict(by_week), segments


def aggregate_week(week_data):
    """週 1 つ分の (信頼度別 × 券種別) → ROI を計算"""
    out = {"by_tier": {}, "overall": {}}
    overall_acc = defaultdict(lambda: {"spend": 0, "return": 0, "hits": 0})

    for tier in ["S", "A", "B", "C", "D"]:
        if tier not in week_data:
            continue
        tier_data = week_data[tier]
        out["by_tier"][tier] = {}
        for bt, d in tier_data.items():
            spend = d["spend"]
            ret = d["return"]
            roi = round(100 * ret / spend, 1) if spend else 0
            out["by_tier"][tier][bt] = {
                "spend": spend, "return": ret, "hits": d["hits"],
                "races": d["races"], "roi_pct": roi
            }
            overall_acc[bt]["spend"] += spend
            overall_acc[bt]["return"] += ret
            overall_acc[bt]["hits"] += d["hits"]

    for bt, d in overall_acc.items():
        roi = round(100 * d["return"] / d["spend"], 1) if d["spend"] else 0
        out["overall"][bt] = {**d, "roi_pct": roi}

    return out


def compute_rolling_average(weekly, window=4):
    """直近 N 週の rolling 平均 ROI を計算"""
    weeks = sorted(weekly.keys())
    rolling = {}
    for i, w in enumerate(weeks):
        window_weeks = weeks[max(0, i - window + 1):i + 1]
        agg = defaultdict(lambda: {"spend": 0, "return": 0})
        for ww in window_weeks:
            for bt, d in weekly[ww]["overall"].items():
                agg[bt]["spend"] += d["spend"]
                agg[bt]["return"] += d["return"]
        rolling[w] = {
            bt: {
                "spend": d["spend"], "return": d["return"],
                "roi_pct": round(100 * d["return"] / d["spend"], 1) if d["spend"] else 0,
                "window_weeks": len(window_weeks)
            }
            for bt, d in agg.items()
        }
    return rolling


def detect_anomalies(weekly):
    """異常週検出: ROI > 200% or < 30% (S/A 集約) なら flag"""
    anomalies = []
    for w in sorted(weekly.keys()):
        wk = weekly[w]
        if not wk.get("overall"):
            continue
        # S+A 集約の三連複 ROI を見る
        sa_spend = 0
        sa_return = 0
        for tier in ("S", "A"):
            if tier in wk["by_tier"] and "三連複" in wk["by_tier"][tier]:
                sa_spend += wk["by_tier"][tier]["三連複"]["spend"]
                sa_return += wk["by_tier"][tier]["三連複"]["return"]
        if sa_spend < 1000:  # 10R 未満は無視
            continue
        roi = 100 * sa_return / sa_spend
        if roi > 200:
            anomalies.append({"week": w, "tier": "S+A", "bet_type": "三連複",
                              "roi_pct": round(roi, 1), "flag": "🚀 異常高配当"})
        elif roi < 30:
            anomalies.append({"week": w, "tier": "S+A", "bet_type": "三連複",
                              "roi_pct": round(roi, 1), "flag": "🔻 異常低 ROI"})
    return anomalies


def format_markdown(report):
    """人間可読サマリを stdout に出力"""
    lines = []
    lines.append(f"# ROI 週次モニタ — 期間: {report['period']['from']} 〜 {report['period']['to']}")
    lines.append("")
    lines.append(f"対象週数: {len(report['weekly'])} 週 / 異常週: {len(report['anomalies'])} 週")
    lines.append("")
    lines.append("## 週別 ROI (三連複 S+A 集約)")
    lines.append("")
    lines.append("| 週 | S races | A races | S+A spend | S+A return | ROI% | flag |")
    lines.append("|---|---|---|---|---|---|---|")

    for w in sorted(report['weekly'].keys()):
        wk = report['weekly'][w]
        s_n = wk['by_tier'].get('S', {}).get('三連複', {}).get('races', 0)
        a_n = wk['by_tier'].get('A', {}).get('三連複', {}).get('races', 0)
        sa_s = (wk['by_tier'].get('S', {}).get('三連複', {}).get('spend', 0) +
                wk['by_tier'].get('A', {}).get('三連複', {}).get('spend', 0))
        sa_r = (wk['by_tier'].get('S', {}).get('三連複', {}).get('return', 0) +
                wk['by_tier'].get('A', {}).get('三連複', {}).get('return', 0))
        roi = round(100 * sa_r / sa_s, 1) if sa_s else 0
        anom = next((a['flag'] for a in report['anomalies'] if a['week'] == w), '')
        lines.append(f"| {w} | {s_n} | {a_n} | {sa_s:,} | {sa_r:,} | {roi}% | {anom} |")

    lines.append("")
    lines.append("## 異常週検出")
    if report['anomalies']:
        for a in report['anomalies']:
            lines.append(f"- {a['week']}: {a['flag']} {a['tier']} {a['bet_type']} ROI {a['roi_pct']}%")
    else:
        lines.append("(なし)")

    lines.append("")
    lines.append("## 4 週 rolling 平均 (三連複)")
    lines.append("")
    lines.append("| 週 | spend | return | rolling ROI% |")
    lines.append("|---|---|---|---|")
    for w in sorted(report['rolling'].keys()):
        d = report['rolling'][w].get('三連複', {})
        lines.append(f"| {w} | {d.get('spend', 0):,} | {d.get('return', 0):,} | {d.get('roi_pct', 0)}% |")

    # ── セグメント監視 (#95) ──
    segs = report.get('segments') or {}
    if segs:
        lines.append("")
        lines.append("## セグメント監視 (#95: band / whitelist / 注)")
        lines.append("")
        lines.append("| segment | races | spend | return | ROI% | flag |")
        lines.append("|---|---|---|---|---|---|")
        for key, seg in segs.items():
            if "spend" in seg:
                lines.append(f"| {key} | {seg.get('races', 0)} | {seg.get('spend', 0):,} "
                             f"| {seg.get('return', 0):,} | {seg.get('roi_pct', 0)}% "
                             f"| {seg.get('flag', '')} |")
        chu = segs.get("chu_mark_複勝", {})
        if chu.get("n"):
            lines.append("")
            lines.append(f"- 注印 複勝率: {chu.get('top3_rate_pct', 0)}% "
                         f"({chu['top3_hits']}/{chu['n']}、設計値 ~11% / 複勝期待回収135円)")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=12, help="過去 N 週を集計 (default 12)")
    ap.add_argument("--db", default="keiba.db")
    ap.add_argument("--output", default="docs/data/roi_weekly.json",
                    help="JSON 出力先 (default: docs/data/roi_weekly.json)")
    ap.add_argument("--no-write", action="store_true", help="JSON 書き込みをスキップ")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    raw, segments = collect_weekly_stats(conn, args.weeks)
    weekly = {w: aggregate_week(wd) for w, wd in raw.items()}
    rolling = compute_rolling_average(weekly, window=4)
    anomalies = detect_anomalies(weekly)
    conn.close()

    # ── セグメント flag 判定 (#95): n>=50 かつ ROI<80% → 降格レビュー ──
    for key, seg in segments.items():
        if "spend" in seg and seg["spend"] > 0:
            seg["roi_pct"] = round(100 * seg["return"] / seg["spend"], 1)
            seg["flag"] = ("🔻 要降格レビュー (n>=50 & ROI<80%)"
                           if seg["races"] >= 50 and seg["roi_pct"] < 80 else "")
        elif key == "chu_mark_複勝" and seg["n"] > 0:
            seg["top3_rate_pct"] = round(100 * seg["top3_hits"] / seg["n"], 1)

    today = datetime.now().date()
    report = {
        "generated_at": datetime.now().isoformat(),
        "period": {
            "from": (today - timedelta(weeks=args.weeks)).isoformat(),
            "to": today.isoformat(),
        },
        "weekly": weekly,
        "rolling_4w": rolling,
        "anomalies": anomalies,
        "segments": segments,
    }

    # JSON 書き込み
    if not args.no_write and args.output:
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"📝 JSON 出力: {args.output}", file=sys.stderr)

    # 人間可読サマリ
    print(format_markdown({**report, "rolling": rolling}))


if __name__ == "__main__":
    main()
