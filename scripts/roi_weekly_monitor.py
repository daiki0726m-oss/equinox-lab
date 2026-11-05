#!/usr/bin/env python3
"""週次 ROI ローリングモニタ — 異常配当週検出 + trend 可視化

#26 教訓 (単日支配バイアス) を週次集計に拡張。短期 backtest の絶対値 ROI が
当てにならないので、週次の trend を追跡して以下を出力:

#169: 母集団を「本番が実際に金額を乗せた買い目」(predictions_YYYYMMDD.json の all_bets) と
「投稿時点の凍結印・オッズ」(posted_marks) に変えた。旧版は cache の上書き後オッズと
模擬の買い目で、本番とは別のレースを測っていた (collect_weekly_stats の docstring 参照)。

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
import glob
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def get_week_start(date_str):
    """日付 (YYYY-MM-DD) → 月曜始まりの週 ID (YYYY-MM-DD)"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    monday = d - timedelta(days=d.weekday())
    return monday.strftime("%Y-%m-%d")


def _segment():
    return {"spend": 0, "return": 0, "hits": 0, "races": 0}


def collect_weekly_stats(conn, weeks_back, data_dir=None, today=None):
    """過去 N 週の開催日を、**本番が実際に金額を乗せた買い目** で週単位に集計する (#169)。

    #169 で何を直したか
    -------------------
    旧版は predictions_cache を DB から読み、(1) レースの区分 (band・注のオッズ帯) を
    cache の odds_win で判定し、(2) 買い目は「◎軸×印相手を一律100円」の模擬で採点していた。
    ところが cache のオッズは refresh_odds が発走直前まで上書きしており (seal が runner の push
    で消えるので #99 の凍結が効かない)、投稿時点のオッズと 0.05 以内で一致したのは
    304R 中 26R (8.6%) しか無かった。その結果、監視が band と数えた 92R と、本番が実際に
    band で張った 56R は 30R しか重ならず、買い方も別物 (監視は印相手10点×100円、本番は
    モデル上位4頭の6点×200円)。trio_focus_band の「要降格レビュー」は 9/14〜11/02 に
    8週連続で出ていたが、本番が実際に張った三連複で採点し直すと 8/15〜11/01 の band は
    56R・96,100円→83,660円 = 87.1% (11/02 時点で再計算) で基準 (ROI<80%) に当たらない。
    **時点のずれた模擬計算の誤警報** だった (反省の検証では 85.5% [58, 115] — ワイド込み)。

    いまの母集団
    ------------
    - 買い目: docs/data/predictions_YYYYMMDD.json の all_bets のうち amount>0 のライン
      (#152 の weekly_scorecard と同じ正本・同じ読み方。should_bet=0 のレースは全ライン0)。
    - band: 本番が実際に「三連複を2倍額」で張ったレース (金額署名、weekly_scorecard と同じ判定)。
      band の判断は予測時点のオッズで行われており、その結果は金額に残っている。投稿時点の
      オッズによる区分は投稿したレースにしか無いので、照合用に population に並べて出す。
    - 注のオッズ帯 / 注の複勝率: posted_marks の凍結印と odds_win_at_post (投稿時点)。
      注の単勝は #100 で「観察のみ・賭けない」と決めた枠なので、ここだけは1点100円の仮想。
    - W1/W2 (whitelist) は本番で使っていない観察枠なので従来どおり ◎単勝100円の仮想。

    戻り値: (by_week, segments, population)
    """
    from weekly_scorecard import load_bets, classify_structure, score_actual, payouts_of
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from race_utils import is_ml_out_of_domain

    data_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "docs", "data")
    today = today or datetime.now().date()
    start8 = (today - timedelta(weeks=weeks_back)).strftime("%Y%m%d")
    today8 = today.strftime("%Y%m%d")
    days = sorted(os.path.basename(p)[12:20]
                  for p in glob.glob(os.path.join(data_dir, "predictions_2*.json")))
    days = [d for d in days if start8 <= d < today8]

    by_week = defaultdict(lambda: defaultdict(lambda: defaultdict(_segment)))
    segments = {
        "trio_focus_band": _segment(),
        "W1_S_2勝_中距離_単勝": _segment(),
        "W2_函館ダート_単勝": _segment(),
        "chu_mark_複勝": {"n": 0, "top3_hits": 0},
        # #100: 観察のみ (賭けない)。C2 20-50帯 (seed依存で78-102%) と実測132% (n=81)
        # だった 7-20帯 の注単勝を追跡し、n>=300 で再判定する
        "chu_単勝_7-20倍": _segment(),
        "chu_単勝_20-50倍": _segment(),
    }
    pop = {"days": [], "races_with_results": 0, "races_funded": 0, "posted_races": 0,
           "band_funded": 0, "band_by_posted_odds": 0, "band_overlap": 0,
           "band_by_posted_odds_unfunded": 0, "missing_posted_odds": 0}

    for d8 in days:
        try:
            with open(os.path.join(data_dir, f"predictions_{d8}.json"), encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        races = {str(r.get("race_id")): r for _v, rs in (doc.get("venues") or {}).items()
                 for r in rs or [] if r.get("race_id")}
        try:
            with open(os.path.join(data_dir, f"posted_marks_{d8}.json"), encoding="utf-8") as f:
                posted = (json.load(f) or {}).get("races") or {}
        except (OSError, ValueError):
            posted = {}
        bets_map = load_bets(d8)
        race_date = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        week = get_week_start(race_date)
        pop["days"].append(d8)

        for rid, race in races.items():
            fin = {hn: fp for hn, fp in conn.execute(
                "SELECT horse_number, finish_position FROM results WHERE race_id=? AND finish_position>0",
                (rid,))}
            if not fin or 1 not in fin.values():
                continue            # 未確定・中止はまだ採点しない
            pop["races_with_results"] += 1
            top3_set = {h for h, p in fin.items() if p <= 3}
            win_h = next((h for h, p in fin.items() if p == 1), None)
            pay = payouts_of(conn, rid)
            conf = race.get("confidence") or "?"
            race_name = race.get("race_name") or ""

            # ── 本番の実額 (券種別・信頼度別) ──
            lines = bets_map.get(rid, {})
            sc = score_actual(pay, lines)
            if sc["spend"] > 0:
                pop["races_funded"] += 1
            for bt, t in sc["by_type"].items():
                bk = by_week[week][conf][bt]
                bk["races"] += 1
                bk["spend"] += t["spend"]
                bk["return"] += round(t["ret"])
                bk["hits"] += t["hits"]

            # ── (1) trio-focus band: 本番が三連複を2倍額で張ったレース ──
            struct = classify_structure(lines)
            funded_band = struct.startswith("band:") and sc["spend"] > 0
            if funded_band:
                pop["band_funded"] += 1
                t = sc["by_type"].get("三連複") or {}
                seg = segments["trio_focus_band"]
                seg["races"] += 1
                seg["spend"] += t.get("spend", 0)
                seg["return"] += round(t.get("ret", 0))
                if t.get("hits"):
                    seg["hits"] += 1

            # ── 投稿したレース: 投稿時点の凍結印とオッズで区分する ──
            pm = posted.get(rid) or []
            if pm:
                pop["posted_races"] += 1
                ax = next((e for e in pm if e.get("mark") == "◎"), None)
                ax_odds = (ax or {}).get("odds_win_at_post") or 0
                if ax is not None and not ax_odds:
                    pop["missing_posted_odds"] += 1
                if (conf in ("S", "A", "B") and 2.0 <= ax_odds < 3.0
                        and not is_ml_out_of_domain(race_name)):
                    pop["band_by_posted_odds"] += 1
                    pop["band_overlap"] += int(funded_band)
                    pop["band_by_posted_odds_unfunded"] += int(not funded_band)
                chu = next((e for e in pm if e.get("mark") == "注"), None)
                if chu and chu.get("horse_number"):
                    # #100: 注の単勝オッズ帯別トラッキング (観察のみ・投稿時点のオッズで区分)
                    chu_odds = chu.get("odds_win_at_post") or 0
                    band_key = None
                    if 7 <= chu_odds < 20:
                        band_key = "chu_単勝_7-20倍"
                    elif 20 <= chu_odds < 50:
                        band_key = "chu_単勝_20-50倍"
                    if band_key:
                        seg = segments[band_key]
                        seg["races"] += 1
                        seg["spend"] += 100
                        amt = (pay.get(("単勝", str(chu["horse_number"])), 0)
                               if chu["horse_number"] == win_h else 0)
                        seg["return"] += amt
                        if amt > 0:
                            seg["hits"] += 1

            # ── (2) 注印の複勝率: 投稿したレースは凍結印、それ以外は公開 JSON の印 ──
            #     (印の選び方は予測時点で決まり、公開 JSON の印は配信版と同じ — #169 で確認)
            hs = race.get("horses") or []
            chu_hn = (next((e.get("horse_number") for e in pm if e.get("mark") == "注"), None) if pm
                      else next((h.get("horse_number") for h in hs if h.get("mark") == "注"), None))
            if chu_hn:
                segments["chu_mark_複勝"]["n"] += 1
                if chu_hn in top3_set:
                    segments["chu_mark_複勝"]["top3_hits"] += 1

            # ── (3) WHITELIST (観察のみ): ◎単勝 100円 ──
            ax_hn = (next((e.get("horse_number") for e in pm if e.get("mark") == "◎"), None) if pm
                     else next((h.get("horse_number") for h in hs if h.get("mark") == "◎"), None))
            if ax_hn:
                dist = race.get("distance") or 0
                w1 = (conf == "S" and "2勝" in race_name and 1800 <= dist <= 2199)
                w2 = ((race.get("venue") or "") == "函館" and (race.get("surface") or "") == "ダート")
                for seg_key, hit_flag in (("W1_S_2勝_中距離_単勝", w1), ("W2_函館ダート_単勝", w2)):
                    if not hit_flag:
                        continue
                    seg = segments[seg_key]
                    seg["races"] += 1
                    seg["spend"] += 100
                    amt = pay.get(("単勝", str(ax_hn)), 0) if ax_hn == win_h else 0
                    seg["return"] += amt
                    if amt > 0:
                        seg["hits"] += 1

    return dict(by_week), segments, pop


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
    pop = report.get('population') or {}
    if pop:
        lines.append(f"母集団 (#169): 本番が金額を乗せた買い目 — 確定 {pop.get('races_with_results', 0)}R 中 "
                     f"投資 {pop.get('races_funded', 0)}R / band は本番が2倍額で張った "
                     f"{pop.get('band_funded', 0)}R (投稿レースを投稿時点のオッズで区分すると "
                     f"{pop.get('band_by_posted_odds', 0)}R、うち一致 {pop.get('band_overlap', 0)}R)")
    lines.append("")
    lines.append("## 週別 ROI (三連複 S+A 集約・実額)")
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
            # #152: 「複勝期待回収135円」は #77 の in-sample 値で、#100 が
            # 「OOS 71.3% / 実運用 複勝76.9%・単勝24.9%」と下方訂正済み。
            # 撤回された数字を毎週ベンチマークとして刷り続けていたので実測値に差し替える。
            lines.append(f"- 注印 複勝率: {chu.get('top3_rate_pct', 0)}% "
                         f"({chu['top3_hits']}/{chu['n']}、設計値 ~11%／"
                         f"複勝回収は OOS 71円・実運用 77円 が実力値 — #77 の135円は撤回済み)")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=12, help="過去 N 週を集計 (default 12)")
    ap.add_argument("--db", default="keiba.db")
    ap.add_argument("--output", default="docs/data/roi_weekly.json",
                    help="JSON 出力先 (default: docs/data/roi_weekly.json)")
    ap.add_argument("--no-write", action="store_true", help="JSON 書き込みをスキップ")
    ap.add_argument("--today", default=None,
                    help="基準日 YYYY-MM-DD (過去の週次 run の再現用。既定は今日)")
    args = ap.parse_args()

    today = (datetime.strptime(args.today, "%Y-%m-%d").date() if args.today
             else datetime.now().date())
    conn = sqlite3.connect(args.db)
    raw, segments, population = collect_weekly_stats(conn, args.weeks, today=today)
    weekly = {w: aggregate_week(wd) for w, wd in raw.items()}
    rolling = compute_rolling_average(weekly, window=4)
    anomalies = detect_anomalies(weekly)
    conn.close()

    # ── セグメント flag 判定 (#95): n>=50 かつ ROI<80% → 降格レビュー ──
    # #169: 「降格」できるのは本番が実際に金額を乗せている枠 (trio_focus_band) だけ。
    # 注の単勝 (#100 で観察のみ・賭けない) と whitelist (USE_WHITELIST は本番で無効) は
    # 降格の対象が存在しないので、旧版のように n>=50 で「要降格」を出すと誤報になる
    # (注20-50倍は 7/14 以降ずっと立っていて、誰も扱わない Issue #116 のノイズになった)。
    # 観察枠は #100 が決めた再判定の標本数 n>=300 に届いた時だけ「再判定」の flag を立てる。
    OBSERVATION = {"W1_S_2勝_中距離_単勝", "W2_函館ダート_単勝", "chu_単勝_7-20倍", "chu_単勝_20-50倍"}
    for key, seg in segments.items():
        if "spend" in seg:
            seg["kind"] = "observation" if key in OBSERVATION else "funded"
        if "spend" in seg and seg["spend"] > 0:
            seg["roi_pct"] = round(100 * seg["return"] / seg["spend"], 1)
            if key in OBSERVATION:
                seg["flag"] = ("🔎 観察枠の再判定 (n>=300 & ROI<80%、賭けていない枠)"
                               if seg["races"] >= 300 and seg["roi_pct"] < 80 else "")
            else:
                seg["flag"] = ("🔻 要降格レビュー (n>=50 & ROI<80%)"
                               if seg["races"] >= 50 and seg["roi_pct"] < 80 else "")
        elif key == "chu_mark_複勝" and seg["n"] > 0:
            seg["top3_rate_pct"] = round(100 * seg["top3_hits"] / seg["n"], 1)

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
        # #169: 何を母集団にしたかを JSON に残す (旧版は cache の上書き後オッズ×模擬買い目で、
        # 本番の band と 30/92R しか重ならない別のレースを測っていた)。
        "population": {
            "bets": "docs/data/predictions_YYYYMMDD.json の all_bets (amount>0 = 本番が金額を乗せたライン)",
            "band": "本番が三連複を2倍額で張ったレース (金額署名 = weekly_scorecard.classify_structure)",
            "chu_odds_band": "posted_marks の注と odds_win_at_post (投稿時点のオッズ)",
            **population,
        },
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
