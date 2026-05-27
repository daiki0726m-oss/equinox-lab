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
    """過去 N 週分の予測 cache + results を週単位で集計"""
    today = datetime.now().date()
    start_date = (today - timedelta(weeks=weeks_back)).strftime("%Y-%m-%d")

    cur = conn.cursor()
    cur.execute("""
        SELECT pc.race_id, r.race_date, pc.confidence, pc.predictions_json
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

    for race_id, race_date, conf, preds_json in races:
        try:
            preds = json.loads(preds_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not preds:
            continue
        sorted_p = sorted(preds, key=lambda x: -x.get("pred_win_pct", 0))
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
                key = "-".join(sorted(str(x) for x in [top1, o]))
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
                key = "-".join(sorted(str(x) for x in [top1, o]))
                amt = payouts["ワイド"].get(key, 0)
                bk["return"] += amt
                if amt > 0:
                    bk["hits"] += 1

        # 三連複 ◎軸 (10点)
        bk = by_week[week][conf]["三連複"]
        bk["races"] += 1
        for a, b in combinations(others, 2):
            bk["spend"] += 100
            if {top1, a, b} == top3_set:
                key = "-".join(sorted(str(x) for x in [top1, a, b]))
                amt = payouts["三連複"].get(key, 0)
                bk["return"] += amt
                if amt > 0:
                    bk["hits"] += 1

    return dict(by_week)


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
    raw = collect_weekly_stats(conn, args.weeks)
    weekly = {w: aggregate_week(wd) for w, wd in raw.items()}
    rolling = compute_rolling_average(weekly, window=4)
    anomalies = detect_anomalies(weekly)
    conn.close()

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
