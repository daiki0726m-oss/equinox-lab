#!/usr/bin/env python3
"""印の実測トラッカー (#142) — 投稿した印が3着以内に来たか、その配当はいくらか。

ユーザー指示 (2026-09-02): 「実際の回収率で判断します。
AIの予想印をつけた馬の3着以内とその配当を追っていく」

**なぜバックテストでなく実測なのか**
過去5回、綺麗なバックテスト結果はすべて反証された
(#73 in-sample / #99 look-ahead / #100 選択バイアス / #109 学習境界 / #141 分割破綻)。
極めつけは #141 で、学習/テストの分割そのものが日付順になっておらず、
**#109 以降の全バックテストが答えを知った状態で採点していた**ことが判明した。
よってバックテストの数字は一切使わない。使うのは以下の3つだけ:

  1. docs/data/posted_marks_YYYYMMDD.json — 投稿した瞬間に凍結した印とオッズ
     (git管理のテキスト。DBと違い他の処理に上書きされない実績が16/16日)
  2. results テーブルの確定着順
  3. payouts テーブルの実際の払戻金

推定オッズも、モデルの出力も、想定配当も使わない。全部「実際にどうだったか」。

**判定に必要なサンプル数について**
#141 で、1レースあたりの回収率のばらつき (標準偏差) は 170ポイントと実測された。
つまり「5ポイント良くなった」を確信するには約9,000レースが必要。
現在の蓄積は数百レースなので、このツールは**まだ結論を出せない**。
出せないことを隠さず、必要サンプル数と現在地を毎回表示する。
"""
import glob
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import get_db  # noqa: E402

MARKS = ['◎', '○', '▲', '△', '☆', '×', '注']
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "data")


def load_frozen_marks(since=None, until=None):
    """投稿時に凍結された印を全日分読む。{date: {race_id: [horse...]}}"""
    out = {}
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "posted_marks_*.json"))):
        date = os.path.basename(path)[13:21]
        if since and date < since.replace('-', ''):
            continue
        if until and date > until.replace('-', ''):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"⚠️ {path} を読めません: {e}")
            continue
        races = data.get("races") or {}
        if races:
            out[date] = races
    return out


def fetch_results(race_ids):
    """確定着順と確定人気。{race_id: {horse_number: (finish, popularity, name)}}"""
    if not race_ids:
        return {}
    out = defaultdict(dict)
    with get_db() as conn:
        for i in range(0, len(race_ids), 400):
            chunk = race_ids[i:i + 400]
            ph = ','.join('?' * len(chunk))
            for r in conn.execute(
                f"""SELECT r.race_id, r.horse_number, r.finish_position, r.popularity,
                           COALESCE(h.horse_name, '')
                    FROM results r LEFT JOIN horses h ON r.horse_id = h.horse_id
                    WHERE r.race_id IN ({ph})""", chunk):
                out[r[0]][r[1]] = (r[2] or 0, r[3] or 0, r[4] or '')
    return out


def fetch_payouts(race_ids):
    """実際の払戻金。{race_id: {bet_type: {combination: payout}}}"""
    if not race_ids:
        return {}
    out = defaultdict(lambda: defaultdict(dict))
    with get_db() as conn:
        for i in range(0, len(race_ids), 400):
            chunk = race_ids[i:i + 400]
            ph = ','.join('?' * len(chunk))
            for r in conn.execute(
                f"""SELECT race_id, bet_type, combination, payout_amount
                    FROM payouts WHERE race_id IN ({ph})""", chunk):
                out[r[0]][r[1]][str(r[2])] = r[3] or 0
    return out


def _key(nums):
    """払戻の組み合わせキー。DB は馬番を**数値順**で持つ。
    #95 で文字列ソート ('13-14-4') と数値ソート ('4-13-14') の不一致により、
    2桁馬番を含む的中の払戻を導入以来すべて取り逃していた前科がある。"""
    return '-'.join(str(n) for n in sorted(int(x) for x in nums))


def analyze(since=None, until=None):
    frozen = load_frozen_marks(since, until)
    if not frozen:
        print("❌ 凍結された印の記録がありません")
        return None

    race_ids = [rid for races in frozen.values() for rid in races]
    results = fetch_results(race_ids)
    payouts = fetch_payouts(race_ids)

    # ── 1. 印ごとの3着以内率と単複の実回収 ──
    per_mark = {m: {'n': 0, 'top3': 0, 'win': 0, 'ret_win': 0, 'ret_place': 0,
                    'pop_sum': 0, 'pop_n': 0, 'odds_sum': 0} for m in MARKS}
    # ── 2. レース単位の集計 ──
    race_rows = []
    skipped_no_result = 0

    for date, races in sorted(frozen.items()):
        for rid, horses in races.items():
            fin = results.get(rid, {})
            if not fin or 1 not in [v[0] for v in fin.values()]:
                skipped_no_result += 1
                continue
            pay = payouts.get(rid, {})
            marked = {}
            marked_all = []
            for h in horses:
                mk, hn = h.get('mark'), h.get('horse_number')
                if mk not in per_mark or hn is None:
                    continue
                # #143: △は2頭つくので上書きしない (印→馬番の多重辞書)
                marked.setdefault(mk, hn)
                marked_all.append((mk, hn))
                f, pop_db, _ = fin.get(hn, (0, 0, ''))
                # 人気は投稿時点の凍結値を優先。results.popularity には
                # 取消馬の 9999 が入っており (実測8件)、平均を壊す (#141 N)。
                pop = h.get('popularity_at_post') or 0
                if not (0 < pop < 100):
                    pop = pop_db if 0 < pop_db < 100 else 0
                s = per_mark[mk]
                s['n'] += 1
                if pop:
                    s['pop_sum'] += pop
                    s['pop_n'] = s.get('pop_n', 0) + 1
                s['odds_sum'] += (h.get('odds_win_at_post') or 0)
                if f == 1:
                    s['win'] += 1
                    s['ret_win'] += pay.get('単勝', {}).get(str(hn), 0)
                if 1 <= f <= 3:
                    s['top3'] += 1
                    s['ret_place'] += pay.get('複勝', {}).get(str(hn), 0)

            # 3着以内3頭のうち何頭を印で捕まえたか
            top3_horses = [hn for hn, v in fin.items() if 1 <= v[0] <= 3]
            _all_hns = {hn for _, hn in marked_all}
            caught = len([hn for hn in top3_horses if hn in _all_hns])
            race_rows.append({
                'date': date, 'race_id': rid,
                'marked': marked, 'marked_all': marked_all,
                'top3_horses': top3_horses,
                'caught': caught, 'need': len(top3_horses),
                'payouts': pay, 'fin': fin,
            })

    return {'per_mark': per_mark, 'races': race_rows,
            'days': len(frozen), 'skipped': skipped_no_result}



# ─────────────────────────────────────────────────────────────
#  印だけで組んだ買い目が実際にいくら返ってきたか (実払戻のみ)
# ─────────────────────────────────────────────────────────────
def _combos(kind, marks_map):
    """印から買い目を組む。marks_map = {印: 馬番}。
    ここで組むのは「印を見た人が普通に買う形」であって、本番の投資判断
    (should_bet / 適応型構造) とは別物。目的は『印そのものにいくらの
    配当力があるか』を、投資ロジックの良し悪しと切り離して測ること。"""
    m = marks_map
    ax = m.get('◎')
    # ☆ は #140 で新設した妙味枠。それ以前のデータでは ○ が妙味枠だった
    val = m.get('☆') or m.get('○')
    others = [m[k] for k in ('○', '▲', '△', '☆', '×') if k in m and m[k] != ax]
    chu = m.get('注')
    out = []
    if kind == '単勝◎' and ax:
        out = [(ax,)]
    elif kind == '複勝◎' and ax:
        out = [(ax,)]
    elif kind == 'ワイド◎軸' and ax:
        out = [(ax, o) for o in others[:4]]
    elif kind == '馬連◎軸' and ax:
        out = [(ax, o) for o in others[:4]]
    elif kind == '三連複◎軸' and ax and len(others) >= 2:
        import itertools
        out = [(ax,) + c for c in itertools.combinations(others[:4], 2)]
    elif kind == '三連複 印5頭BOX':
        import itertools
        pool = ([ax] if ax else []) + others
        if len(pool) >= 3:
            out = list(itertools.combinations(pool[:5], 3))
    elif kind == '三連複◎軸+注' and ax and len(others) >= 2 and chu:
        import itertools
        pool = others[:3] + [chu]
        out = [(ax,) + c for c in itertools.combinations(pool, 2)]
    elif kind == '単勝 注' and chu:
        out = [(chu,)]
    elif kind == 'ワイド ◎-妙味' and ax and val and val != ax:
        out = [(ax, val)]
    return out


BET_KINDS = ['単勝◎', '複勝◎', 'ワイド◎軸', '馬連◎軸',
             '三連複◎軸', '三連複 印5頭BOX', '三連複◎軸+注',
             'ワイド ◎-妙味', '単勝 注']

_PAY_TYPE = {'単勝◎': '単勝', '複勝◎': '複勝', 'ワイド◎軸': 'ワイド',
             '馬連◎軸': '馬連', '三連複◎軸': '三連複',
             '三連複 印5頭BOX': '三連複', '三連複◎軸+注': '三連複',
             'ワイド ◎-妙味': 'ワイド', '単勝 注': '単勝'}


def bet_returns(races):
    """各買い方の実績。1点100円固定。払戻は payouts テーブルの実額のみ。"""
    agg = {k: {'races': 0, 'points': 0, 'spend': 0, 'ret': 0, 'hits': 0,
               'best': 0, 'best_date': '', 'by_day': defaultdict(lambda: [0, 0])}
           for k in BET_KINDS}
    for r in races:
        for kind in BET_KINDS:
            combos = _combos(kind, r['marked'])
            if not combos:
                continue
            ptype = _PAY_TYPE[kind]
            table = r['payouts'].get(ptype, {})
            spend = len(combos) * 100
            ret = 0
            hit = 0
            for c in combos:
                got = table.get(_key(c), 0)
                if got:
                    ret += got
                    hit += 1
            a = agg[kind]
            a['races'] += 1
            a['points'] += len(combos)
            a['spend'] += spend
            a['ret'] += ret
            a['hits'] += hit
            a['by_day'][r['date']][0] += spend
            a['by_day'][r['date']][1] += ret
            if ret > a['best']:
                a['best'], a['best_date'] = ret, r['date']
    return agg


def report_bets(races):
    agg = bet_returns(races)
    print("\n【印だけで組んだ買い目の実回収】(1点100円・実際の払戻のみ)")
    print(f"{'買い方':<16}{'R数':>5}{'点数':>6}{'投資':>10}{'回収':>10}"
          f"{'回収率':>8}{'的中':>6}{'最高配当':>10}{'最良日除外':>10}")
    print("-" * 82)
    for k in BET_KINDS:
        a = agg[k]
        if a['races'] == 0:
            continue
        roi = a['ret'] / a['spend'] * 100 if a['spend'] else 0
        # 単日支配チェック: 最も回収の多かった1日を除くと回収率がどう動くか
        # (#26 で「17日間のROI 151%が5/9単日に支配されていた」前科)
        if a['by_day']:
            worst = max(a['by_day'].items(), key=lambda x: x[1][1])
            sp = a['spend'] - worst[1][0]
            rt = a['ret'] - worst[1][1]
            ex = rt / sp * 100 if sp else 0
        else:
            ex = 0
        print(f"{k:<16}{a['races']:>5}{a['points']:>6}{a['spend']:>9,}円{a['ret']:>9,}円"
              f"{roi:>7.1f}%{a['hits']:>6}{a['best']:>9,}円{ex:>9.1f}%")
    return agg


def _wilson(k, n):
    """3着内率の95%信頼区間 (Wilson)。n が小さい時に点推定だけ見ると誤判断する。"""
    if n == 0:
        return (0.0, 0.0)
    z = 1.96
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    hw = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - hw) * 100, min(1.0, c + hw) * 100)


def report(since=None, until=None):
    a = analyze(since, until)
    if not a:
        return
    pm, races = a['per_mark'], a['races']

    print("=" * 66)
    print("  印の実測トラッカー — 投稿した印の3着以内と実配当のみで判定")
    print("=" * 66)
    print(f"対象: {a['days']}開催日 / {len(races)}レース"
          + (f"  (結果未確定で除外 {a['skipped']}R)" if a['skipped'] else ""))
    print("※ バックテストの数字は一切含みません。凍結した印 × 確定着順 × 実払戻のみ。\n")

    print("【印ごとの成績】(単勝・複勝を100円ずつ買った場合の実回収)")
    print(f"{'印':<3}{'頭数':>5}{'1着':>5}{'3着内':>6}{'3着内率':>9}{'95%信頼区間':>16}"
          f"{'単勝回収':>10}{'複勝回収':>10}{'平均人気':>9}")
    print("-" * 76)
    for m in MARKS:
        s = pm[m]
        if s['n'] == 0:
            continue
        lo, hi = _wilson(s['top3'], s['n'])
        print(f"{m:<3}{s['n']:>5}{s['win']:>5}{s['top3']:>6}"
              f"{s['top3']/s['n']*100:>8.1f}%{f'{lo:.0f}〜{hi:.0f}%':>16}"
              f"{s['ret_win']/s['n']:>9.0f}円{s['ret_place']/s['n']:>9.0f}円"
              f"{(s['pop_sum']/s['pop_n']) if s.get('pop_n') else 0:>8.1f}")

    # 捕捉
    print("\n【3着以内3頭を印で何頭捕まえたか】")
    dist = defaultdict(int)
    for r in races:
        dist[r['caught']] += 1
    tot = len(races)
    for c in sorted(dist, reverse=True):
        bar = '█' * int(dist[c] / max(tot, 1) * 40)
        print(f"  {c}頭捕捉: {dist[c]:>4}R ({dist[c]/tot*100:>5.1f}%) {bar}")
    avg = sum(r['caught'] for r in races) / max(tot, 1)
    print(f"  平均 {avg:.2f}頭 / 3頭")

    report_bets(races)

    # 判定できるかどうか
    print("\n【この数字で判断できるか】")
    n = tot
    # #141 実測: 1レースあたり回収率の標準偏差 ~170pt
    sd = 170.0
    mde = 2.8 * sd / math.sqrt(max(n, 1))   # 80%検出力・両側5%のおおよその最小検出差
    print(f"  現在 {n} レース → 検出できる回収率の差は約 ±{mde:.0f}ポイントまで")
    for target in (10, 5):
        need = int((2.8 * sd / target) ** 2)
        weeks = need / max(tot / max(a['days'] / 2, 1), 1) if tot else 0
        print(f"  「{target}ポイントの改善」を確信するには約 {need:,} レース"
              f" (今のペースで約 {need/(tot/(a['days']/2)):.0f} 週)")
    print("  → 現時点では『どの施策が良いか』は判定できません。積み上げが必要です。")
    return a


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD 以降のみ")
    ap.add_argument("--until", help="YYYY-MM-DD 以前のみ")
    ap.add_argument("--json", help="結果をJSONで書き出すパス")
    args = ap.parse_args()
    a = report(args.since, args.until)
    if a and args.json:
        agg = bet_returns(a['races'])
        out = {
            'days': a['days'], 'races': len(a['races']),
            'per_mark': a['per_mark'],
            'bets': {k: {'races': v['races'], 'points': v['points'],
                         'spend': v['spend'], 'return': v['ret'], 'hits': v['hits'],
                         'roi_pct': round(v['ret'] / v['spend'] * 100, 1) if v['spend'] else 0,
                         'best_payout': v['best'], 'best_day': v['best_date']}
                     for k, v in agg.items() if v['races']},
            'note': ('凍結した投稿印 × 確定着順 × 実払戻のみ。'
                     'バックテスト・推定オッズ・モデル出力は一切含まない (#142)'),
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"\n📝 {args.json} に書き出しました")
