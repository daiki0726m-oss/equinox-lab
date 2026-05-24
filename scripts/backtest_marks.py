#!/usr/bin/env python3
"""印別 ROI バックテスト v2 (2026-05-24)

対象期間の predictions_cache + results + payouts を照合して、
- 印別の的中率(1着・連対・複勝)
- 印別の人気帯分布
- 推奨買い目別の ROI (システム現状)
- EVベース投資の効果検証
- 「もしこういう買い方だったら」のシミュレーション
  - 馬連 流し (◎ → ○▲△×注)
  - ワイド 流し (◎ → ○▲△×注)
  - 三連複 BOX (◎○▲△, ◎○▲△×)
  - 三連複 フォーメーション (◎ - ○▲△ - 全)
  - 三連単 フォーメーション (◎ - ○▲△ - ○▲△×注)
を出力する。

CLI:
  python scripts/backtest_marks.py --from YYYY-MM-DD --to YYYY-MM-DD
"""
from __future__ import annotations
import sqlite3
import json
import sys
import os
import argparse
from collections import defaultdict, Counter
from itertools import combinations, permutations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MARKS = ['◎', '○', '▲', '△', '×', '注']


def get_conn():
    conn = sqlite3.connect('keiba.db')
    conn.row_factory = sqlite3.Row
    return conn


def fetch_races(conn, date_from: str, date_to: str):
    return conn.execute("""
        SELECT DISTINCT r.race_id, r.race_date, r.venue, r.race_number, r.race_name,
               r.horse_count
        FROM races r
        JOIN predictions_cache pc ON r.race_id = pc.race_id
        JOIN payouts p ON r.race_id = p.race_id
        WHERE r.race_date >= ? AND r.race_date <= ?
          AND EXISTS (SELECT 1 FROM results WHERE race_id = r.race_id AND finish_position > 0)
        ORDER BY r.race_date, r.race_id
    """, (date_from, date_to)).fetchall()


def normalize_combo(bt: str, combo: str) -> str:
    c = combo.replace(' ', '').replace('→', '-').replace(',', '-')
    if bt in ('三連複', '馬連', 'ワイド'):
        nums = sorted([int(x) for x in c.split('-') if x.strip().isdigit()])
        return '-'.join(str(x) for x in nums)
    return c


def load_race_context(conn, race_id):
    """1レースの 印付け + 結果 + 払戻 をまとめて取得"""
    preds_row = conn.execute(
        "SELECT predictions_json FROM predictions_cache WHERE race_id = ?", (race_id,)
    ).fetchone()
    if not preds_row:
        return None
    preds = json.loads(preds_row['predictions_json'])
    marks = {}  # mark → horse_number
    for p in preds:
        if p.get('mark') in MARKS:
            marks[p['mark']] = p['horse_number']

    fin_rows = conn.execute(
        "SELECT horse_number, finish_position FROM results "
        "WHERE race_id = ? AND finish_position > 0", (race_id,)
    ).fetchall()
    fin_map = {r['horse_number']: r['finish_position'] for r in fin_rows}
    rev = {v: k for k, v in fin_map.items()}  # finish → horse_number
    top3 = [rev.get(i) for i in (1, 2, 3)]

    payouts_rows = conn.execute(
        "SELECT bet_type, combination, payout_amount FROM payouts WHERE race_id = ?", (race_id,)
    ).fetchall()
    payout = defaultdict(dict)
    for p in payouts_rows:
        payout[p['bet_type']][normalize_combo(p['bet_type'], p['combination'])] = p['payout_amount']

    return {
        'preds': preds, 'marks': marks, 'fin_map': fin_map, 'top3': top3,
        'payout': payout
    }


def analyze_marks(conn, races):
    """印別の的中率"""
    mk_stat = {m: {'n': 0, 'win': 0, 'place': 0, 'show': 0,
                   'pop_list': []} for m in MARKS}
    for r in races:
        ctx = load_race_context(conn, r['race_id'])
        if not ctx:
            continue
        for p in ctx['preds']:
            mk = p.get('mark', '')
            if mk not in MARKS:
                continue
            fin = ctx['fin_map'].get(p['horse_number'])
            if not fin:
                continue
            s = mk_stat[mk]
            s['n'] += 1
            s['pop_list'].append(p.get('popularity', 0) or 0)
            if fin == 1: s['win'] += 1
            if fin <= 2: s['place'] += 1
            if fin <= 3: s['show'] += 1
    return mk_stat


def analyze_current_bets(conn, races):
    """システム現状の推奨買い目 ROI"""
    bet_stat = defaultdict(lambda: {'n': 0, 'hit': 0, 'spend': 0, 'return': 0,
                                     'ev_lt08': 0, 'ev_ge09': 0, 'ev_ge12': 0})
    for r in races:
        ctx = load_race_context(conn, r['race_id'])
        if not ctx:
            continue
        cache_row = conn.execute(
            "SELECT all_bets_json, should_bet FROM predictions_cache WHERE race_id = ?",
            (r['race_id'],)
        ).fetchone()
        if not cache_row or not cache_row['should_bet']:
            continue
        all_bets = json.loads(cache_row['all_bets_json'])
        for bt, bets in all_bets.items():
            if not isinstance(bets, list):
                continue
            for b in bets:
                stake = b.get('amount', 100)
                ev = b.get('ev', 0)
                s = bet_stat[bt]
                s['n'] += 1
                s['spend'] += stake
                if ev < 0.8: s['ev_lt08'] += 1
                if ev >= 0.9: s['ev_ge09'] += 1
                if ev >= 1.2: s['ev_ge12'] += 1
                detail = b.get('detail', '')
                if bt == '三連複':
                    hs = sorted(b.get('horse_numbers', []))
                    detail = '-'.join(str(x) for x in hs)
                else:
                    detail = normalize_combo(bt, detail)
                if detail in ctx['payout'].get(bt, {}):
                    amt = ctx['payout'][bt][detail]
                    s['hit'] += 1
                    s['return'] += amt * stake / 100
    return bet_stat


def sim_buy(strategy_name, get_combos_fn, races, conn, stake_per=100):
    """汎用シミュレーター。
    get_combos_fn(ctx) → [(bet_type, combo_str), ...]
    """
    spend = ret = n_bets = n_hits = 0
    races_with_bets = 0
    for r in races:
        ctx = load_race_context(conn, r['race_id'])
        if not ctx:
            continue
        combos = get_combos_fn(ctx)
        if combos:
            races_with_bets += 1
        for bt, combo in combos:
            n_bets += 1
            spend += stake_per
            combo = normalize_combo(bt, combo)
            if combo in ctx['payout'].get(bt, {}):
                amt = ctx['payout'][bt][combo]
                ret += amt * stake_per / 100
                n_hits += 1
    return {
        'name': strategy_name, 'n_bets': n_bets, 'n_hits': n_hits,
        'spend': spend, 'return': ret, 'races': races_with_bets,
    }


def umaren_nagashi(ctx):
    """馬連 流し: ◎ → ○▲△×注 (5点)"""
    out = []
    a = ctx['marks'].get('◎')
    if not a: return out
    for m in ['○', '▲', '△', '×', '注']:
        b = ctx['marks'].get(m)
        if b and b != a:
            out.append(('馬連', f"{min(a,b)}-{max(a,b)}"))
    return out


def wide_nagashi(ctx):
    """ワイド 流し: ◎ → ○▲△×注 (5点)"""
    out = []
    a = ctx['marks'].get('◎')
    if not a: return out
    for m in ['○', '▲', '△', '×', '注']:
        b = ctx['marks'].get(m)
        if b and b != a:
            out.append(('ワイド', f"{min(a,b)}-{max(a,b)}"))
    return out


def trio_box_4(ctx):
    """三連複 BOX (◎○▲△) = 4頭BOX = 4C3 = 4点"""
    horses = [ctx['marks'].get(m) for m in ['◎', '○', '▲', '△']]
    horses = [h for h in horses if h]
    if len(horses) < 3: return []
    return [('三連複', '-'.join(str(x) for x in sorted(c))) for c in combinations(horses, 3)]


def trio_box_5(ctx):
    """三連複 BOX (◎○▲△×) = 5頭BOX = 5C3 = 10点"""
    horses = [ctx['marks'].get(m) for m in ['◎', '○', '▲', '△', '×']]
    horses = [h for h in horses if h]
    if len(horses) < 3: return []
    return [('三連複', '-'.join(str(x) for x in sorted(c))) for c in combinations(horses, 3)]


def trio_box_6(ctx):
    """三連複 BOX (◎○▲△×注) = 6頭BOX = 6C3 = 20点"""
    horses = [ctx['marks'].get(m) for m in MARKS]
    horses = [h for h in horses if h]
    if len(horses) < 3: return []
    return [('三連複', '-'.join(str(x) for x in sorted(c))) for c in combinations(horses, 3)]


def trio_formation_axis(ctx):
    """三連複 フォーメーション (◎ 軸固定 → ○▲△×注の中から2頭) = 5C2 = 10点"""
    axis = ctx['marks'].get('◎')
    if not axis: return []
    others = [ctx['marks'].get(m) for m in ['○', '▲', '△', '×', '注']]
    others = [h for h in others if h and h != axis]
    if len(others) < 2: return []
    out = []
    for pair in combinations(others, 2):
        combo = sorted([axis] + list(pair))
        out.append(('三連複', '-'.join(str(x) for x in combo)))
    return out


def trifecta_axis_formation(ctx):
    """三連単 フォーメーション ◎軸1着 / 2着3頭(○▲△) / 3着5頭(○▲△×注) = 3*4 = 12点"""
    axis = ctx['marks'].get('◎')
    if not axis: return []
    second_pool = [ctx['marks'].get(m) for m in ['○', '▲', '△']]
    third_pool = [ctx['marks'].get(m) for m in ['○', '▲', '△', '×', '注']]
    second_pool = [h for h in second_pool if h and h != axis]
    third_pool = [h for h in third_pool if h and h != axis]
    out = []
    for s in second_pool:
        for t in third_pool:
            if t == s: continue
            out.append(('三連単', f"{axis}-{s}-{t}"))
    return out


def tan_complex_axis(ctx):
    """単勝 ◎ (1点) + 複勝 ◎○ (2点) = 3点 (シンプル軸)"""
    out = []
    a = ctx['marks'].get('◎')
    if a:
        out.append(('単勝', str(a)))
        out.append(('複勝', str(a)))
    b = ctx['marks'].get('○')
    if b: out.append(('複勝', str(b)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='date_from', default='2026-04-01')
    ap.add_argument('--to', dest='date_to', default='2026-05-31')
    args = ap.parse_args()

    conn = get_conn()
    races = fetch_races(conn, args.date_from, args.date_to)
    print(f"━━━━━━ Backtest {args.date_from} 〜 {args.date_to} ━━━━━━")
    print(f"対象レース: {len(races)}件\n")

    # 1. 印別
    mk_stat = analyze_marks(conn, races)
    print("━━━━━━ 印別成績 ━━━━━━")
    print(f"{'印':<3} {'n':>4} {'1着率':>7} {'連対率':>7} {'複勝率':>7} {'平均人気':>8}")
    for m in MARKS:
        s = mk_stat[m]
        if s['n'] == 0: continue
        avg_pop = sum(s['pop_list']) / len(s['pop_list']) if s['pop_list'] else 0
        print(f"{m:<3} {s['n']:>4} {100*s['win']/s['n']:>6.1f}% {100*s['place']/s['n']:>6.1f}% {100*s['show']/s['n']:>6.1f}% {avg_pop:>7.1f}")

    # 2. 印×人気
    print(f"\n━━━━━━ 印 × 人気帯 ━━━━━━")
    for m in MARKS:
        c = Counter()
        for pop in mk_stat[m]['pop_list']:
            if pop <= 3: c['1-3'] += 1
            elif pop <= 9: c['4-9'] += 1
            else: c['10+'] += 1
        total = sum(c.values())
        if total == 0: continue
        print(f"  {m}: 1-3={c['1-3']:>3}({100*c['1-3']//total}%) 4-9={c['4-9']:>3}({100*c['4-9']//total}%) 10+={c['10+']:>3}({100*c['10+']//total}%)")

    # 3. システム現状 ROI
    bet_stat = analyze_current_bets(conn, races)
    print(f"\n━━━━━━ システム現状 ROI ━━━━━━")
    print(f"{'券種':<8} {'点数':>5} {'的中':>4} {'投資':>9} {'回収':>11} {'ROI':>7}")
    print('-' * 55)
    tot_s = tot_r = 0
    for bt, s in sorted(bet_stat.items(), key=lambda x: -x[1]['spend']):
        roi = (s['return']/s['spend']*100) if s['spend'] else 0
        print(f"{bt:<8} {s['n']:>5} {s['hit']:>4} {s['spend']:>8.0f}円 {s['return']:>10.0f}円 {roi:>6.1f}%")
        tot_s += s['spend']; tot_r += s['return']
    print('-' * 55)
    roi = (tot_r/tot_s*100) if tot_s else 0
    print(f"{'合計':<8} {'':>5} {'':>4} {tot_s:>8.0f}円 {tot_r:>10.0f}円 {roi:>6.1f}%")

    # 4. シミュレーション (現実的買い方)
    print(f"\n━━━━━━ 仮想シナリオ ROI (各100円固定) ━━━━━━")
    print(f"{'戦略':<35} {'対象R':>6} {'点数':>5} {'的中':>5} {'投資':>9} {'回収':>11} {'ROI':>7}")
    print('-' * 95)
    strategies = [
        ('馬連 流し ◎→○▲△×注 (5点)', umaren_nagashi),
        ('ワイド 流し ◎→○▲△×注 (5点)', wide_nagashi),
        ('三連複 BOX ◎○▲△ (4点)', trio_box_4),
        ('三連複 BOX ◎○▲△× (10点)', trio_box_5),
        ('三連複 BOX ◎○▲△×注 (20点)', trio_box_6),
        ('三連複 フォメ ◎軸 × ○▲△×注 (10点)', trio_formation_axis),
        ('三連単 フォメ ◎-○▲△-○▲△×注 (12点)', trifecta_axis_formation),
        ('単勝◎+複勝◎○ (3点)', tan_complex_axis),
    ]
    for name, fn in strategies:
        s = sim_buy(name, fn, races, conn)
        if s['n_bets'] == 0:
            continue
        roi = (s['return']/s['spend']*100) if s['spend'] else 0
        print(f"{name:<35} {s['races']:>6} {s['n_bets']:>5} {s['n_hits']:>5} {s['spend']:>8.0f}円 {s['return']:>10.0f}円 {roi:>6.1f}%")


if __name__ == '__main__':
    main()
