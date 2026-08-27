#!/usr/bin/env python3
"""週次採点スクリプト (#121 2026-08-24): 週末の投稿印×結果×買い目構造を採点する正式版。

毎週の振り返りで inline スクリプトを書き捨てていた結果、「対象週末の日付取り違え」
(8/17週・8/24週の2回発生) と集計ロジックの微妙な揺れが起きていた。
- 対象週末は「posted_marks が存在し results が確定している最新2日」を自動判定
- 集計: 印別成績 / 信頼度別◎複勝率 (#120以降のみ) / 適応型買い目構造ROI (#113) /
  新体制累計 / ○(妙味枠)トレンド / A構造ブートストラップCI (分散vs構造の判定)
実行: python3 scripts/weekly_scorecard.py [--days YYYYMMDD YYYYMMDD]
"""
import argparse, glob, json, os, random, sqlite3, statistics, sys
from itertools import combinations

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_ERA = '20260718'      # 新体制 (◎ブレンド×妙味○×適応型買い目) 開始日
CONF_STABLE = '20260809'  # #120 (信頼度基準の安定化) 以降のみ信頼度別を集計
DESIGN = {'o_rate': 25, 'chu_rate': 20, 'roi': (85, 94), 'full': 30}

def db():
    return sqlite3.connect(os.path.join(ROOT, 'keiba.db'))

def load_marks(day):
    p = os.path.join(ROOT, 'docs', 'data', f'posted_marks_{day}.json')
    return json.load(open(p))['races'] if os.path.exists(p) else {}

def load_conf(day):
    p = os.path.join(ROOT, 'docs', 'data', f'predictions_{day}.json')
    if not os.path.exists(p):
        return {}
    d = json.load(open(p))
    return {r['race_id']: r.get('confidence', '?')
            for v, rs in d.get('venues', {}).items() for r in rs}

def race_rows(c, day):
    """day の投稿レースを (marks, finish_map, meta) で列挙。結果未確定はスキップ。"""
    for rid, lst in load_marks(day).items():
        mk = {e['mark']: e for e in lst}
        if '◎' not in mk:
            continue
        fin = {hn: fp for hn, fp in c.execute(
            "SELECT horse_number, finish_position FROM results WHERE race_id=? AND finish_position>0", (rid,))}
        if not fin or 1 not in fin.values():
            continue
        meta = c.execute("SELECT venue, race_number, race_name, horse_count FROM races WHERE race_id=?",
                         (rid,)).fetchone()
        yield rid, mk, fin, meta

def structure_bets(c, rid, mk, fin, meta):
    """適応型買い目 (#113) を再現し (構造名, spend, return) を返す。対象外は None。"""
    rn = (meta[2] or '')
    sys.path.insert(0, ROOT)
    from race_utils import is_ml_out_of_domain
    if is_ml_out_of_domain(rn):    # #123: "新潟JS" 等のジャンプSも除外
        return None
    ax = mk['◎']; axhn = ax['horse_number']
    axod = ax.get('odds_win_at_post') or 0
    ohn = mk.get('○', {}).get('horse_number')
    others = [mk[m]['horse_number'] for m in ('▲', '△', '×') if m in mk]
    chu = mk.get('注', {}).get('horse_number')
    nh = meta[3] or len(fin)
    pay = {(bt, cb): amt for bt, cb, amt in c.execute(
        "SELECT bet_type, combination, payout_amount FROM payouts WHERE race_id=?", (rid,))}
    sp = ret = 0
    if 0 < axod < 2.0 and ohn:
        name = 'G:2頭軸'
        for p in others + ([chu] if chu else []):
            cb = '-'.join(str(x) for x in sorted([axhn, ohn, p]))
            sp += 100; ret += pay.get(('三連複', cb), 0)
    elif 2.0 <= axod < 3.0 and nh >= 11 and ohn:
        name = 'D:三連単F'
        for a in [axhn, ohn]:
            for b in [axhn, ohn] + others[:2]:
                if b == a:
                    continue
                for cc in [axhn, ohn] + others + ([chu] if chu else []):
                    if cc in (a, b):
                        continue
                    sp += 100; ret += pay.get(('三連単', f'{a}-{b}-{cc}'), 0)
    else:
        name = 'A:3連複軸'
        ptn = ([ohn] if ohn else []) + others
        for p, q in combinations(ptn[:4], 2):
            cb = '-'.join(str(x) for x in sorted([axhn, p, q]))
            sp += 100; ret += pay.get(('三連複', cb), 0)
    return (name, sp, ret) if sp else None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', nargs='*', help='対象日 YYYYMMDD (省略時は自動判定)')
    args = ap.parse_args()
    c = db()
    all_days = sorted(os.path.basename(p)[13:21]
                      for p in glob.glob(os.path.join(ROOT, 'docs', 'data', 'posted_marks_2026*.json')))
    era_days = [d for d in all_days if d >= NEW_ERA]
    if args.days:
        target = args.days
    else:
        # 結果が確定している最新の開催日から直近2日 (=直近週末) を自動判定
        settled = [d for d in era_days if any(True for _ in race_rows(c, d))]
        target = settled[-2:]
    print(f"=== 対象週末: {', '.join(target)} (自動判定) ===\n")

    tot = [0, 0]
    for day in target:
        st = dict(n=0, axw=0, axt3=0, full=0, o=0, on=0, opop=[], chu=0, chun=0)
        struct = {}
        byconf = {}
        conf_map = load_conf(day)
        for rid, mk, fin, meta in race_rows(c, day):
            top3 = {hn for hn, fp in fin.items() if fp <= 3}
            axhn = mk['◎']['horse_number']
            st['n'] += 1
            st['axw'] += (fin.get(axhn) == 1)
            st['axt3'] += (axhn in top3)
            five = [mk[m]['horse_number'] for m in ('◎', '○', '▲', '△', '×') if m in mk]
            st['full'] += (len(set(five) & top3) == 3)
            if '○' in mk:
                st['on'] += 1
                st['o'] += (mk['○']['horse_number'] in top3)
                st['opop'].append(mk['○'].get('popularity_at_post') or 0)
            if '注' in mk:
                st['chun'] += 1
                st['chu'] += (mk['注']['horse_number'] in top3)
            cf = conf_map.get(rid, '?')
            if cf in 'SABCD' and day >= CONF_STABLE:
                b = byconf.setdefault(cf, [0, 0]); b[1] += 1; b[0] += (axhn in top3)
            sb = structure_bets(c, rid, mk, fin, meta)
            if sb:
                s = struct.setdefault(sb[0], [0, 0, 0])
                s[0] += sb[1]; s[1] += sb[2]; s[2] += 1
                tot[0] += sb[1]; tot[1] += sb[2]
        n = st['n'] or 1
        opop = statistics.mean(st['opop']) if st['opop'] else 0
        print(f"【{day[4:6]}/{day[6:]}】投稿{st['n']}R")
        print(f"  ◎勝率{st['axw']}/{st['n']} ◎複勝{st['axt3']}/{st['n']} ({100*st['axt3']/n:.0f}%) 完全捕捉{st['full']}/{st['n']}")
        print(f"  ○複勝{st['o']}/{st['on']} ({opop:.1f}人気) 注{st['chu']}/{st['chun']}")
        if byconf:
            print("  信頼度別◎複勝: " + ' '.join(f"{k}:{v[0]}/{v[1]}" for k, v in sorted(byconf.items())))
        for k, (a, b, cnt) in sorted(struct.items()):
            print(f"  {k}: {cnt}R {a:,}円→{b:,}円 ROI {100*b/max(a,1):.0f}%")
        print()
    print(f"週末合計: {tot[0]:,}円 → {tot[1]:,}円 = ROI {100*tot[1]/max(tot[0],1):.1f}%\n")

    # ── 累計 (新体制) ──
    agg = {}; conf_agg = {}; o_tot = [0, 0]; A_races = []
    for d in era_days:
        conf_map = load_conf(d)
        for rid, mk, fin, meta in race_rows(c, d):
            top3 = {hn for hn, fp in fin.items() if fp <= 3}
            axhn = mk['◎']['horse_number']
            if '○' in mk:
                o_tot[1] += 1; o_tot[0] += (mk['○']['horse_number'] in top3)
            if d >= CONF_STABLE:
                cf = conf_map.get(rid, '?')
                if cf in 'SABCD':
                    b = conf_agg.setdefault(cf, [0, 0]); b[1] += 1; b[0] += (axhn in top3)
            sb = structure_bets(c, rid, mk, fin, meta)
            if sb:
                s = agg.setdefault(sb[0], [0, 0, 0])
                s[0] += sb[1]; s[1] += sb[2]; s[2] += 1
                if sb[0] == 'A:3連複軸':
                    A_races.append((sb[1], sb[2]))
    print(f"=== 新体制累計 ({era_days[0][4:6]}/{era_days[0][6:]}〜) ===")
    gsp = gret = 0
    for k, (sp, ret, cnt) in sorted(agg.items()):
        gsp += sp; gret += ret
        print(f"  {k}: {cnt}R ROI {100*ret/max(sp,1):.1f}%")
    print(f"  総合: {gsp:,}円→{gret:,}円 ROI {100*gret/max(gsp,1):.1f}%")
    print(f"  ○複勝率 累計: {o_tot[0]}/{o_tot[1]} = {100*o_tot[0]/max(o_tot[1],1):.1f}% (設計値{DESIGN['o_rate']}%)")
    print(f"  信頼度別◎複勝 (#120以降): " + ' '.join(
        f"{k}:{v[0]}/{v[1]}({100*v[0]/v[1]:.0f}%)" for k, v in sorted(conf_agg.items())))
    # A構造の分散vs構造判定 (bootstrap 95%CI)
    if len(A_races) >= 30:
        random.seed(42)
        n = len(A_races)
        boots = sorted(100 * sum(x[1] for x in (random.choices(A_races, k=n))) /
                       sum(x[0] for x in (A_races)) for _ in range(4000))
        lo, hi = boots[100], boots[3899]
        roi = 100 * sum(x[1] for x in A_races) / sum(x[0] for x in A_races)
        verdict = '分散の範囲' if lo <= DESIGN['roi'][0] <= hi else '構造的低迷 (要対処)'
        print(f"  A構造判定: {n}R ROI {roi:.1f}% / 95%CI {lo:.0f}-{hi:.0f}% → 設計値{DESIGN['roi'][0]}%は{verdict}")

if __name__ == '__main__':
    main()
