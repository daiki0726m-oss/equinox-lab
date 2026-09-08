#!/usr/bin/env python3
"""レース条件ごとの「実測値」基準表を作る (#151)

なぜ必要か
----------
これまで読者に出していたのは「信頼度S(鉄板級)」という **ラベル1個** だった。
検証 (#151) の結果、そのラベルは

  - ◎を選ぶモデル (点数表) と、信頼度を計算するモデル (ML勝率) が別物で
    連動していない (◎が「AI勝率1位」だった割合は 75% → 42% に低下)
  - 1番人気オッズ+頭数+クラス に足しても予測力の増分は AUC +0.0009 (p=0.455)
    = 「1番人気のオッズ」の劣化した言い換えでしかない

と判明した。ラベルを直すのではなく、**そのレース条件で実際に何が起きたかの
実測値** を出す方が、読者 (印を見て自分で買い目を組む人) の役に立つ。

出すもの (すべて実測、モデルの予言ではない)
--------------------------------------------
  1. 印5頭 / 印7頭で3着内3頭が全部揃った割合 → 買い目の点数設計に直結
  2. 三連複の配当中央値 / 1万円超の割合     → 期待値の感覚

条件は **出走頭数** で切る。#151 の検証で、捕捉率を動かしている実体は
頭数であり (能力スコアのシェアは 5/頭数 との相関 +0.741、機械的部分を引くと
AUC 0.5005 = 情報ゼロ)、他のオッズ非依存な候補はどれも頭数を超えなかったため。
「当たり前の方向」ではあるが **大きさは頭で計算できない** ので価値がある。

副産物: 信頼度の閾値を分布から決める
------------------------------------
信頼度ラベル自体は読者には出さなくなるが、投資判定 (should_bet の C/D 遮断)
が内部で使い続ける。#147 で ◎ が点数表由来になった瞬間に ◎表示勝率の分布が
下にずれ、固定閾値 (S≥45/A≥30/B≥20/C≥13) とペアが外れて **D が 11% → 39% に
激増** した (#36/#120 で二度書いた「閾値と分布はペア」の三度目の再発)。
ここで **現行の印ロジックが実際に出している分布の分位** から閾値を作り、
モデルや印ロジックが変わっても自動で追従するようにする。

実行:
    python3 scripts/build_race_baseline.py            # docs/data/race_baseline.json を更新
    python3 scripts/build_race_baseline.py --dry-run  # 書かずに表示だけ
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from race_utils import is_ml_out_of_domain  # noqa: E402

JST = timezone(timedelta(hours=9))
OUT = os.path.join(ROOT, "docs", "data", "race_baseline.json")

# 出走頭数の区切り。JRA は 5〜18頭。境界は [min, max] の閉区間。
BUCKETS = [
    (0, 10, "10頭以下"),
    (11, 12, "11-12頭"),
    (13, 14, "13-14頭"),
    (15, 16, "15-16頭"),
    (17, 99, "17頭以上"),
]

# 信頼度の目標構成比。#147 (点数表ON) の直前、2026-08 の実分布を採用。
# 「どの程度の割合をS/A/B/C/Dと呼ぶか」は製品の決めごとなので固定し、
# その割合を満たす閾値だけを実分布から毎回引き直す。
TARGET_MIX = {"S": 0.18, "A": 0.24, "B": 0.29, "C": 0.18, "D": 0.11}
# 30 は「精度」ではなく「正しい分布に乗る」ことを優先した下限。
# 印ロジックを変えた直後は現行ロジックの標本が数十件しか無いが、
# 旧ロジック混じりの大標本から引くと中心がずれたまま固定される (それが #147 の失敗)。
# 標本が薄い間は分位が粗いだけで、中心は正しい方に寄る。週次で引き直されるので収束する。
MIN_SAMPLE_FOR_THRESHOLDS = 30


def _bucket_of(n):
    for lo, hi, lab in BUCKETS:
        if lo <= n <= hi:
            return lab
    return None


# レースの層。ML の学習範囲外 (未勝利・新馬・障害) は捕捉率も配当も
# 平場とはっきり違う (実測: 15-16頭で 36.4% vs 20.9%) ので必ず分ける。
# 混ぜて出すと、未勝利戦に平場の数字を当ててしまい嘘になる。
GROUPS = ("flat", "maiden")


def _group_of(race_name):
    return "maiden" if is_ml_out_of_domain(race_name or "") else "flat"


def build_capture(conn):
    """印が3着内をどれだけ捕まえたかの実測 (凍結印 × 確定着順)。層別。"""
    rows = conn.execute("""
        SELECT pc.race_id, pc.predictions_json, g.race_name, g.race_date
        FROM predictions_cache pc JOIN races g ON pc.race_id = g.race_id
        ORDER BY g.race_date
    """).fetchall()
    acc = {g: {lab: {"n": 0, "c5": 0, "c7": 0, "got": 0} for _, _, lab in BUCKETS}
           for g in GROUPS}
    dates = []
    for r in rows:
        grp = _group_of(r["race_name"])
        res = {x["horse_number"]: x["finish_position"] for x in conn.execute(
            "SELECT horse_number, finish_position FROM results WHERE race_id = ?", (r["race_id"],))}
        if not any(v == 1 for v in res.values()):
            continue
        top3 = {h for h, f in res.items() if 0 < f <= 3}
        if len(top3) != 3:            # 同着で4頭以上になる場合は対象外
            continue
        try:
            preds = json.loads(r["predictions_json"] or "[]")
        except (ValueError, TypeError):
            continue
        cap5 = {p["horse_number"] for p in preds if p.get("mark") in ("◎", "○", "▲", "△", "×")}
        if len(cap5) < 4:             # 印が揃っていないレースは除く
            continue
        cap7 = {p["horse_number"] for p in preds if p.get("mark")}
        b = _bucket_of(len(preds))
        if not b:
            continue
        a = acc[grp][b]
        a["n"] += 1
        a["got"] += len(cap5 & top3)
        a["c5"] += 1 if len(cap5 & top3) == 3 else 0
        a["c7"] += 1 if len(cap7 & top3) == 3 else 0
        dates.append(r["race_date"])
    groups = {}
    for g in GROUPS:
        out = []
        for lo, hi, lab in BUCKETS:
            a = acc[g][lab]
            if a["n"] < 20:           # 母数が薄い区分は数字を出さない (誤解を招くため)
                out.append({"label": lab, "min": lo, "max": hi, "n": a["n"]})
                continue
            out.append({
                "label": lab, "min": lo, "max": hi, "n": a["n"],
                "cap5": round(a["c5"] / a["n"], 4),
                "cap7": round(a["c7"] / a["n"], 4),
                "avg_captured": round(a["got"] / a["n"], 3),
            })
        groups[g] = {"buckets": out}
    period = f"{min(dates)}〜{max(dates)}" if dates else "-"
    return {"groups": groups, "period": period,
            "source": "投稿時に凍結した印 × 確定着順 (平場 / 未勝利・新馬・障害 を分けて集計)"}


def build_payout(conn):
    """三連複の配当実測 (全期間)。捕捉率と同じ層で分ける。"""
    rows = conn.execute("""
        SELECT p.payout_amount a, g.race_name,
               (SELECT COUNT(*) FROM results r WHERE r.race_id = g.race_id) nh
        FROM payouts p JOIN races g ON p.race_id = g.race_id
        WHERE p.bet_type = '三連複' AND p.payout_amount > 0
    """).fetchall()
    acc = {g: {lab: [] for _, _, lab in BUCKETS} for g in GROUPS}
    for x in rows:
        lab = _bucket_of(x["nh"])
        if lab:
            acc[_group_of(x["race_name"])][lab].append(x["a"])
    groups = {}
    for g in GROUPS:
        out = []
        for lo, hi, lab in BUCKETS:
            v = sorted(acc[g][lab])
            if len(v) < 100:
                out.append({"label": lab, "min": lo, "max": hi, "n": len(v)})
                continue
            out.append({
                "label": lab, "min": lo, "max": hi, "n": len(v),
                "trio_median": int(v[len(v) // 2]),
                "trio_over10k": round(sum(1 for x in v if x >= 10000) / len(v), 4),
            })
        groups[g] = {"buckets": out}
    r = conn.execute("""SELECT MIN(g.race_date) a, MAX(g.race_date) b FROM payouts p
                        JOIN races g ON p.race_id = g.race_id WHERE p.bet_type='三連複'""").fetchone()
    return {"groups": groups, "period": f"{r['a']}〜{r['b']}",
            "source": "確定配当 (全レース、平場 / 未勝利・新馬・障害 を分けて集計)"}


def build_confidence_thresholds(conn):
    """現行の印ロジックが出している ◎表示勝率の分布から、目標構成比を満たす閾値を引く。

    固定閾値は印ロジックやモデルが変わるたびに陳腐化する (#36/#120/#147)。
    分位で決めれば、次に印の選び方を変えても構成比は保たれる。
    """
    # 現行ロジック = 点数表 (sc_points が入っている) のレースを優先
    ws, used = [], None
    for cond, label in (("AND pc.predictions_json LIKE '%\"sc_points\"%'", "点数表 (現行)"),
                        ("", "直近全体 (点数表以前を含む)")):
        ws = []
        for r in conn.execute(f"""
            SELECT pc.predictions_json, g.race_name FROM predictions_cache pc
            JOIN races g ON pc.race_id = g.race_id
            WHERE 1=1 {cond} ORDER BY g.race_date DESC LIMIT 400
        """):
            if is_ml_out_of_domain(r["race_name"] or ""):
                continue
            try:
                preds = json.loads(r["predictions_json"] or "[]")
            except (ValueError, TypeError):
                continue
            ax = next((p for p in preds if p.get("mark") == "◎"), None)
            if not ax:
                continue
            w = ax.get("pred_win_display_pct")
            # sc_points 条件では「点数表が実際に効いた」レースだけを見る
            if cond and not any((p.get("sc_points") or 0) for p in preds):
                continue
            if w:
                ws.append(float(w))
        used = label
        if len(ws) >= MIN_SAMPLE_FOR_THRESHOLDS:
            break
    if len(ws) < MIN_SAMPLE_FOR_THRESHOLDS:
        return {"available": False, "n": len(ws),
                "note": f"標本 {len(ws)} 件では閾値を引けない (最低 {MIN_SAMPLE_FOR_THRESHOLDS} 件)"}
    ws.sort()
    n = len(ws)
    th, cum = {}, 0.0
    for g in ("S", "A", "B", "C"):
        cum += TARGET_MIX[g]
        idx = max(0, min(n - 1, int(round((1.0 - cum) * n))))
        th[g] = round(ws[idx], 1)
    # 単調性を保証 (標本が薄いと同値が並びうる)
    for a, b in (("S", "A"), ("A", "B"), ("B", "C")):
        if th[a] <= th[b]:
            th[a] = round(th[b] + 0.1, 1)
    return {"available": True, "n": n, "sample": used,
            "target_mix": TARGET_MIX, "thresholds": th}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=os.path.join(ROOT, "keiba.db"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    data = {
        "generated_at": datetime.now(JST).isoformat(),
        "capture": build_capture(conn),
        "payout": build_payout(conn),
        "confidence": build_confidence_thresholds(conn),
    }

    print("📊 レース条件別の実測値")
    for g, gl in (("flat", "平場"), ("maiden", "未勝利・新馬・障害")):
        print(f"  【{gl}】")
        for cb in data["capture"]["groups"][g]["buckets"]:
            pb = next((x for x in data["payout"]["groups"][g]["buckets"]
                       if x["label"] == cb["label"]), {})
            if "cap5" not in cb:
                print(f"    {cb['label']:8s} 母数 {cb['n']:4d} — 少なすぎるため非表示")
                continue
            print(f"    {cb['label']:8s} n={cb['n']:4d}  印5頭 {100*cb['cap5']:5.1f}%  印7頭 {100*cb['cap7']:5.1f}%"
                  f"  三連複中央値 {pb.get('trio_median', 0):7,}円  1万円超 {100*pb.get('trio_over10k', 0):4.1f}%")
    cf = data["confidence"]
    if cf.get("available"):
        print(f"\n🎚 信頼度の閾値 ({cf['sample']}, n={cf['n']}): "
              + " / ".join(f"{k}≥{v}" for k, v in cf["thresholds"].items()))
    else:
        print(f"\n⚠️ 信頼度の閾値: {cf.get('note')} → 従来の固定値を使う")

    if args.dry_run:
        print("\n(--dry-run: 書き込みなし)")
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"\n✅ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
