#!/usr/bin/env python3
"""期待値の実測表を作る (#163)。

    python3 scripts/build_ev_table.py            # docs/data/ev_table.json を更新
    python3 scripts/build_ev_table.py --dry-run  # 書かずに表示だけ

## なぜ AI の「期待値」でなく実測なのか
AI勝率 × オッズ で出した期待値は、実際の回収と合わない (2026-09-29 検証、19エージェント)。
  - 以前のモデルで「期待値1.2超え」になった馬は、AI の見込み 166.6勝に対し実際 53勝。
    市場のオッズから見込まれる 55.9勝とほぼ同じ = AI の上乗せゼロ。
  - 週次の再学習で勝率の尺度 (温度) が週ごとに大きく動くので、「期待値1.3」の意味を
    事前に保証できない (#156 と同じ構造)。
  - AI の3着内確率 × 想定複勝配当 も同様 (期待値1超えの印馬の複勝回収 42-56%)。
正直に出せる期待値は「同じくらいのオッズの馬を過去に100円ずつ買い続けたらどうなったか」だけ。
過去の馬は**確定オッズ**で帯に分け、読者に見える想定オッズからは締切までの動き (投稿印の実測) で換算する。
7/11〜9/27 の投稿印 299レースで答え合わせすると、◎の複勝は
的中 実測170 / 予想168、回収 実測83円 / 予想84円 で一致した。

## 作り方
1. 2020年〜 全馬について、層 (平場 / 未勝利・新馬) × **確定オッズ帯** ごとに
   単勝・複勝の的中率と「的中した時の平均払戻 (100円あたり)」を集計する。障害は対象外。
   7頭以下は複勝が2着までなので別の表にする。4頭以下は複勝が発売されない。
2. 読者に見えるのは投稿時点の「想定オッズ」で、締切までに動く (◎は平均約1割下がる)。
   そこで投稿印の記録 (docs/data/posted_marks_*.json の odds_win_at_post) と確定オッズから
   「確定 ÷ 想定」の分布を、印の役割 × 想定オッズ帯 ごとに分位点で保存する。
   読み出し側 (ev_table.py) は 想定オッズ × 各分位点 → 確定帯 → 表の値 を平均する。
3. 印の役割: ◎○▲△(×) は「捕捉」、☆ は「妙味」、注 は「穴」。
   ○ は 2026-07-13〜08-31 だけ妙味枠 (#110) だったので、その期間の ○ は「妙味」に数える
   (想定オッズで選ばれた馬は締切でオッズが伸びやすく、分布が別物になる)。
   このため同じ想定オッズでも印によって数字は少し変わる (☆・注は伸びやすいぶん的中率が低めに出る)。
4. 7頭以下の表は「複勝が2着までしか払われなかったレース」。発売後の取消で頭数が減っても
   3着まで払ったレースは8頭以上の表に入れる。
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from race_utils import is_jump_race  # noqa: E402

DB = os.path.join(ROOT, "keiba.db")
OUT = os.path.join(ROOT, "docs", "data", "ev_table.json")
MARKS_GLOB = os.path.join(ROOT, "docs", "data", "posted_marks_*.json")
JST = timezone(timedelta(hours=9))

DATE_FROM = "2020-01-01"
# 帯の区切り (確定オッズ)。1倍台前半は複勝の回収が目立って高いので分ける。
EDGES = [1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 20.0, 50.0]
LABELS = ["1.0-1.4", "1.5-1.9", "2.0-2.9", "3.0-4.9", "5.0-9.9", "10-19.9", "20-49.9", "50+"]
SMALL_FIELD = 7          # 7頭以下は複勝2着まで
# 締切までの動きの分布を保存する分位点 (2.5%刻み)
QS = [round(0.025 * i, 3) for i in range(1, 40)]
MIN_DRIFT_N = 25         # これ未満の帯は役割全体の分布で代用
VALUE_TAIKO_FROM, VALUE_TAIKO_TO = "2026-07-13", "2026-08-31"   # #110 → #140


def band_of(o):
    i = int(np.searchsorted(EDGES, float(o), side="right")) - 1
    return LABELS[max(0, min(i, len(LABELS) - 1))]


def role_of(mark, race_date=""):
    if mark == "注":
        return "chu"
    if mark == "☆":
        return "value"
    if mark == "○" and VALUE_TAIKO_FROM <= (race_date or "") <= VALUE_TAIKO_TO:
        return "value"
    return "capture"


def layer_of(race_name):
    """平場 / 未勝利・新馬 / 障害。単勝の回収は層で違う (1.5-1.9倍帯で 85円 vs 74円) ので分ける。
    障害は表を作らない (読み出し側は None を返し、表示しない)。"""
    n = race_name or ""
    if is_jump_race(n):
        return "jump"
    if "未勝利" in n or "新馬" in n:
        return "maiden"
    return "flat"


def load_horses(conn, date_to):
    races = pd.read_sql_query(
        "SELECT race_id, race_date, race_name FROM races WHERE race_date BETWEEN ? AND ?",
        conn, params=(DATE_FROM, date_to))
    races["layer"] = [layer_of(n) for n in races.race_name]
    races = races[races.layer != "jump"]
    res = pd.read_sql_query(
        """SELECT r.race_id, r.horse_number, r.odds, r.popularity, r.finish_position
           FROM results r JOIN races x ON x.race_id = r.race_id
           WHERE x.race_date BETWEEN ? AND ?""", conn, params=(DATE_FROM, date_to))
    pay = pd.read_sql_query(
        """SELECT p.race_id, p.bet_type, p.combination, p.payout_amount
           FROM payouts p JOIN races x ON x.race_id = p.race_id
           WHERE x.race_date BETWEEN ? AND ? AND p.bet_type IN ('単勝','複勝')""",
        conn, params=(DATE_FROM, date_to))
    res = res[res.race_id.isin(set(races.race_id))]
    # 母数に入れるのは「馬券が有効だった馬」。
    #   - 取消・除外は返還なので外す: オッズ 0 / 999倍台のセンチネル / 人気 9999 など。
    #   - 競走中止 (着順0 だがオッズ・人気が有効) は返還されない外れ馬券なので入れる
    #     (#163 レビュー: 旧版は着順0を一律に外しており、1,009頭の負けを数えていなかった)。
    ok_bet = (res.odds > 0) & (res.odds < 999) & res.popularity.between(1, 30)
    res = res[ok_bet & ((res.finish_position > 0) | (res.finish_position == 0))].copy()
    win_races = set(pay[pay.bet_type == "単勝"].race_id)
    res = res[res.race_id.isin(win_races)].copy()   # 払戻が揃っているレースだけ
    pay["hn"] = pd.to_numeric(pay.combination, errors="coerce")
    pay = pay.dropna(subset=["hn"])
    win = pay[pay.bet_type == "単勝"].groupby(["race_id", "hn"]).payout_amount.sum()
    plc_pay = pay[pay.bet_type == "複勝"]
    plc = plc_pay.groupby(["race_id", "hn"]).payout_amount.sum()
    key = list(zip(res.race_id, res.horse_number.astype(float)))
    res["win_pay"] = [float(win.get(k, 0.0)) for k in key]
    res["plc_pay"] = [float(plc.get(k, 0.0)) for k in key]
    res["plc_sold"] = res.race_id.isin(set(plc_pay.race_id))
    res["field"] = res.groupby("race_id").horse_number.transform("count")
    # 「複勝が2着まで」の表に入れるかは、実際に何着まで払い戻したかで決める。
    # 完走頭数で決めると、発売後の取消で7頭に減ったが3着まで払ったレース (39件) が混ざる (#163 レビュー)。
    # 複勝が売られていないレースは頭数で決める (単勝の表だけに効く)。
    n_paid = plc_pay.groupby("race_id").hn.nunique()
    paid = res.race_id.map(n_paid)
    res["small"] = paid.le(2).where(paid.notna(), res.field <= SMALL_FIELD).astype(bool)
    res["band"] = [band_of(o) for o in res.odds]
    res = res.merge(races[["race_id", "layer"]], on="race_id", how="left")
    return res


def build_table(h):
    """層 × (8頭以上 / 7頭以下) × 確定オッズ帯 の的中率と的中時の平均払戻。

    7頭以下は標本が薄い (1セル 36〜400頭) ので、層をまたいでプールした表を両方の層に使う。
    少頭数では「複勝が2着まで」という頭数の効果が層の差より大きい。
    """
    small = _build_rows(h[h.small])
    return {layer: {"big": _build_rows(h[(h.layer == layer) & ~h.small]),
                    "small": small}
            for layer in ("flat", "maiden")}


def _build_rows(sub):
    rows = {}
    for b in LABELS:
        d = sub[sub.band == b]
        if d.empty:
            continue
        won = d.win_pay > 0
        dp = d[d.plc_sold]
        placed = dp.plc_pay > 0
        rows[b] = {
            "n": int(len(d)),
            "win_rate": round(float(won.mean()), 4),
            "win_pay": round(float(d.win_pay[won].mean()), 1) if won.any() else None,
            "n_place": int(len(dp)),
            "place_rate": round(float(placed.mean()), 4) if len(dp) else None,
            "place_pay": round(float(dp.plc_pay[placed].mean()), 1) if placed.any() else None,
        }
    return rows


def load_drift(conn):
    """投稿印の 想定オッズ → 確定オッズ の比。"""
    rows = []
    for f in sorted(glob.glob(MARKS_GLOB)):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        day = str(d.get("date") or "")
        if len(day) != 8:
            continue
        date = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
        for rid, ms in (d.get("races") or {}).items():
            for m in ms or []:
                o = m.get("odds_win_at_post") or 0
                if o and o > 1.0:
                    rows.append((rid, date, m.get("mark"), int(m.get("horse_number") or 0), float(o)))
    if not rows:
        return pd.DataFrame(columns=["role", "band", "ratio"])
    df = pd.DataFrame(rows, columns=["race_id", "date", "mark", "hn", "odds_post"])
    ids = list(df.race_id.unique())
    fin = pd.read_sql_query(
        f"""SELECT race_id, horse_number AS hn, odds AS odds_final, finish_position
            FROM results WHERE race_id IN ({','.join('?' * len(ids))})""",
        conn, params=ids)
    df = df.merge(fin, on=["race_id", "hn"], how="inner")
    # 取消 (オッズ無効) だけ外す。競走中止の馬も締切までのオッズの動きとしては有効。
    df = df[(df.odds_final > 0) & (df.odds_final < 999)].copy()
    df["ratio"] = df.odds_final / df.odds_post
    df["role"] = [role_of(m, d) for m, d in zip(df.mark, df.date)]
    df["band"] = [band_of(o) for o in df.odds_post]
    return df


def build_drift(df):
    out = {}
    for role, g in df.groupby("role"):
        ent = {"all": {"n": int(len(g)), "q": [round(float(x), 4) for x in np.quantile(g.ratio, QS)]}}
        for b, gb in g.groupby("band"):
            if len(gb) >= MIN_DRIFT_N:
                ent[b] = {"n": int(len(gb)),
                          "q": [round(float(x), 4) for x in np.quantile(gb.ratio, QS)]}
        out[role] = ent
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    now = datetime.now(JST)
    date_to = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    h = load_horses(conn, date_to)
    table = build_table(h)
    drift_df = load_drift(conn)
    drift = build_drift(drift_df) if len(drift_df) else {}
    conn.close()

    data = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M JST"),
        "period": [DATE_FROM, date_to],
        "start_year": int(DATE_FROM[:4]),       # 表示の「◯年以降」に使う
        "n_horses": int(len(h)),
        "n_races": int(h.race_id.nunique()),
        "edges": EDGES,
        "labels": LABELS,
        "small_field": SMALL_FIELD,
        "quantiles": QS,
        "table": table,
        "drift": drift,
        "drift_n": int(len(drift_df)),
        "note": "層 (平場 / 未勝利・新馬) 別の実測。障害は対象外。払戻は100円あたり。"
                "想定オッズは締切までの動き (投稿印の実測分布) を混ぜて確定帯に当てる。",
    }

    print(f"📊 期待値の実測表: {data['n_races']:,}レース / {data['n_horses']:,}頭 "
          f"({DATE_FROM}〜{date_to})、締切までの動き {data['drift_n']:,}頭")
    for layer, key in [(ly, k) for ly in ("flat", "maiden") for k in ("big", "small")]:
        print(f"  [{'平場' if layer == 'flat' else '未勝利・新馬'} / {'8頭以上' if key == 'big' else '7頭以下'}]")
        for b in LABELS:
            r = table.get(layer, {}).get(key, {}).get(b)
            if not r:
                continue
            pe = (r["place_rate"] or 0) * (r["place_pay"] or 0)
            we = r["win_rate"] * (r["win_pay"] or 0)
            print(f"    {b:>8} n={r['n']:>6}  複勝 {100*(r['place_rate'] or 0):5.1f}% × "
                  f"{(r['place_pay'] or 0):6.0f}円 = {pe:5.1f}円 / "
                  f"単勝 {100*r['win_rate']:5.1f}% × {(r['win_pay'] or 0):6.0f}円 = {we:5.1f}円")
    for role, ent in drift.items():
        med = ent["all"]["q"][len(QS) // 2]
        print(f"  締切までの動き {role}: n={ent['all']['n']} 中央値 ×{med:.2f} "
              f"(帯別 {', '.join(k for k in ent if k != 'all')})")

    if args.dry_run:
        return
    # 読み手 (投稿・ダッシュボード出力) が書きかけのファイルを掴まないよう、一時ファイル → 置き換え
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print(f"✅ {os.path.relpath(OUT, ROOT)} を更新")


if __name__ == "__main__":
    main()
