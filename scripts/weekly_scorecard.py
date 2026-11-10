#!/usr/bin/env python3
"""週次採点スクリプト (#121 2026-08-24 / #152 2026-09-10 実額ベースに改修)。

毎週の振り返りで inline スクリプトを書き捨てていた結果、「対象週末の日付取り違え」
(8/17週・8/24週の2回発生) と集計ロジックの微妙な揺れが起きていた。
- 対象週末は「results が確定している最新2開催日」を自動判定 (開催日は posted_marks ∪ 予測 JSON、#173)
- 集計: 印別成績 (投稿した凍結印) / 信頼度別◎複勝率 (#120以降のみ) /
  買い目の実額ROI (金額が乗った全レース。予測が発走後に作り直されたレースは除く、#173) / 新体制累計

#152 でなぜ実額ベースに変えたか
------------------------------
旧版は「全投稿レースに一律 1点100円を賭けた」仮想ポートフォリオを採点していた。
その結果 A構造は 141R ROI 45.8% と出て「設計値85%を割った = 構造的低迷」と3週連続で
警報を出していたが、その141Rのうち **91R は本番が1円も張っていない**
(信頼度C/Dで買い目ゼロ、あるいは should_bet=0 で全ライン金額0)。
同じ一律採点でも「1円でも張った50R」に絞ると 45.8% → 65.2% に変わる。
警報は構造の成績ではなく、投資していないレースを計上していたことの反映だった。

同じ理由で「G:2頭軸 155.7%」も虚構だった。G は ◎が2.0倍未満で発動するが、
`should_bet_race` は `top_odds < 2.0` を必ず見送りにする (#25) ため、
**G判定レースは構造上1円も投資されない**。実測22R すべて投資額ゼロ。

そこで採点の母集団を「本番が実際に金額を乗せた買い目」に変えた。
買い目の正本は `docs/data/predictions_YYYYMMDD.json` の `all_bets`
(= predictions_cache.all_bets_json)。表示用の horses[].odds は refresh_odds が
上書きするので使わない (凍結印との一致率2.1%、#99/#141)。

all_bets_json はどこまで信用できるか
------------------------------------
`refresh_odds.py` がこの列に触らないのは確認済み (predictions_cache への UPDATE は
`SET predictions_json = ?` の1文だけ)。ただし「predict しか書かない」わけではない:
  - predict.py:1128 / :1277  … seal ガードあり
  - app.py:938               … **seal チェックなしの INSERT OR REPLACE**。
    過去日をダッシュボードAPIで開くと再生成され、posted_at と posted_marks_json まで
    NULL に落ちる。ローカル閲覧で汚染しうる経路。
  - scripts/flush_old_cache.py:68 … 古い分を '{}' に潰す (DB cache は恒久正本ではない)
実測では現時点の16開催日に汚染は無い (投稿時点コミットの JSON と一致) が、
**恒久正本は git 上の per-day JSON 側**であって DB cache ではない。

構造の判定に金額署名を使う理由
------------------------------
band (trio_focus #95) は betting.py:501 で三連複の**単価だけを2倍**にし、点数は増やさない。
点数で分類すると band が A (6点) に埋没する。実際それで band 12R (ROI 84.0%) が
通常A 37R (51.5%) と混ざり「A 65.0%」という中間値になっていた。

設計値との比較をやめた理由
--------------------------
`models/train_boundary.json` にあった `"split_key": "race_id (broken, pre-#141)"` の通り、
A の設計値 84-86% (#112/#113) を測った窓は学習データに食い込んでいた。#139 は既に
「設計値として信じない」扱い。信頼区間が85%を外すのは *Aが悪い証拠* であると同時に
*85%が無効である証拠* でもあり、判定材料にならない。
代わりに「この構造は残りの買い目と統計的に区別できるか」+ 2標本の最小検出差 (MDE) +
単日支配・一撃依存 (CLAUDE.md #26 の必須チェック) を出す。区別できないなら
「区別できない」と言う。

#169 で足したもの (末尾の2セクション)
------------------------------------
- 予測時点の市場との比較 (scripts/weekly_market_compare.py): (a) 印5頭 vs 予測時の人気上位5頭の
  捕捉、(b) LGB vs 同時刻市場のレース内AUC、(c) 初ダート/初芝替わり馬の実際 vs 見込み、
  (e) 週末の投資ゲート遮断率 (目標 30%/25%)。予測時オッズは S 班のスナップショットがあればそれ、
  無い過去分は git 履歴の予測 JSON から取り、どちらを使ったかを出す。
  確定オッズで測った val AUC やバックテスト162% はモデルの価値の根拠として出さない。
- v3 と v2 の対応比較 (scripts/scorecard_v2_paired.py): 同じレースで v2 の印を作り直し、
  docs/data/scorecard_v2_paired.json に貯めて累積で見る。対象日の未計算分は既定で計算する
  (1レース約70秒。--no-v2 で省くと、未計算の数を出したうえで貯めた分だけ集計する)。
- 毎週月曜に .github/workflows/weekly_report.yml が GitHub 側でこのスクリプトを実行し、
  出力を docs/data/weekly_reports/ に commit する (月曜の定例は無人実行で止まるため #169)。

#173 で直したもの (11/07-08 の振り返りの反証で発覚)
--------------------------------------------------
- 「○複勝」は実際には ☆ を数えていた (`mk.get('☆') or mk.get('○')`)。☆ が無いレースは
  ○ (#140 以降は実力2位) で代用され、累計「妙味枠(☆) 22.7% (100/441)」は
  ☆ 46/241 + #110 時代 (07/18〜09/04) の妙味○ 46/192 + ☆無しの日の○代用 8/8 の混ぜ物だった。
  → 表示を ☆ にし、☆ が無いレースは数えない。累計は ☆ が付き始めた日 (posted_marks から
    自動判定、実測 2026-09-05) 以降の ☆ だけ。注の累計も同じ期間で並べる。
- 実額 ROI の母集団が「投稿したレース」だけだった (race_rows が posted_marks を回すため)。
  金額が乗ったのに投稿上限で投稿されなかったレース (11/08 福島12R 1,200円→0円) が抜け、
  週末 120.1% (正 114.6%)・累計 79.9% (正 79.5%) と出ていた。
  → 金額の母集団 (all_bets の amount>0) と印の母集団 (投稿した凍結印) を分ける。
    金額の開催日は posted_marks ∪ 予測 JSON から列挙する。posted_marks は実際に配信できた時しか
    書かれない (#150(7)) ので、X も Threads も落ちた日 (posted_marks 無し) の金額も数え、
    冒頭の ⚠️ 行と累計の ※ 行で「投稿なしの日」と書き分ける。その日は対象週末の判定にも入れる。
    未投稿レースは seal されない (#169 以降 seal は投稿したレースだけ) ので、予測が発走後に
    作り直されていた (predictions_cache.created_at が発走時刻以降) レースは look-ahead (#99) の
    疑いとして数えず、件数を出す。

実行: python3 scripts/weekly_scorecard.py [--days YYYYMMDD YYYYMMDD] [--legacy] [--no-v2] [--no-market]
"""
import argparse, glob, json, math, os, random, re, sqlite3, statistics, sys
from itertools import combinations

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_ERA = '20260718'      # 新体制 (◎ブレンド×妙味○×適応型買い目) 開始日
CONF_STABLE = '20260809'  # #120 (信頼度基準の安定化) 以降のみ信頼度別を集計
# #152: 'roi' は撤去 (上記の理由)。#173: 残りも #141 で測った窓が学習データに食い込んでいたと
# 分かっており物差しにならない。表示する時は「参考」と明記し、達成/未達の判定には使わない
DESIGN = {'o_rate': 25, 'chu_rate': 20, 'full': 30}

# 払戻の組み合わせキーが着順を保つ券種
ORDERED_BETS = {'三連単', '馬単'}
BET_ORDER = ['三連複', 'ワイド', '馬連', '三連単', '馬単', '単勝', '複勝']


def db():
    return sqlite3.connect(os.path.join(ROOT, 'keiba.db'))


def _days_of(prefix):
    """docs/data/{prefix}_YYYYMMDD.json がある日付を昇順で返す。

    #173: 旧版は 'posted_marks_2026*.json' の glob で、2027年の開催日を拾えなかった。
    """
    pat = re.compile(rf'^{re.escape(prefix)}_(\d{{8}})\.json$')
    out = []
    for p in glob.glob(os.path.join(ROOT, 'docs', 'data', f'{prefix}_*.json')):
        m = pat.match(os.path.basename(p))
        if m:
            out.append(m.group(1))
    return sorted(out)


def load_marks(day):
    p = os.path.join(ROOT, 'docs', 'data', f'posted_marks_{day}.json')
    return json.load(open(p))['races'] if os.path.exists(p) else {}


def _pred_json(day):
    p = os.path.join(ROOT, 'docs', 'data', f'predictions_{day}.json')
    return json.load(open(p)) if os.path.exists(p) else None


def load_conf(day):
    d = _pred_json(day)
    if not d:
        return {}
    return {r['race_id']: r.get('confidence', '?')
            for v, rs in d.get('venues', {}).items() for r in rs}


def load_bets(day):
    """本番が実際に出した買い目 (投稿時点で凍結) を race_id -> {券種: [line]} で返す。

    line は {'nums': [...], 'amount': int}。amount==0 は「ダッシュボードに表示された
    だけで1円も張っていない」ライン (should_bet=0 のレースは全ライン0、#95)。
    """
    d = _pred_json(day)
    if not d:
        return {}
    out = {}
    for _v, races in d.get('venues', {}).items():
        for r in races:
            ab = r.get('all_bets') or {}
            if not isinstance(ab, dict):
                continue
            lines = {}
            for bt, ls in ab.items():
                acc = []
                for b in ls or []:
                    nums = b.get('horse_numbers')
                    if not nums:
                        try:
                            nums = [int(x) for x in str(b.get('detail', '')).split('-') if x]
                        except ValueError:
                            nums = []
                    if nums:
                        # honor=True が適応型構造 (#113) の本体ライン。
                        # 単勝/複勝/馬連 と一部の三連複は EV フィルタ由来の別レイヤーで、
                        # 混ぜると「◎軸ながし」の共通軸が崩れて構造判定を誤る。
                        acc.append({'nums': nums, 'amount': int(b.get('amount') or 0),
                                    'honor': bool(b.get('honor'))})
                if acc:
                    lines[bt] = acc
            out[str(r['race_id'])] = lines
    return out


def _unit(lines_of_type):
    """そのライン群の1点あたり金額 (中央値)。0 なら資金が乗っていない。"""
    amts = [l['amount'] for l in lines_of_type if l['amount'] > 0]
    return statistics.median(amts) if amts else 0


def classify_structure(lines):
    """本番が **実際に金額を乗せた買い目の形** から適応型構造 (#113) を判定する。

    オッズから分岐を再現するのでなく「何を買ったか」を正とする。#150d の band 優先や
    信頼度C/D の空買い目など、分岐の後段で起きる差し替えを取りこぼさないため。

    band (trio_focus #95) の判別は **点数でなく金額** で行う。betting.py:501 の
    `amount=(amt*2 if trio_focus else None)` は三連複の単価だけを2倍にし点数は増やさない
    ので、点数で見ると band が A (6点) に埋没する。実際それで band 11R (ROI 86.8%) が
    通常A 37R (51.5%) と混ざり、A全体 65.0% という平均値になっていた。
    """
    honor = {bt: [l for l in ls if l['honor']] for bt, ls in lines.items()}
    honor = {bt: ls for bt, ls in honor.items() if ls}
    if not honor:
        return 'EV層のみ' if lines else 'ゼロ:買い目なし'
    funded = {bt: [l for l in ls if l['amount'] > 0] for bt, ls in honor.items()}
    funded = {bt: ls for bt, ls in funded.items() if ls}
    # 資金が乗っていればその形で、1円も乗っていなければ表示された形で分類する
    src = funded or honor
    trio = src.get('三連複') or []
    tri = src.get('三連単') or []
    wide = src.get('ワイド') or []

    tu, wu = _unit(trio), _unit(wide)
    if trio and wu > 0 and tu >= 2 * wu:
        return 'band:三連複◎軸(2倍額)'
    if tri and not trio:
        return f'D:三連単F({len(tri)}点)'
    if len(trio) >= 2:
        common = set.intersection(*(set(l['nums']) for l in trio))
        if len(common) >= 2:
            return 'G:2頭軸'
        if len(common) == 1:
            return 'A:三連複◎軸'
    if trio or tri:
        return 'その他'
    if wide:
        # honor がワイドだけ = #150d 以前の band×構造D 競合。三連複が1点も作られず、
        # 三連単は should_bet=0 で0円に潰れ、結局ワイドしか残らなかったレース。
        return 'ワイドのみ(band×D競合)'
    return 'その他'


def payouts_of(c, rid):
    return {(bt, cb): amt for bt, cb, amt in c.execute(
        "SELECT bet_type, combination, payout_amount FROM payouts WHERE race_id=?", (str(rid),))}


def _key(bet_type, nums):
    seq = nums if bet_type in ORDERED_BETS else sorted(nums)
    return '-'.join(str(x) for x in seq)


def score_actual(pay, lines):
    """実際に金額が乗ったラインだけを確定払戻で採点する。

    返り値: {'spend','ret','hits','points','shown_only','by_type'}
    shown_only = 表示だけされて1円も張っていない点数。
    """
    res = {'spend': 0, 'ret': 0.0, 'hits': 0, 'points': 0, 'shown_only': 0, 'by_type': {},
           'honor': {'spend': 0, 'ret': 0.0, 'hits': 0}, 'ev': {'spend': 0, 'ret': 0.0, 'hits': 0}}
    for bt, ls in lines.items():
        for l in ls:
            amt = l['amount']
            if amt <= 0:
                res['shown_only'] += 1
                continue
            unit = pay.get((bt, _key(bt, l['nums'])), 0)
            gain = unit * (amt / 100.0)
            hit = 1 if unit else 0
            t = res['by_type'].setdefault(bt, {'spend': 0, 'ret': 0.0, 'hits': 0, 'points': 0})
            t['spend'] += amt; t['ret'] += gain; t['points'] += 1; t['hits'] += hit
            lay = res['honor'] if l['honor'] else res['ev']
            lay['spend'] += amt; lay['ret'] += gain; lay['hits'] += hit
            res['spend'] += amt; res['ret'] += gain; res['points'] += 1; res['hits'] += hit
    return res


def uniform_bets(c, rid, mk, fin, meta):
    """[旧基準・参考用] 全投稿レースに一律 1点100円を賭けた場合の仮想採点 (#113 再現)。

    #152 以降これは主指標ではない。本番が1円も賭けていないレースを含むため、
    実際のお金の話をしていない。--legacy でのみ表示する。
    """
    rn = (meta[2] or '')
    sys.path.insert(0, ROOT)
    from race_utils import is_ml_out_of_domain
    if is_ml_out_of_domain(rn):
        return None
    ax = mk['◎']; axhn = ax['horse_number']
    axod = ax.get('odds_win_at_post') or 0
    _val = mk.get('☆') or mk.get('○') or {}
    ohn = _val.get('horse_number')
    others = [e['horse_number'] for e in mk['_all']
              if e['mark'] in ('▲', '△', '×', '○', '☆') and e['horse_number'] != ohn]
    chu = mk.get('注', {}).get('horse_number')
    nh = meta[3] or len(fin)
    pay = payouts_of(c, rid)
    sp = ret = 0
    if 0 < axod < 2.0 and ohn:
        name = 'G:2頭軸'
        for p in others + ([chu] if chu else []):
            sp += 100; ret += pay.get(('三連複', _key('三連複', [axhn, ohn, p])), 0)
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
            sp += 100; ret += pay.get(('三連複', _key('三連複', [axhn, p, q])), 0)
    return (name, sp, ret) if sp else None


def race_rows(c, day):
    """day の投稿レースを (rid, marks, finish_map, meta) で列挙。結果未確定はスキップ。"""
    for rid, lst in load_marks(day).items():
        # #146: △は2頭つく (#143)。dict にすると2頭目が黙って消えるので、
        # 代表1頭の dict と、全頭の順序リストを両方持つ。
        ORDER = ['◎', '○', '▲', '△', '☆', '×', '注']
        mk = {}
        for e in lst:
            mk.setdefault(e['mark'], e)
        mk['_all'] = sorted([e for e in lst if e.get('mark') in ORDER],
                            key=lambda e: ORDER.index(e['mark']))
        if '◎' not in mk:
            continue
        fin = {hn: fp for hn, fp in c.execute(
            "SELECT horse_number, finish_position FROM results WHERE race_id=? AND finish_position>0", (rid,))}
        if not fin or 1 not in fin.values():
            continue
        meta = c.execute("SELECT venue, race_number, race_name, horse_count FROM races WHERE race_id=?",
                         (rid,)).fetchone()
        yield rid, mk, fin, meta


def _made_after_start(c, rid):
    """そのレースの予測 (= all_bets) が発走後に作り直された証拠があれば True。

    未投稿レースは seal されない (#169 以降 seal は投稿したレースだけ) ので、凍結は
    #172 の 9:30 確定ロック頼み。それ以前の日や --force の再予測では、発走後に確定オッズで
    作り直された買い目が入りうる (#99 の look-ahead)。predictions_cache.created_at は
    最後に書いた時刻 (UTC 保存、database.py)。行・時刻が無く判定できない時は False (落とさない)。
    """
    row = c.execute("SELECT datetime(pc.created_at, '+9 hours'), r.race_date, r.start_time "
                    "FROM predictions_cache pc JOIN races r ON r.race_id = pc.race_id "
                    "WHERE pc.race_id = ?", (str(rid),)).fetchone()
    if not row or not row[0] or not row[1] or not row[2]:
        return False
    rd, stt = str(row[1]), str(row[2]).strip()
    if len(rd) == 8 and rd.isdigit():
        rd = f"{rd[:4]}-{rd[4:6]}-{rd[6:]}"
    if len(stt) != 5 or stt[2] != ':':
        return False
    return row[0] >= f"{rd} {stt}:00"


def funded_unposted(c, bets_map, seen, skipped=None):
    """#173: 投稿されなかったが金額が乗ったレースを (rid, lines) で列挙。結果未確定はスキップ。

    race_rows は posted_marks (投稿した凍結印) しか回さないので、投稿上限 (#71) などで
    投稿から落ちたレースの買い目が実額から抜けていた (11/08 福島12R 1,200円→0円)。
    金額の母集団は all_bets の amount>0 が正本 (#152)。印の成績には入れない (印は投稿していない)。
    投稿が1本も配信されなかった日 (posted_marks 無し) は seen が空なので、全レースがここを通る。
    seen = race_rows で既に数えた race_id (str)。
    予測が発走後に作り直されていたレースは数えず、skipped (list) に race_id を積む。
    """
    for rid, lines in bets_map.items():
        if rid in seen:
            continue
        if not any(l['amount'] > 0 for ls in lines.values() for l in ls):
            continue
        # race_rows と同じ「1着が居れば確定」(#91)
        if not c.execute("SELECT 1 FROM results WHERE race_id=? AND finish_position=1 LIMIT 1",
                         (rid,)).fetchone():
            continue
        if _made_after_start(c, rid):
            if skipped is not None:
                skipped.append(rid)
            continue
        yield rid, lines


def _day_settled(c, day):
    """予測 JSON のレースに1着が1つでもあれば True (= 結果が確定し始めた開催日)。

    posted_marks の無い日 (投稿が1本も配信されなかった日) の対象週末判定に使う (#173)。
    posted_marks のある日は従来どおり race_rows (投稿レースの確定) で判定する。
    """
    for rid in load_bets(day):
        if c.execute("SELECT 1 FROM results WHERE race_id=? AND finish_position=1 LIMIT 1",
                     (rid,)).fetchone():
            return True
    return False


def _staked(lines):
    """そのレースで金額が乗ったラインの合計額 (円)。"""
    return sum(l['amount'] for ls in lines.values() for l in ls if l['amount'] > 0)


def add_money(bucket, sc, key):
    """構造ごとの実額集計に1レース分を足す。金額が乗っていれば True、表示のみなら False。"""
    m = bucket.setdefault(key, {'spend': 0, 'ret': 0.0, 'hits': 0, 'races': 0, 'shown': 0})
    m['races'] += 1
    if sc['spend'] > 0:
        m['spend'] += sc['spend']; m['ret'] += sc['ret']; m['hits'] += sc['hits']
        return True
    m['shown'] += 1
    return False


def first_star_day(days):
    """posted_marks で ☆ が最初に付いた開催日 (#140 の新設。実測 2026-09-05)。無ければ None。

    ☆ の累計はこの日以降だけ。それ以前の妙味枠は ○ (#110) で、#140 で ○ は実力2位に
    意味が変わったため、記号で混ぜると別物の合算になる (#173)。
    """
    for d in days:
        if any(e.get('mark') == '☆' for lst in load_marks(d).values() for e in lst):
            return d
    return None


def _race_label(c, rid):
    r = c.execute("SELECT venue, race_number FROM races WHERE race_id=?", (str(rid),)).fetchone()
    return f"{r[0]}{r[1]}R" if r and r[0] else str(rid)


def boot(pairs, seed=42, n_boot=4000):
    """(spend, return) をレース単位でリサンプルした ROI の 95%CI と標準誤差。

    旧実装は分子だけリサンプルして **分母は元データ固定** だった。全レース同額の
    仮想採点では偶然一致していたが、実額 (レースごとに金額が違う) では誤る。
    """
    if not pairs:
        return {'lo': 0.0, 'hi': 0.0, 'se': 0.0}
    rnd = random.Random(seed)
    n = len(pairs)
    boots = []
    for _ in range(n_boot):
        smp = rnd.choices(pairs, k=n)
        sp = sum(x[0] for x in smp)
        boots.append(100 * sum(x[1] for x in smp) / sp if sp else 0.0)
    se = statistics.stdev(boots) if len(boots) > 1 else 0.0
    boots.sort()
    return {'lo': boots[int(n_boot * 0.025)], 'hi': boots[int(n_boot * 0.975) - 1], 'se': se}


def mde2(se_self, se_rest):
    """2群の差として 80%検出力・両側5% で見える最小差 (pt)。

    1標本の 2.8*SD/sqrt(n) を「他の買い目と区別できるか」の直下に出すと過小になる
    (実測 A: ±37pt と出るが2標本では ±91pt)。金額加重の推定量に合わせて
    bootstrap 分布の SD を SE として使う。
    """
    return 2.8 * math.sqrt(se_self ** 2 + se_rest ** 2)


def concentration(rows):
    """単日支配と一撃依存を測る (CLAUDE.md #26 の必須チェック)。

    rows: [(spend, return, day), ...]
    """
    tot_sp = sum(r[0] for r in rows)
    tot_rt = sum(r[1] for r in rows)
    if not tot_sp:
        return None
    by_day = {}
    for sp, rt, d in rows:
        e = by_day.setdefault(d, [0, 0.0])
        e[0] += sp; e[1] += rt
    top_day = max(by_day.items(), key=lambda kv: kv[1][1])
    rest_sp = tot_sp - top_day[1][0]
    rest_rt = tot_rt - top_day[1][1]
    hits = sorted((r[1] for r in rows if r[1] > 0), reverse=True)
    return {
        'roi': 100 * tot_rt / tot_sp,
        'top_day': top_day[0],
        'top_day_spend_share': 100 * top_day[1][0] / tot_sp,
        'top_day_return_share': 100 * top_day[1][1] / tot_rt if tot_rt else 0.0,
        'roi_excl': 100 * rest_rt / rest_sp if rest_sp else 0.0,
        'top1_share': 100 * hits[0] / tot_rt if hits and tot_rt else 0.0,
        'top3_share': 100 * sum(hits[:3]) / tot_rt if hits and tot_rt else 0.0,
    }


def _roi(sp, ret):
    return 100 * ret / sp if sp else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', nargs='*', help='対象日 YYYYMMDD (省略時は自動判定)')
    ap.add_argument('--legacy', action='store_true',
                    help='旧基準 (全投稿レース一律100円の仮想ポートフォリオ) も併記する')
    # #169: 予測時点の市場との比較 (印 vs 人気・LGB vs 同時刻市場・初替わり・投資ゲート) と
    # v3 と v2 の対応比較。前者は git 履歴の予測 JSON を読むので数十秒かかる。
    ap.add_argument('--no-market', action='store_true', help='予測時点の市場との比較 (#169) を省く')
    # #169 (レビュー指摘): 旧版は --v2 を付けた時だけ計算し、付け忘れると「今週」の行が黙って
    # 消えて累計だけが出た。既定で対象日の未計算分を計算する (--no-v2 で省く)。
    ap.add_argument('--v2', dest='v2', action='store_true', default=True,
                    help='v3 と v2 の対応比較を、まだ貯めていないレースについて計算する (既定)')
    ap.add_argument('--no-v2', dest='v2', action='store_false',
                    help='対応比較の計算を省く (特徴量の作り直しで1レース約70秒かかるため)。'
                         '未計算の数は出す')
    ap.add_argument('--v2-workers', type=int, default=4)
    args = ap.parse_args()
    c = db()
    # 印の開催日 = posted_marks のある日 (投稿した凍結印)
    era_days = [d for d in _days_of('posted_marks') if d >= NEW_ERA]
    marks_set = set(era_days)
    # #173: 金額の開催日 = posted_marks ∪ 予測 JSON。posted_marks は実際に配信できた時しか
    # 書かれない (#150(7)) ので、X も Threads も落ちた日は posted_marks が無いが、all_bets には
    # 金額が乗っている。旧版は posted_marks だけを回していたため、その日の金額が警告なしで
    # 累計から消えていた (roi_weekly_monitor は予測 JSON の日付を回すので2つの実額がずれる)。
    money_days = sorted(marks_set | {d for d in _days_of('predictions') if d >= NEW_ERA})
    missing_json = [d for d in era_days if _pred_json(d) is None]
    if missing_json:
        print(f"⚠️ 買い目JSONが無い開催日 {len(missing_json)}件 → 集計から静かに落ちる: "
              f"{', '.join(missing_json)}\n")
    # missing_json の逆向き: 金額が乗って着順も確定したのに posted_marks が無い開催日
    nopost_warn = []
    for d in money_days:
        if d in marks_set:
            continue
        late_d = []
        rows = list(funded_unposted(c, load_bets(d), set(), late_d))
        if rows or late_d:
            nopost_warn.append(f"{d} ({len(rows)}R {sum(_staked(ls) for _, ls in rows):,}円"
                               + (f"・発走後に作り直されていて数えない{len(late_d)}R" if late_d else "") + ")")
    if nopost_warn:
        print(f"⚠️ 金額が乗って着順も確定したのに posted_marks が無い開催日 (投稿が1本も配信されなかった日) "
              f"{len(nopost_warn)}件 → 金額は実額に含め、印の成績には入れない (#173): "
              f"{', '.join(nopost_warn)}\n")
    if args.days:
        target = args.days
        how = '指定'
    else:
        # #173: 投稿の無かった開催日も「結果が確定した開催日」として数える (旧版はその日を飛ばし、
        # 対象週末が前の週にずれていた)
        settled = [d for d in money_days
                   if (any(True for _ in race_rows(c, d)) if d in marks_set else _day_settled(c, d))]
        target = settled[-2:]
        how = '自動判定'
    print(f"=== 対象週末: {', '.join(target)} ({how}) ===\n")

    wk = {'spend': 0, 'ret': 0.0}
    for day in target:
        st = dict(n=0, axw=0, axt3=0, full=0, o=0, on=0, opop=[], chu=0, chun=0)
        byconf = {}
        conf_map = load_conf(day)
        bets_map = load_bets(day)
        money = {}          # 構造 -> {'spend','ret','hits','races','shown_races'}
        legacy = {}
        seen = set()        # #173: 印の母集団 (投稿した凍結印) として数えた race_id
        unposted = []       # #173: 金額だけの母集団 [(rid, spend, ret)]
        for rid, mk, fin, meta in race_rows(c, day):
            seen.add(str(rid))
            top3 = {hn for hn, fp in fin.items() if fp <= 3}
            axhn = mk['◎']['horse_number']
            st['n'] += 1
            st['axw'] += (fin.get(axhn) == 1)
            st['axt3'] += (axhn in top3)
            # #150: #143 の設計値 (印5頭で33.9%) と比較するため、母集団は
            # 捕捉印5頭 (◎○▲△△) のみ。☆ は穴枠なので入れない。
            five = [e['horse_number'] for e in mk['_all']
                    if e['mark'] in ('◎', '○', '▲', '△', '×')]
            st['full'] += (len(set(five) & top3) == 3)
            # #173: ☆ だけを数える。旧版は ☆ が無いレースで ○ (実力2位) を代用していた
            _v = mk.get('☆')
            if _v:
                st['on'] += 1
                st['o'] += (_v['horse_number'] in top3)
                st['opop'].append(_v.get('popularity_at_post') or 0)
            if '注' in mk:
                st['chun'] += 1
                st['chu'] += (mk['注']['horse_number'] in top3)
            cf = conf_map.get(rid, '?')
            if cf in 'SABCD' and day >= CONF_STABLE:
                b = byconf.setdefault(cf, [0, 0]); b[1] += 1; b[0] += (axhn in top3)

            lines = bets_map.get(str(rid), {})
            sc = score_actual(payouts_of(c, rid), lines)
            if add_money(money, sc, classify_structure(lines)):
                wk['spend'] += sc['spend']; wk['ret'] += sc['ret']
            if args.legacy:
                lb = uniform_bets(c, rid, mk, fin, meta)
                if lb:
                    s = legacy.setdefault(lb[0], [0, 0, 0])
                    s[0] += lb[1]; s[1] += lb[2]; s[2] += 1
        # #173: 投稿から落ちたが金額が乗ったレース (印の成績には入れない)
        late = []
        for rid, lines in funded_unposted(c, bets_map, seen, late):
            sc = score_actual(payouts_of(c, rid), lines)
            if add_money(money, sc, classify_structure(lines)):
                wk['spend'] += sc['spend']; wk['ret'] += sc['ret']
            unposted.append((rid, sc['spend'], sc['ret']))

        n = st['n'] or 1
        opop = statistics.mean(st['opop']) if st['opop'] else 0
        # #173: posted_marks の無い日 = 投稿が1本も配信されなかった日。印の成績は無く、金額だけ数える
        nopost_day = not load_marks(day)
        if nopost_day:
            # 「配信されなかった」と言えるのは、予測 JSON があって結果が確定し始めている時だけ。
            # --days で開催の無い日・まだ走っていない日を渡した時に、配信の失敗と断定しない
            if not load_bets(day):
                why0 = "posted_marks も予測 JSON も無い (開催日でないか日付の指定違い)"
            elif not _day_settled(c, day):
                why0 = "posted_marks が無く、結果も未確定"
            else:
                why0 = "posted_marks が無い (投稿が1本も配信されなかった日)。印の成績は無し、金額だけ数える (#173)"
            print(f"【{day[4:6]}/{day[6:]}】投稿0R — {why0}")
        else:
            print(f"【{day[4:6]}/{day[6:]}】投稿{st['n']}R")
            print(f"  ◎勝率{st['axw']}/{st['n']} ◎複勝{st['axt3']}/{st['n']} ({100*st['axt3']/n:.0f}%) 完全捕捉{st['full']}/{st['n']}")
            star = f"☆複勝{st['o']}/{st['on']} ({opop:.1f}人気)" if st['on'] else "☆なし"
            print(f"  {star} 注{st['chu']}/{st['chun']}")
        if byconf:
            print("  信頼度別◎複勝: " + ' '.join(f"{k}:{v[0]}/{v[1]}" for k, v in sorted(byconf.items())))
        print("  買い目 (実額):")
        if not money:
            print("    金額が乗って着順も確定したレースなし")
        for k, m in sorted(money.items()):
            invested = m['races'] - m['shown']
            if m['spend']:
                print(f"    {k}: 投資{invested}R {m['spend']:,}円→{m['ret']:,.0f}円 "
                      f"ROI {_roi(m['spend'], m['ret']):.0f}% 的中{m['hits']}本"
                      + (f" / 表示のみ{m['shown']}R" if m['shown'] else ""))
            else:
                print(f"    {k}: {m['races']}R すべて表示のみ (投資額0円)")
        if unposted:
            why = "投稿なしの日" if nopost_day else "未投稿 (投稿上限など) "
            print(f"    ※ {why}だが金額が乗ったレースも実額に含めた (#173): "
                  + ' / '.join(f"{_race_label(c, r)} {sp:,}円→{rt:,.0f}円" for r, sp, rt in unposted))
        if late:
            print("    ⚠️ 未投稿で金額が乗ったが予測が発走後に作り直されていたため数えないレース (#173、look-ahead の疑い): "
                  + ' / '.join(_race_label(c, r) for r in late))
        if args.legacy and legacy:
            print("  [旧基準・参考] 全レース一律100円の仮想:")
            for k, (a, b, cnt) in sorted(legacy.items()):
                print(f"    {k}: {cnt}R {a:,}円→{b:,}円 ROI {100*b/max(a,1):.0f}%")
        print()
    print(f"週末合計 (実額): {wk['spend']:,}円 → {wk['ret']:,.0f}円 = ROI {_roi(wk['spend'], wk['ret']):.1f}%\n")

    # ── 累計 (新体制) ──
    # #173: ☆ と注の累計は ☆ が付き始めた日以降だけ (それ以前の妙味枠は ○ で別物)
    star_from = first_star_day(era_days)
    agg = {}; conf_agg = {}; o_tot = [0, 0]; chu_tot = [0, 0]; by_type = {}
    pairs_by_struct = {}
    layer = {'honor': {'spend': 0, 'ret': 0.0, 'hits': 0}, 'ev': {'spend': 0, 'ret': 0.0, 'hits': 0}}
    legacy_agg = {}
    unposted_tot = [0, 0, 0.0]      # #173: 金額だけのレース [R数, spend, ret] (投稿のあった日)
    nopost_tot = [0, 0, 0.0]        # #173: 投稿が1本も配信されなかった日 (posted_marks 無し) の分
    late_tot = []                   # #173: 予測が発走後に作り直されていて数えなかったレース

    def add_cum(rid, lines, d):
        sc = score_actual(payouts_of(c, rid), lines)
        key = classify_structure(lines)
        if add_money(agg, sc, key):
            pairs_by_struct.setdefault(key, []).append((sc['spend'], sc['ret'], d))
            for bt, t in sc['by_type'].items():
                g = by_type.setdefault(bt, {'spend': 0, 'ret': 0.0, 'hits': 0, 'points': 0})
                g['spend'] += t['spend']; g['ret'] += t['ret']
                g['hits'] += t['hits']; g['points'] += t['points']
            for lk in ('honor', 'ev'):
                layer[lk]['spend'] += sc[lk]['spend']
                layer[lk]['ret'] += sc[lk]['ret']
                layer[lk]['hits'] += sc[lk]['hits']
        return sc

    # #173: 金額は money_days (posted_marks ∪ 予測 JSON) を回す。posted_marks の無い日は
    # race_rows が空なので、印の成績には何も入らず、全レースが funded_unposted を通る
    for d in money_days:
        conf_map = load_conf(d)
        bets_map = load_bets(d)
        seen = set()
        for rid, mk, fin, meta in race_rows(c, d):
            seen.add(str(rid))
            top3 = {hn for hn, fp in fin.items() if fp <= 3}
            axhn = mk['◎']['horse_number']
            if star_from and d >= star_from:
                _v = mk.get('☆')        # #173: ☆ が無いレースは数えない (○ で代用しない)
                if _v:
                    o_tot[1] += 1; o_tot[0] += (_v['horse_number'] in top3)
                if '注' in mk:
                    chu_tot[1] += 1; chu_tot[0] += (mk['注']['horse_number'] in top3)
            if d >= CONF_STABLE:
                cf = conf_map.get(rid, '?')
                if cf in 'SABCD':
                    b = conf_agg.setdefault(cf, [0, 0]); b[1] += 1; b[0] += (axhn in top3)

            add_cum(rid, bets_map.get(str(rid), {}), d)
            if args.legacy:
                lb = uniform_bets(c, rid, mk, fin, meta)
                if lb:
                    s = legacy_agg.setdefault(lb[0], [0, 0, 0])
                    s[0] += lb[1]; s[1] += lb[2]; s[2] += 1
        # #173: 投稿から落ちたが金額が乗ったレース (印の成績には入れない)
        t = unposted_tot if d in marks_set else nopost_tot
        for rid, lines in funded_unposted(c, bets_map, seen, late_tot):
            sc = add_cum(rid, lines, d)
            t[0] += 1; t[1] += sc['spend']; t[2] += sc['ret']

    print(f"=== 新体制累計 ({money_days[0][4:6]}/{money_days[0][6:]}〜) — 実額ベース ===")
    gsp = sum(m['spend'] for m in agg.values())
    gret = sum(m['ret'] for m in agg.values())
    for k, m in sorted(agg.items()):
        invested = m['races'] - m['shown']
        if m['spend']:
            pr = [(a, b) for a, b, _ in pairs_by_struct.get(k, [])]
            ci = ''
            if len(pr) >= 5:
                bs = boot(pr)
                ci = f" [95%CI {bs['lo']:.0f}-{bs['hi']:.0f}]"
            print(f"  {k}: 投資{invested}R {m['spend']:,}円→{m['ret']:,.0f}円 "
                  f"ROI {_roi(m['spend'], m['ret']):.1f}%{ci} "
                  f"的中{m['hits']}本" + (f" / 表示のみ{m['shown']}R" if m['shown'] else ""))
        else:
            print(f"  {k}: {m['races']}R すべて表示のみ — **投資額0円なのでROIは存在しない**")
    print(f"  総合: {gsp:,}円→{gret:,.0f}円 ROI {_roi(gsp, gret):.1f}%")
    if unposted_tot[0]:
        print(f"    ※ うち未投稿 (投稿上限など) で金額が乗ったレース {unposted_tot[0]}R "
              f"({unposted_tot[1]:,}円→{unposted_tot[2]:,.0f}円) を含む (#173)。印の成績は投稿した凍結印だけで数える")
    if nopost_tot[0]:
        print(f"    ※ うち投稿なしの日 (posted_marks 無し = 投稿が1本も配信されなかった日) の金額 {nopost_tot[0]}R "
              f"({nopost_tot[1]:,}円→{nopost_tot[2]:,.0f}円) を含む (#173)。この日の印の成績は無い")
    if late_tot:
        print(f"    ⚠️ 未投稿で金額が乗ったが予測が発走後に作り直されていたため数えなかったレース {len(late_tot)}R "
              f"(#173、look-ahead の疑い): {', '.join(late_tot[:10])}" + (" ほか" if len(late_tot) > 10 else ""))
    if layer['honor']['spend'] or layer['ev']['spend']:
        print("  レイヤー別 (実額): "
              f"適応型構造 {layer['honor']['spend']:,}円→{layer['honor']['ret']:,.0f}円 "
              f"ROI {_roi(layer['honor']['spend'], layer['honor']['ret']):.1f}% / "
              f"EVフィルタ層 {layer['ev']['spend']:,}円→{layer['ev']['ret']:,.0f}円 "
              f"ROI {_roi(layer['ev']['spend'], layer['ev']['ret']):.1f}%")
    if by_type:
        print("  券種別 (実額):")
        for bt in sorted(by_type, key=lambda x: BET_ORDER.index(x) if x in BET_ORDER else 99):
            t = by_type[bt]
            print(f"    {bt}: {t['points']}点 {t['spend']:,}円→{t['ret']:,.0f}円 "
                  f"ROI {_roi(t['spend'], t['ret']):.1f}% 的中{t['hits']}本")
    # #173: ☆ が付いたレースだけ (○ で代用しない)。注も同じ期間で並べる
    if star_from:
        print(f"  妙味枠(☆)複勝率 累計 ({star_from[4:6]}/{star_from[6:]}〜・☆が付いたレースのみ): "
              f"{o_tot[0]}/{o_tot[1]} = {100*o_tot[0]/max(o_tot[1],1):.1f}% / "
              f"注 {chu_tot[0]}/{chu_tot[1]} = {100*chu_tot[0]/max(chu_tot[1],1):.1f}%")
        print(f"      ※ 旧設計値 (☆{DESIGN['o_rate']}%・注{DESIGN['chu_rate']}%) は参考。"
              "測った窓が学習データに食い込んでいたと #141 で分かったため、達成/未達の判定には使わない")
    else:
        print("  妙味枠(☆)複勝率 累計: ☆ が付いたレースがまだ無い")
    if conf_agg:
        print("  信頼度別◎複勝 (#120以降): " + ' '.join(
            f"{k}:{v[0]}/{v[1]}({100*v[0]/v[1]:.0f}%)" for k, v in sorted(conf_agg.items())))

    # ── 構造の判定: 設計値でなく「残りの買い目と区別できるか」で見る (#152) ──
    print("\n  構造の判定 (設計値との比較は #141 で無効と判明したため行わない):")
    for k, rows in sorted(pairs_by_struct.items()):
        pr = [(a, b) for a, b, _ in rows]
        con = concentration(rows)
        if len(pr) < 20:
            roi = _roi(sum(x[0] for x in pr), sum(x[1] for x in pr))
            print(f"    {k}: n={len(pr)} ROI {roi:.1f}% — 標本不足、他との比較はしない")
            if con:
                delta = con['roi_excl'] - con['roi']
                warn = ' ⚠️' if abs(delta) >= 20 or con['top_day_spend_share'] >= 20 else ''
                print(f"        単日支配: {con['top_day']} が回収の{con['top_day_return_share']:.0f}% "
                      f"→ 除くと {con['roi_excl']:.1f}% ({delta:+.1f}pt){warn} / "
                      f"上位1的中が回収の{con['top1_share']:.0f}%")
            continue
        rest = [(a, b) for kk, v in pairs_by_struct.items() if kk != k for a, b, _ in v]
        bs, br = boot(pr), (boot(rest) if rest else {'lo': 0, 'hi': 0, 'se': 0})
        roi = _roi(sum(x[0] for x in pr), sum(x[1] for x in pr))
        rroi = _roi(sum(x[0] for x in rest), sum(x[1] for x in rest)) if rest else 0.0
        overlap = not (bs['hi'] < br['lo'] or bs['lo'] > br['hi'])
        note = "他の買い目と区別できない" if overlap else "他の買い目と区別できる (要検討)"
        print(f"    {k}: {len(pr)}R ROI {roi:.1f}% [{bs['lo']:.0f}-{bs['hi']:.0f}] vs 残り "
              f"{rroi:.1f}% [{br['lo']:.0f}-{br['hi']:.0f}] → {note}")
        print(f"        残りとの差として見える最小差 ±{mde2(bs['se'], br['se']):.0f}pt "
              f"(この n での検出力の限界)")
        if con:
            delta = con['roi_excl'] - con['roi']
            warn = ' ⚠️' if abs(delta) >= 20 or con['top_day_spend_share'] >= 20 else ''
            print(f"        単日支配: 最大寄与日 {con['top_day']} が回収の"
                  f"{con['top_day_return_share']:.0f}% (投資の{con['top_day_spend_share']:.0f}%) "
                  f"→ 除くと ROI {con['roi_excl']:.1f}% ({delta:+.1f}pt){warn}")
            print(f"        一撃依存: 上位1的中が回収の{con['top1_share']:.0f}% / "
                  f"上位3件で{con['top3_share']:.0f}%")

    if args.legacy and legacy_agg:
        print("\n  [旧基準・参考] 全投稿レースに一律100円を賭けた仮想ポートフォリオ:")
        lsp = sum(v[0] for v in legacy_agg.values()); lrt = sum(v[1] for v in legacy_agg.values())
        for k, (sp, ret, cnt) in sorted(legacy_agg.items()):
            print(f"    {k}: {cnt}R ROI {100*ret/max(sp,1):.1f}%")
        print(f"    総合: {lsp:,}円→{lrt:,}円 ROI {100*lrt/max(lsp,1):.1f}%")
        print("    ※ 本番が1円も賭けていないレースを含むため、実際のお金の話ではない")

    # ── #169: 予測時点の市場との比較 / v3 と v2 の対応比較 ──
    # 11/05 の反省で「LGB は確定オッズで学習・評価されているが、本番は朝の薄いオッズで動き、
    # 同時刻の市場とほぼ同じ」「印5頭は予測時の人気上位5頭より捕捉が少ない (#147 の代償)」
    # が分かったが、どちらも週次の監視に無かった。v3 の評価も1週末の勝ち負けでなく、
    # 同じレースの対応比較を貯めて累積で見る。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    # #170: 2セクションは例外を握って本体の ROI 集計を守るが、握るだけだと欠けたレポートが
    # exit 0 で commit され、weekly_report.yml の run は緑のまま誰にも届かなかった。
    # 劣化 (例外で落ちた / 今週の予測時オッズがスナップショットからも履歴からも1レースも取れない) を
    # 末尾の1行に残し、WEEKLY_REPORT_FAIL_ON_DEGRADED=1 (weekly_report.yml) の時だけ終了コード 3 で返す
    # (手元の実行の終了コードは変えない)。スナップショットがあれば履歴が無くても今週の (a)(b) は出るので
    # 劣化扱いにしない。
    # #170 (検証の指摘): 判定は出力そのもの (今週の行の出どころ) で行う。旧版は「履歴の clone が
    # 読めるか (available)」も条件にしていたが、available は HEAD を見るだけなので、clone はできて
    # 予測 JSON の版 (blob) の遅延取得だけが落ちた時は (a)(b) が消えても exit 0 のまま緑で commit され、
    # その版が「完全版」として火曜の上書きからも守られていた (check_raceday_completeness の
    # 「版が読めなかった」と同じ故障)。履歴が健全なら投稿前の版の中に必ず同じ予測の版がある
    # (投稿直前の版そのもの) ので、今週の行が1件以上あって予測時の出どころが0件なら、どの原因でも
    # (a)(b) は欠けている。available は理由の書き分けにだけ使う。
    import traceback
    degraded = []
    if not args.no_market:
        try:
            import weekly_market_compare as wmc
            res = wmc.report(target, conn=c) or {}
            wk_m = res.get('week') or []
            if wk_m and not any(r.get('src') == 'スナップショット' or str(r.get('src') or '').startswith('git:')
                                for r in wk_m):
                # report() と同じ場所 (RACEDAY_HISTORY_REPO か このリポジトリ) の履歴が読めるか (理由の書き分け)
                if wmc.ph.History(os.environ.get('RACEDAY_HISTORY_REPO') or wmc.ROOT).available():
                    why = ("予測 JSON の履歴はあるが今週の版を1つも読めない (blob の取得失敗・投稿前の公開版なし・"
                           "shallow の境界より前のどれか)")
                else:
                    why = "予測 JSON の履歴も読めない"
                degraded.append(f"今週 {len(wk_m)}R の予測時オッズがスナップショットに無く、{why} "
                                f"((a) 予測時・(b) が出ていない)")
        except Exception as e:
            print(f"\n⚠️ 予測時点の市場との比較 (#169) に失敗: {type(e).__name__}: {e}")
            traceback.print_exc()       # #170: 詳細は stderr (CI のログ) に。レポート本文は上の1行
            degraded.append(f"予測時点の市場との比較が例外で落ちた ({type(e).__name__})")
    try:
        import scorecard_v2_paired as v2p
        print("\n=== 点数表 v3 (本番) と v2 の対応比較 (#169) ===")
        if args.v2:
            v2p.compute(target, c, workers=args.v2_workers)
        v2p.report(target, conn=c)
    except Exception as e:
        print(f"\n⚠️ v3/v2 の対応比較 (#169) に失敗: {type(e).__name__}: {e}")
        traceback.print_exc()
        degraded.append(f"v3/v2 の対応比較が例外で落ちた ({type(e).__name__})")
    if degraded:
        print("\n⚠️ 週次レポートの劣化 (#170): " + " / ".join(degraded))
        if os.environ.get('WEEKLY_REPORT_FAIL_ON_DEGRADED') == '1':
            sys.exit(3)


if __name__ == '__main__':
    main()
