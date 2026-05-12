"""平日コンテンツ生成エンジン

post_x.py の cmd_morning / cmd_weekday / cmd_evening から呼ばれ、
今週末重賞のコース統計と血統データから「内容の濃い」ツイートを生成する。

設計方針:
- race_date と today から **動的に** 「明日(土)」「明後日(日)」「日曜は」を決める
- セクション(枠順/脚質/上がり/騎手/種牡馬/母父/人気)を再利用可能な関数で提供
- 月-木: 重賞ローテーション(朝Race1/昼Race2/夜Race3)
- 金: 土曜重賞(昼)・日曜重賞(夜)を別々に取り上げる
"""

from datetime import datetime, timezone, timedelta, date as date_cls

WEEKDAY_LABELS = ['月', '火', '水', '木', '金', '土', '日']


# ───────────────────────────────────────────
# 日付判定ヘルパー
# ───────────────────────────────────────────

def now_jst():
    return datetime.now(timezone(timedelta(hours=9)))


def parse_race_date(race):
    """race['race_date'] (YYYY-MM-DD or YYYYMMDD) を date オブジェクトに"""
    s = race.get('race_date', '') if isinstance(race, dict) else race
    if not s:
        return None
    s = str(s)
    try:
        if '-' in s:
            return date_cls.fromisoformat(s[:10])
        if len(s) == 8 and s.isdigit():
            return date_cls(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except Exception:
        return None
    return None


def race_day_phrase(race, today_d=None, with_paren=True):
    """raceの開催日に基づくラベル。

    delta=0 → '本日(日)'
    delta=1 → '明日(土)'
    delta=2 → '明後日(日)'
    delta>=3 → '今週末(日)' / '日曜'
    delta<0(過去レース) → '先週末(日)' (フォールバック表記)
    """
    if today_d is None:
        today_d = now_jst().date()
    rd = parse_race_date(race)
    if not rd:
        return ''
    delta = (rd - today_d).days
    wd = WEEKDAY_LABELS[rd.weekday()]
    paren = f"({wd})" if with_paren else wd
    if delta < 0:
        # 過去レース。本番では発生しないが念のためのフォールバック
        return f"先週{paren}"
    if delta == 0:
        return f"本日{paren}"
    if delta == 1:
        return f"明日{paren}"
    if delta == 2:
        return f"明後日{paren}"
    if delta == 3:
        return f"3日後{paren}"
    if 3 < delta < 7:
        return f"今週末{paren}"
    return wd + '曜'


def predict_announce_phrase(race, today_d=None):
    """そのレースの AI 予想配信タイミング案内。

    delta<0(過去): 空文字を返して案内をスキップ
    delta==0    : 本日朝に配信済み案内
    delta==1    : 明日朝
    その他       : N曜朝(MM/DD)
    """
    if today_d is None:
        today_d = now_jst().date()
    rd = parse_race_date(race)
    if not rd:
        return ""
    delta = (rd - today_d).days
    wd = WEEKDAY_LABELS[rd.weekday()] + '曜'
    if delta < 0:
        return ""
    if delta == 0:
        return "📢 本日朝のAI予想は配信済み"
    if delta == 1:
        return "📢 明日朝AI予想を配信します🔔"
    md = f"{rd.month}/{rd.day}"
    return f"📢 {wd}朝({md})にAI予想を配信予定🔔"


def split_weekend_races(races):
    """重賞リストを土曜・日曜に分ける。各日内ではグレード順でソート維持。"""
    sat, sun = [], []
    for r in races:
        rd = parse_race_date(r)
        if not rd:
            continue
        if rd.weekday() == 5:
            sat.append(r)
        elif rd.weekday() == 6:
            sun.append(r)
    return sat, sun


# ───────────────────────────────────────────
# レース基本情報フォーマッタ
# ───────────────────────────────────────────

def race_header_line(race, day_phrase=None, today_d=None):
    """🏆 レース名 (G1) | 東京芝1600m | 明日(土)"""
    if day_phrase is None:
        day_phrase = race_day_phrase(race, today_d)
    grade = (race.get('grade') or '').strip()
    grade_label = f"({grade})" if grade else ""
    return f"🏆 {race.get('race_name','')}{grade_label} | {race['venue']}{race['surface']}{race['distance']}m | {day_phrase}"


# ───────────────────────────────────────────
# DB直接集計: 種牡馬/母父 (コース過去6年)
# ───────────────────────────────────────────

def get_sire_top(conn, venue, surface, distance, min_runs=5, top=10, year_min=2020):
    """コース全体の sire 複勝率TOP (top多めに取り、後で出走馬とクロス)"""
    rows = conn.execute("""
        SELECT h.sire AS name, COUNT(*) AS runs,
               ROUND(100.0*SUM(CASE WHEN r.finish_position<=3 THEN 1 ELSE 0 END)/COUNT(*),1) AS top3,
               SUM(CASE WHEN r.finish_position=1 THEN 1 ELSE 0 END) AS wins
        FROM results r JOIN races ra ON r.race_id=ra.race_id
        JOIN horses h ON r.horse_id=h.horse_id
        WHERE ra.venue=? AND ra.surface=? AND ra.distance=?
          AND r.finish_position>0 AND h.sire IS NOT NULL AND h.sire!=''
          AND ra.race_date>=?
        GROUP BY h.sire HAVING runs>=?
        ORDER BY top3 DESC, runs DESC LIMIT ?
    """, (venue, surface, distance, f"{year_min}-01-01", min_runs, top)).fetchall()
    return [dict(r) for r in rows]


def get_damsire_top(conn, venue, surface, distance, min_runs=5, top=10, year_min=2020):
    rows = conn.execute("""
        SELECT h.damsire AS name, COUNT(*) AS runs,
               ROUND(100.0*SUM(CASE WHEN r.finish_position<=3 THEN 1 ELSE 0 END)/COUNT(*),1) AS top3
        FROM results r JOIN races ra ON r.race_id=ra.race_id
        JOIN horses h ON r.horse_id=h.horse_id
        WHERE ra.venue=? AND ra.surface=? AND ra.distance=?
          AND r.finish_position>0 AND h.damsire IS NOT NULL AND h.damsire!=''
          AND ra.race_date>=?
        GROUP BY h.damsire HAVING runs>=?
        ORDER BY top3 DESC, runs DESC LIMIT ?
    """, (venue, surface, distance, f"{year_min}-01-01", min_runs, top)).fetchall()
    return [dict(r) for r in rows]


def get_workouts_for_race(conn, race_id):
    """対象レースの追い切り評価一覧 (馬番順)"""
    rows = conn.execute("""
        SELECT w.horse_number AS num, h.horse_name AS name,
               w.evaluation_grade AS grade, w.evaluation_text AS text
        FROM workouts w
        LEFT JOIN horses h ON w.horse_id = h.horse_id
        WHERE w.race_id = ?
        ORDER BY w.horse_number
    """, (race_id,)).fetchall()
    return [dict(r) for r in rows]


def workout_section(workouts, max_show=4):
    """調教評価セクション(A評価馬を中心に表示)"""
    if not workouts:
        return None
    a_grade = [w for w in workouts if w.get('grade') == 'A']
    b_grade = [w for w in workouts if w.get('grade') == 'B']
    if not a_grade and not b_grade:
        return None
    lines = ["【追い切り評価(netkeiba)】"]
    if a_grade:
        for w in a_grade[:max_show]:
            lines.append(f"  🏅A {w['num']}番 {w['name']} ({w['text']})")
    elif b_grade:
        # Aがいない場合は上位B評価の中から目立つコメントを抜粋
        prio_b = [w for w in b_grade if w.get('text') and any(
            kw in w['text'] for kw in ('絶好','気力','上積','好気配','益々','気配上'))]
        for w in prio_b[:max_show]:
            lines.append(f"  ◯B {w['num']}番 {w['name']} ({w['text']})")
        if not prio_b:
            return None
    return "\n".join(lines)


def get_entry_pedigree(conn, race_id):
    """そのレースの出走馬の (horse_name, sire, damsire) 一覧。
    血統未取得の馬はスキップ。
    """
    rows = conn.execute("""
        SELECT h.horse_name AS name, h.sire AS sire, h.damsire AS damsire,
               r.horse_number AS num, r.popularity AS pop
        FROM results r LEFT JOIN horses h ON r.horse_id = h.horse_id
        WHERE r.race_id = ? AND h.sire IS NOT NULL AND h.sire != ''
        ORDER BY r.horse_number
    """, (race_id,)).fetchall()
    return [dict(r) for r in rows]


def cross_reference_sires(top_list, entries, key='sire', limit=3):
    """top_list (course top sires) を entries の sire/damsire と突き合わせ、
    出走馬がいる項目のみ抽出。各項目に出走馬の馬名リストを付与。
    """
    if not entries or not top_list:
        return []
    matched = []
    for item in top_list:
        name = item['name']
        horses = [e['name'] for e in entries if e.get(key) == name]
        if horses:
            matched.append({**item, 'entries': horses})
        if len(matched) >= limit:
            break
    return matched


# ───────────────────────────────────────────
# AI synthesis: 出走馬×複数軸スコア
# ───────────────────────────────────────────

def get_jockey_course_top(conn, venue, surface, distance,
                           min_runs=10, top=20, year_min=2020):
    """コース×距離 過去6年の騎手成績TOP (cross 参照用に多めに取る)"""
    rows = conn.execute("""
        SELECT j.jockey_name AS name, COUNT(*) AS runs,
               ROUND(100.0*SUM(CASE WHEN r.finish_position<=3 THEN 1 ELSE 0 END)/COUNT(*),1) AS top3,
               SUM(CASE WHEN r.finish_position=1 THEN 1 ELSE 0 END) AS wins
        FROM results r JOIN races ra ON r.race_id=ra.race_id
        JOIN jockeys j ON r.jockey_id=j.jockey_id
        WHERE ra.venue=? AND ra.surface=? AND ra.distance=?
          AND r.finish_position>0 AND ra.race_date>=?
        GROUP BY j.jockey_name HAVING runs>=?
        ORDER BY top3 DESC, runs DESC LIMIT ?
    """, (venue, surface, distance, f"{year_min}-01-01", min_runs, top)).fetchall()
    return [dict(r) for r in rows]


def get_entry_with_jockey(conn, race_id):
    """出走馬と騎手のセット(+pop/odds)。AI synthesis 用。"""
    rows = conn.execute("""
        SELECT h.horse_name AS name, h.horse_id AS hid,
               h.sire AS sire, h.damsire AS damsire,
               r.horse_number AS num, r.popularity AS pop, r.odds AS odds,
               j.jockey_name AS jockey
        FROM results r
        LEFT JOIN horses h ON r.horse_id=h.horse_id
        LEFT JOIN jockeys j ON r.jockey_id=j.jockey_id
        WHERE r.race_id=?
        ORDER BY r.horse_number
    """, (race_id,)).fetchall()
    return [dict(r) for r in rows if r['name']]


def _compact_reason(reason):
    """spotlight の reason を短縮形に圧縮。
    例: '父リアルスティールは当コース複勝65.0%' → '父65%'
        '鞍上C.ルメールは当コース複勝22.4%' → '鞍上22%'
        '市場1人気' → '1人気'
    """
    import re
    if not reason:
        return ''
    # XX人気
    m = re.match(r'市場(\d+)人気', reason)
    if m:
        return f"{m.group(1)}人気"
    # 父/母父/鞍上 ... 複勝XX.X% → 父XX%
    m = re.match(r'^(父|母父|鞍上)(.+?)は当コース複勝(\d+\.?\d*)%', reason)
    if m:
        return f"{m.group(1)}{round(float(m.group(3)))}%"
    return reason[:14]


def ai_spotlight_horses(conn, race, sires, damsires, entries, year_min=2020,
                       sire_threshold=50.0, damsire_threshold=50.0,
                       jockey_threshold=20.0, max_horses=3):
    """AIならではの synthesis: 出走馬を複数軸でスコア化、上位を返す。

    各軸 (該当時+1):
      - 父×コース 複勝率 >= sire_threshold (default 50%, 過去6年)
      - 母父×コース 複勝率 >= damsire_threshold (default 50%, 過去6年)
      - 騎手×コース 複勝率 >= jockey_threshold (default 20%, 過去6年)
      - 市場人気1-3 (堅軸 = +1)

    複数軸が立つ馬は「コース×血統×騎手の全条件 OK」のような
    AIならではの synthesis。1軸以下は surface しない(generic と区別)。

    Returns: [{'name', 'num', 'score', 'reasons':[str], 'jockey'}, ...]
    """
    if not entries:
        return []
    # entries に jockey が無ければ get_entry_with_jockey で再取得
    if entries and 'jockey' not in (entries[0] or {}):
        try:
            entries_full = get_entry_with_jockey(conn, race['race_id'])
            # 元の entries の sire/damsire を保存しつつ jockey を merge
            byname = {e.get('name'): e for e in entries}
            for ef in entries_full:
                if ef['name'] in byname:
                    byname[ef['name']]['jockey'] = ef.get('jockey', '')
                    byname[ef['name']]['pop'] = byname[ef['name']].get('pop') or ef.get('pop')
            # jockey が取れない entries も merge — sire のみの entry でも spotlight 評価する
            for ef in entries_full:
                if ef['name'] and ef['name'] not in byname:
                    byname[ef['name']] = ef
            entries = list(byname.values())
        except Exception:
            pass

    venue = race['venue']
    surface = race['surface']
    distance = race['distance']

    sire_top = {s['name']: s['top3'] for s in (sires or []) if s.get('top3', 0) >= sire_threshold}
    damsire_top = {d['name']: d['top3'] for d in (damsires or []) if d.get('top3', 0) >= damsire_threshold}
    jockey_top_list = get_jockey_course_top(
        conn, venue, surface, distance, min_runs=8, top=30, year_min=year_min
    )
    jockey_top = {j['name']: j['top3'] for j in jockey_top_list if j.get('top3', 0) >= jockey_threshold}

    spotlights = []
    for e in entries:
        score = 0
        reasons = []
        if e.get('sire') in sire_top:
            score += 1
            reasons.append(f"父{e['sire']}は当コース複勝{sire_top[e['sire']]}%")
        if e.get('damsire') in damsire_top:
            score += 1
            reasons.append(f"母父{e['damsire']}は当コース複勝{damsire_top[e['damsire']]}%")
        if e.get('jockey') in jockey_top:
            score += 1
            reasons.append(f"鞍上{e['jockey']}は当コース複勝{jockey_top[e['jockey']]}%")
        pop = e.get('pop') or 0
        if isinstance(pop, (int, float)) and 1 <= pop <= 3:
            score += 1
            reasons.append(f"市場{int(pop)}人気")
        if score >= 2:  # 2軸以上立っているもののみ surface
            spotlights.append({
                'name': e.get('name', '?'),
                'num': e.get('num', 0),
                'jockey': e.get('jockey', ''),
                'score': score,
                'reasons': reasons,
            })
    spotlights.sort(key=lambda x: (-x['score'], x['num']))
    return spotlights[:max_horses]


# ───────────────────────────────────────────
# セクション関数群（短いテキスト断片を返す）
# ───────────────────────────────────────────

def sec_frame(stats, depth='brief'):
    fs = stats.get('frame_stats') or []
    if not fs:
        return None
    valid = [f for f in fs if f.get('runs', 0) > 0]
    if not valid:
        return None
    best = max(valid, key=lambda x: x.get('top3_rate', 0))
    if depth == 'brief':
        return f"✅ {best['frame']}枠が複勝率{best['top3_rate']}%でトップ"
    worst = min(valid, key=lambda x: x.get('top3_rate', 0))
    return (
        f"✅ {best['frame']}枠が複勝率{best['top3_rate']}% ⬆️\n"
        f"⚠️ {worst['frame']}枠が複勝率{worst['top3_rate']}% ⬇️"
    )


def sec_pace(stats):
    rs = stats.get('running_style_stats') or []
    if not rs:
        return None
    best = max(rs, key=lambda x: x.get('win_rate', 0))
    style = best.get('style', '')
    return f"✅ {style} が勝率{best['win_rate']}% / 複勝{best['top3_rate']}%"


def sec_last3f(stats, depth='brief'):
    l3f = stats.get('last3f_stats') or []
    if not l3f:
        return None
    fastest = l3f[0]
    if depth == 'brief':
        return f"⚡ {fastest['label']} → 複勝率{fastest['top3_rate']}%"
    lines = []
    for lf in l3f[:3]:
        lines.append(f"  {lf['label']}: 勝率{lf['win_rate']}% 複勝{lf['top3_rate']}%")
    return "\n".join(lines)


def sec_jockey(stats, entry_jockeys=None, top_n=3, filter_fn=None):
    js = stats.get('jockey_stats') or []
    if filter_fn and entry_jockeys:
        js = filter_fn(js, entry_jockeys)
    if not js:
        return None
    lines = []
    for j in js[:top_n]:
        lines.append(f"  {j['jockey']} 複勝率{j['top3_rate']}%({j['runs']}騎乗)")
    return "\n".join(lines)


def sec_popularity(stats):
    ps = stats.get('popularity_stats') or []
    if not ps:
        return None
    lines = []
    for p in ps:
        rec = p.get('recovery', 0)
        lines.append(f"  {p['label']}: 複勝{p['top3_rate']}% / 回収{rec}%")
    return "\n".join(lines)


def sec_sire_lines(sires, label_prefix=''):
    if not sires:
        return None
    lines = []
    for s in sires:
        lines.append(f"  {label_prefix}{s['name']}: {s['top3']}%({s['runs']}頭)")
    return "\n".join(lines)


# ───────────────────────────────────────────
# テンプレート: 月-金 × 朝/昼/夜
# ───────────────────────────────────────────

def _course_scope_label(race, stats):
    """データ範囲を明示するラベル(誤解防止)"""
    n = stats.get('total_races') or '?'
    return f"{race['venue']}{race['surface']}{race['distance']}m / 全レース過去6年({n}R)"


# ───────────────────────────────────────────
# v8: 中身のある投稿のための共通ヘルパー
# 全 slot で「注目馬1頭+数値根拠+行動指針」の3要素を保証する
# ───────────────────────────────────────────

def get_ai_spotlight_top(conn, race, sires, damsires, entries, max_horses=3):
    """8軸スコア(血統/末脚/斤量/状態/同コース/馬齢/重賞/騎手) で上位を返す。

    既存の ai_spotlight_horses (4軸) を拡張した完全版。各馬を
    多角的に評価し、TOP N を返す。
    """
    if not entries:
        return []
    K_SIRES = {'ルーラーシップ', 'キングカメハメハ', 'キズナ', 'ドゥラメンテ',
               'レイデオロ', 'エピファネイア', 'ロードカナロア'}
    K_DAMSIRES = K_SIRES | {'ハービンジャー', 'ハービンジャーHarbinger(英)',
                            'フジキセキ', 'スクリーンヒーロー'}
    TOP_JOCKEYS = {'ルメール', 'C.ルメール', '武豊', '岩田康', '岩田康誠',
                   '川田', '戸崎', '横山典', '横山武', '池添', '浜中', '丹内'}

    race_id = race.get('race_id', '')
    venue = race.get('venue', '')
    surface = race.get('surface', '')
    distance = race.get('distance', 0) or 0

    # 出走馬に jockey 情報が無ければ補完
    if entries and 'jockey' not in (entries[0] or {}):
        try:
            entries_full = get_entry_with_jockey(conn, race_id)
            byname = {e.get('name'): e for e in entries}
            for ef in entries_full:
                if ef.get('name') in byname:
                    byname[ef['name']]['jockey'] = ef.get('jockey', '')
                    byname[ef['name']]['pop'] = byname[ef['name']].get('pop') or ef.get('pop')
                    byname[ef['name']]['num'] = byname[ef['name']].get('num') or ef.get('num')
            for ef in entries_full:
                if ef.get('name') and ef['name'] not in byname:
                    byname[ef['name']] = ef
            entries = list(byname.values())
        except Exception:
            pass

    scored = []
    for e in entries:
        score = 0
        reasons = []
        name = e.get('name', '?')
        sire = e.get('sire', '') or ''
        damsire = e.get('damsire', '') or ''
        impost = e.get('impost')
        try:
            impost = float(impost) if impost else 0.0
        except (TypeError, ValueError):
            impost = 0.0
        age = e.get('age')
        try:
            age = int(age) if age else 0
        except (TypeError, ValueError):
            age = 0
        jockey = e.get('jockey', '') or ''
        horse_id = e.get('hid') or e.get('horse_id', '')
        pop = e.get('pop') or 0
        try:
            pop = int(pop) if pop else 0
        except (TypeError, ValueError):
            pop = 0

        # ① 血統 (W条件は +5、片方K系は +3 or +2)
        s_score = 0
        if sire in K_SIRES:
            s_score += 3
        if damsire in K_DAMSIRES:
            s_score += 2
        if s_score >= 5:
            score += 5
            reasons.append('W血統')
        elif s_score > 0:
            score += s_score
            if sire in K_SIRES:
                reasons.append(f'父{sire}')
            else:
                reasons.append(f'母父{damsire}')

        # ② 末脚 (直近5走の上り3F最速回数)
        fastest = 0
        if horse_id and conn:
            try:
                pasts = conn.execute("""
                    SELECT r.last_3f, r.race_id
                    FROM results r JOIN races ra ON r.race_id=ra.race_id
                    WHERE r.horse_id=? AND r.last_3f>0 AND r.finish_position>0
                      AND ra.race_date<?
                    ORDER BY ra.race_date DESC LIMIT 5
                """, (horse_id, str(today_d_default()))).fetchall()
                for p in pasts:
                    min_l3 = conn.execute(
                        "SELECT MIN(last_3f) m FROM results WHERE race_id=? AND last_3f>0",
                        (p['race_id'],)
                    ).fetchone()
                    if min_l3 and min_l3['m'] and abs(p['last_3f'] - min_l3['m']) < 0.05:
                        fastest += 1
            except Exception:
                pass
        if fastest >= 2:
            score += 2; reasons.append(f'上り最速{fastest}回')
        elif fastest == 1:
            score += 1; reasons.append('上り最速1回')

        # ③ 斤量
        if impost > 0:
            if impost <= 54:
                score += 2; reasons.append(f'軽斤量{impost}')
            elif impost <= 56:
                score += 1
            elif impost >= 58:
                score -= 1; reasons.append(f'重斤量{impost}')

        # ④ 直近3走の状態
        f3 = []
        if horse_id and conn:
            try:
                pasts3 = conn.execute("""
                    SELECT r.finish_position
                    FROM results r JOIN races ra ON r.race_id=ra.race_id
                    WHERE r.horse_id=? AND r.finish_position>0 AND ra.race_date<?
                    ORDER BY ra.race_date DESC LIMIT 3
                """, (horse_id, str(today_d_default()))).fetchall()
                f3 = list(reversed([p['finish_position'] for p in pasts3]))
            except Exception:
                pass
        if len(f3) >= 3:
            if f3[2] < f3[1] < f3[0]:  # 急上昇
                score += 2; reasons.append(f'急上昇({f3[0]}→{f3[1]}→{f3[2]}着)')
            elif all(x <= 5 for x in f3):  # 上位安定
                score += 2; reasons.append(f'上位安定({f3[0]}→{f3[1]}→{f3[2]}着)')
            elif sum(1 for x in f3 if x >= 10) >= 2:  # 低調
                score -= 2; reasons.append(f'低調')
            elif f3[2] >= 10:  # 前走凡走
                score -= 1; reasons.append(f'前走{f3[2]}着で凡走')

        # ⑤ 同コース実績
        if horse_id and conn and venue and surface and distance:
            try:
                sc = conn.execute("""
                    SELECT COUNT(*) t, SUM(CASE WHEN finish_position<=3 THEN 1 ELSE 0 END) t3
                    FROM results r JOIN races ra ON r.race_id=ra.race_id
                    WHERE r.horse_id=? AND ra.venue=? AND ra.surface=? AND ra.distance=?
                      AND r.finish_position>0 AND ra.race_date<?
                """, (horse_id, venue, surface, distance, str(today_d_default()))).fetchone()
                if sc and sc['t3'] and sc['t3'] >= 1:
                    score += 2; reasons.append('同コース複勝経験')
            except Exception:
                pass

        # ⑥ 馬齢 (4-5歳ピーク)
        if 4 <= age <= 5:
            score += 1
        elif age >= 8:
            score -= 1; reasons.append(f'{age}歳高齢')

        # ⑦ 重賞経験
        if horse_id and conn:
            try:
                gr = conn.execute("""
                    SELECT SUM(CASE WHEN finish_position<=3 THEN 1 ELSE 0 END) t3
                    FROM results r JOIN races ra ON r.race_id=ra.race_id
                    WHERE r.horse_id=? AND ra.grade IN ('G1','G2','G3')
                      AND r.finish_position>0 AND ra.race_date<?
                """, (horse_id, str(today_d_default()))).fetchone()
                if gr and gr['t3']:
                    if gr['t3'] >= 2:
                        score += 2; reasons.append(f'重賞複勝{gr["t3"]}回')
                    elif gr['t3'] >= 1:
                        score += 1; reasons.append('重賞経験')
            except Exception:
                pass

        # ⑧ 騎手
        if jockey in TOP_JOCKEYS:
            score += 1; reasons.append(f'鞍上{jockey}')

        scored.append({
            'name': name,
            'num': e.get('num') or e.get('horse_number', 0),
            'score': score,
            'reasons': reasons,
            'sire': sire,
            'damsire': damsire,
            'jockey': jockey,
            'impost': impost,
            'pop': pop,
        })

    scored.sort(key=lambda x: -x['score'])
    return scored[:max_horses]


def today_d_default():
    """racing-day cutoff: 今日の日付を YYYY-MM-DD で返す"""
    return now_jst().date().isoformat()


def get_undervalued_horses(entries, max_horses=2):
    """市場が過小評価している可能性のある馬を抽出。
    人気外 (7番人気以下) で、血統/騎手等が強い馬。
    実 AI 予測値(pred_win)があれば EV>=1.5 で抽出するが、無ければ
    人気外 + 血統条件マッチで代替。
    """
    if not entries:
        return []
    K_SIRES = {'ルーラーシップ', 'キングカメハメハ', 'キズナ', 'ドゥラメンテ',
               'レイデオロ', 'エピファネイア', 'ロードカナロア'}
    candidates = []
    for e in entries:
        pop = e.get('pop') or 0
        try:
            pop = int(pop) if pop else 0
        except (TypeError, ValueError):
            pop = 0
        sire = e.get('sire', '') or ''
        # 人気外 (7-12) で血統 OK
        if 7 <= pop <= 12 and sire in K_SIRES:
            candidates.append({
                'name': e.get('name', '?'),
                'num': e.get('num') or e.get('horse_number', 0),
                'pop': pop,
                'sire': sire,
                'damsire': e.get('damsire', ''),
                'reason': f'{pop}人気想定 + 父{sire}(K系・コース複勝率高)',
            })
    return candidates[:max_horses]


def get_dangerous_favorites(entries, conn, race, max_horses=2):
    """危険な人気馬を抽出。1-3人気だが評価が低い馬。
    8軸スコアで人気と乖離が大きい馬。
    """
    if not entries or not conn:
        return []
    # 全頭のスコア計算
    all_scored = get_ai_spotlight_top(conn, race, [], [], entries, max_horses=len(entries))
    dangerous = []
    for s in all_scored:
        pop = s.get('pop') or 0
        score = s.get('score', 0)
        # 1-3人気だが score 低い (≤2) = 過大評価
        if 1 <= pop <= 3 and score <= 2:
            dangerous.append({
                'name': s['name'],
                'num': s.get('num', 0),
                'pop': pop,
                'score': score,
                'reason': s.get('reasons', []),
            })
    return dangerous[:max_horses]


def short_race_label(race):
    """ヘッダー用の短いレース名。例: '🎯 ヴィクトリアマイル(G1) | 東京芝1600m'"""
    grade = race.get('grade') or ''
    grade_part = f"({grade})" if grade else ''
    return f"{race.get('race_name','')}{grade_part} | {race.get('venue','')}{race.get('surface','')}{race.get('distance','')}m"


def build_morning_tweet(race, stats, sires, damsires, entries, today_d, hashtags_fn, dow=None):
    """朝テンプレ: 曜日別 angle で同レースでも違う content を出す。

    同じレース(例:ヴィクトリアマイル)を月-金で feature しても、各日違う
    切り口になるよう dow で section を切り替える:

      月: 枠順 + 脚質 + 上がり3F (基本傾向)
      火: 種牡馬×コース実績 + 出走馬クロス
      水: 母父×コース実績 + 出走馬クロス
      木: 人気別成績(妙味のある人気帯)
      金: 当日のまとめ(枠+末脚+父+予告) ※従来通り
    """
    if not race or not stats:
        return None
    if dow is None:
        dow = today_d.weekday() if today_d else 0
    day_phrase = race_day_phrase(race, today_d, with_paren=True)
    grade_label = f"({race['grade']})" if race.get('grade') else ""

    parts = [
        f"📊 {day_phrase}の{race.get('race_name','')}{grade_label}",
        _course_scope_label(race, stats),
        "",
    ]

    if dow == 0:  # 月曜朝: 基本傾向 (枠/脚質/上がり)
        f = sec_frame(stats, depth='brief')
        if f: parts.append(f)
        p = sec_pace(stats)
        if p: parts.append(p)
        l3 = sec_last3f(stats, depth='brief')
        if l3: parts.append(l3)

    elif dow == 1:  # 火曜朝: 種牡馬×コース(出走馬クロス) — 該当馬必須
        cs = cross_reference_sires(sires or [], entries or [], key='sire', limit=2)
        if cs:
            parts.append("【出走馬の父×コース(過去6年)】")
            for s in cs[:2]:
                horses = '・'.join(s['entries'][:1])
                parts.append(f" 🧬{s['name']}({horses}):複勝{s['top3']}%")
        else:
            # 該当出走馬がいない場合は section をスキップ(汎用 sire は出さない)
            parts.append("【今週のコース傾向】")
            f = sec_frame(stats, depth='brief')
            if f: parts.append(f)
            l3 = sec_last3f(stats, depth='brief')
            if l3: parts.append(l3)

    elif dow == 2:  # 水曜朝: 母父×コース(出走馬クロス) — 該当馬必須
        cd = cross_reference_sires(damsires or [], entries or [], key='damsire', limit=2)
        if cd:
            parts.append("【出走馬の母父×コース実績(過去6年)】")
            for d in cd[:2]:
                horses = '・'.join(d['entries'][:2])
                parts.append(f" 🧬{d['name']}({horses}):複勝{d['top3']}%")
        else:
            # 母父データなしならコース脚質傾向にフォールバック
            parts.append("【コース脚質傾向】")
            p = sec_pace(stats)
            if p: parts.append(p)
            l3 = sec_last3f(stats, depth='brief')
            if l3: parts.append(l3)

    elif dow == 3:  # 木曜朝: 人気別成績 — top3に圧縮、形式短く
        ps = stats.get('popularity_stats') or []
        parts.append("【人気別 成績(過去6年)】")
        if ps:
            for p in ps[:3]:
                rec = p.get('recovery', 0)
                mark = '🔥' if rec >= 80 else ''
                parts.append(f" {p['label']}:複勝{p['top3_rate']}%/回収{rec}%{mark}")
        else:
            parts.append("  人気別データ集計中")

    else:  # 金曜朝: 当日まとめ (圧縮版)
        f = sec_frame(stats, depth='brief')
        if f: parts.append(f)
        l3 = sec_last3f(stats, depth='brief')
        if l3: parts.append(l3)
        if entries:
            cs = cross_reference_sires(sires or [], entries, key='sire', limit=1)
            if cs:
                s = cs[0]
                names = '・'.join(s['entries'][:1])
                parts.append(f"🧬{s['name']}({names}):複勝{s['top3']}%")

    parts.append("")
    pa = predict_announce_phrase(race, today_d)
    if pa:
        parts.append(pa)
    parts.append("")
    parts.append(hashtags_fn(race))
    return "\n".join(parts)


def build_weekday_tweet(race, stats, sires, damsires, entries, today_d, hashtags_fn, dow):
    """昼テンプレ: 曜日別深掘りテーマ + 出走馬とのクロス参照"""
    if not race or not stats:
        return None
    day_phrase = race_day_phrase(race, today_d, with_paren=True)
    grade_label = f"({race['grade']})" if race.get('grade') else ""
    head = f"🔍 {day_phrase}の{race.get('race_name','')}{grade_label}\n{_course_scope_label(race, stats)}"

    if dow == 0:  # 月曜昼: 枠順詳細(TOP2 + WORST 1)
        body = ["", "【枠順別 複勝率(過去6年)】"]
        fs = stats.get('frame_stats') or []
        valid = [f for f in fs if f.get('runs', 0) > 0]
        if valid:
            sorted_fs = sorted(valid, key=lambda x: x.get('top3_rate', 0), reverse=True)
            for f in sorted_fs[:2]:
                body.append(f" ⬆️{f['frame']}枠:{f['top3_rate']}%({f['runs']}走)")
            worst = sorted_fs[-1]
            body.append(f" ⬇️{worst['frame']}枠:{worst['top3_rate']}%({worst['runs']}走)")
        body.append("")
        body.append("→ 枠は当日抽選/該当馬は朝に")

    elif dow == 1:  # 火曜昼: 出走馬とマッチする母父TOP3
        body = ["", "【出走馬の母父×コース実績】"]
        cd = cross_reference_sires(damsires or [], entries or [], key='damsire', limit=3)
        if cd:
            for d in cd:
                names = '・'.join(d['entries'][:2])
                body.append(f"  {d['name']}({names}): {d['top3']}% / {d['runs']}走中{int(d['runs']*d['top3']/100)}回複勝")
        else:
            body.append("  該当する母父データなし")
            body.append("  (出走馬の血統データ未収集 or 該当統計不足)")
        body.append("")
        body.append("→ 母父は予想の盲点")

    elif dow == 2:  # 水曜昼: 人気別成績 (TOP3 + 妙味)
        ps = stats.get('popularity_stats') or []
        body = ["", "【人気別 成績(過去6年)】"]
        for p in (ps or [])[:3]:
            rec = p.get('recovery', 0)
            mark = '🔥' if rec >= 80 else ''
            body.append(f" {p['label']}:複勝{p['top3_rate']}%/回収{rec}%{mark}")
        body.append("")
        if any(p.get('recovery', 0) >= 80 for p in ps):
            body.append("→ 妙味の人気帯あり")
        else:
            body.append("→ 上位人気中心")

    elif dow == 3:  # 木曜昼: 末脚詳細
        body = ["", "【上がり3F バケット別】(コース全レース)"]
        l3 = sec_last3f(stats, depth='full')
        if l3:
            body.append(l3)
        body.append("")
        body.append("→ 末脚の切れ味が勝敗を分ける")

    elif dow == 4:  # 金曜昼: 土曜重賞詳細(出走馬の父TOP1のみ)
        body = []
        cs = cross_reference_sires(sires or [], entries or [], key='sire', limit=1)
        if cs:
            body.append("")
            body.append("【出走馬の父×コース(過去6年)】")
            for s in cs[:1]:
                names = '・'.join(s['entries'][:1])
                body.append(f" 🧬{s['name']}({names}):複勝{s['top3']}%")
        else:
            body.append("")
            f = sec_frame(stats, depth='brief')
            if f: body.append(f)
            l3 = sec_last3f(stats, depth='brief')
            if l3: body.append(l3)
        body.append("")
        pa = predict_announce_phrase(race, today_d)
        if pa: body.append(pa)

    else:
        body = []

    parts = [head] + body + ["", hashtags_fn(race)]
    return "\n".join(parts)


def build_evening_tweet(race, stats, sires, damsires, entries, workouts, today_d, hashtags_fn, dow,
                        conn=None):
    """夜テンプレ: 出走馬とのクロス参照を含む。

    conn: AI spotlight 用に DB 接続を渡す(任意)。
    """
    if not race or not stats:
        return None
    day_phrase = race_day_phrase(race, today_d, with_paren=True)
    grade_label = f"({race['grade']})" if race.get('grade') else ""
    head = f"🌙 {day_phrase}の{race.get('race_name','')}{grade_label}\n{_course_scope_label(race, stats)}"

    if dow == 0:  # 月曜夜: コース全体の傾向
        body = ["", "【コース傾向(過去6年)】"]
        ff = sec_frame(stats, depth='full')
        if ff:
            body.append(ff)
        p = sec_pace(stats)
        if p:
            body.append(p)

    elif dow == 1:  # 火曜夜: AI注目馬 synthesis (重複する単独 sire セクションは省略)
        body = []
        spots = []
        if conn:
            spots = ai_spotlight_horses(conn, race, sires, damsires, entries,
                                         sire_threshold=50.0, damsire_threshold=50.0,
                                         jockey_threshold=20.0, max_horses=3)
        if spots:
            body.append("")
            body.append("【🤖 AI注目馬(過去6年・複数軸)】")
            for sp in spots:
                body.append(f" ⭐{sp['num']}番 {sp['name']}({sp['score']}軸)")
                # 理由を全て1行に圧縮(短縮形)
                if sp['reasons']:
                    short = '・'.join([_compact_reason(r) for r in sp['reasons'][:3]])
                    body.append(f"  ↳{short}")
        else:
            # spotlight が出ない場合のみ単独 sire を表示
            cs = cross_reference_sires(sires or [], entries or [], key='sire', limit=2)
            if cs:
                body.append("")
                body.append("【出走馬の父×コース(過去6年)】")
                for s in cs:
                    names = '・'.join(s['entries'][:2])
                    body.append(f" 🧬{s['name']}({names}):複勝{s['top3']}%")
            else:
                body.append("")
                body.append("【当コース 過去6年の傾向】")
                f = sec_frame(stats, depth='brief')
                if f: body.append(f)
                p = sec_pace(stats)
                if p: body.append(p)

    elif dow == 2:  # 水曜夜: 穴馬条件
        body = ["", "【穴馬の条件】(コース全レース)"]
        ps = stats.get('popularity_stats') or []
        anaba = [p for p in ps if p['label'] in ('7-9人気', '10人気以下')]
        for p in anaba:
            body.append(f"  {p['label']}: 複勝{p['top3_rate']}% / 回収{p.get('recovery', 0)}%")
        body.append("")
        if any(p.get('recovery', 0) >= 80 for p in anaba):
            body.append("→ 穴馬での妙味あり🔥")
        else:
            body.append("→ 堅実派向きのコース")

    elif dow == 3:  # 木曜夜: 全データまとめ + 追い切り
        body = []
        # 木曜夜: AI注目馬 + 追い切り (重複する 父注目 は省略)
        spots = []
        if conn:
            spots = ai_spotlight_horses(conn, race, sires, damsires, entries,
                                         sire_threshold=50.0, damsire_threshold=50.0,
                                         jockey_threshold=20.0, max_horses=2)
        if spots:
            body.append("")
            body.append("【🤖 AI注目馬(過去6年・複数軸)】")
            for sp in spots:
                body.append(f" ⭐{sp['num']}番 {sp['name']}({sp['score']}軸)")
                if sp['reasons']:
                    short = '・'.join([_compact_reason(r) for r in sp['reasons'][:3]])
                    body.append(f"  ↳{short}")
        ws = workout_section(workouts, max_show=2)
        if ws:
            body.append("")
            body.append(ws)
        # fallback: spots も追い切りも無ければコース傾向
        if not spots and not ws:
            body.append("")
            body.append(f"【コース傾向(過去6年)】")
            f = sec_frame(stats, depth='brief')
            if f: body.append(f)
            l3 = sec_last3f(stats, depth='brief')
            if l3: body.append(l3)
        body.append("")
        pa = predict_announce_phrase(race, today_d)
        if pa:
            body.append(pa)

    elif dow == 4:  # 金曜夜: 日曜G1詳細(AI注目馬 + 追い切り、重複セクションは省略)
        body = []
        spots = []
        if conn:
            spots = ai_spotlight_horses(conn, race, sires, damsires, entries,
                                         sire_threshold=50.0, damsire_threshold=50.0,
                                         jockey_threshold=20.0, max_horses=3)
        if spots:
            body.append("")
            body.append("【🤖 AI注目馬(過去6年・複数軸)】")
            for sp in spots:
                body.append(f" ⭐{sp['num']}番 {sp['name']}({sp['score']}軸)")
                if sp['reasons']:
                    short = '・'.join([_compact_reason(r) for r in sp['reasons'][:3]])
                    body.append(f"  ↳{short}")
        ws = workout_section(workouts, max_show=2)
        if ws:
            body.append("")
            body.append(ws)
        # fallback: spotlight も追い切りも無ければ 父クロス → コース傾向
        if not spots and not ws:
            cs = cross_reference_sires(sires or [], entries or [], key='sire', limit=2)
            if cs:
                body.append("")
                body.append("【出走馬の父×コース実績】")
                for s in cs:
                    names = '・'.join(s['entries'][:2])
                    body.append(f" 🧬{s['name']}({names}):複勝{s['top3']}%")
            else:
                body.append("")
                body.append("【勝ちパターン(過去6年)】")
                f = sec_frame(stats, depth='brief')
                if f: body.append(f)
                l3 = sec_last3f(stats, depth='brief')
                if l3: body.append(l3)
        body.append("")
        pa = predict_announce_phrase(race, today_d)
        if pa:
            body.append(pa)

    else:
        body = []

    parts = [head] + body + ["", hashtags_fn(race)]
    return "\n".join(parts)


# ───────────────────────────────────────────
# 統合エントリポイント
# ───────────────────────────────────────────

def build_post_for_slot(slot, today_d, conn, get_todays_race_fn, get_course_stats_fn,
                        get_entry_jockeys_fn, hashtags_fn, jockey_filter_fn,
                        return_race=False):
    """slot='morning'|'weekday'|'evening' のツイートを生成

    Args:
        return_race: True を返すと(tweet, race_id)タプルで返す。
                     後方互換のため省略時は tweet 文字列のみ返す。
    """
    dow = today_d.weekday()
    # 平日のみ稼働(土日は別の予測/結果系cmdが受け持つ)
    if dow >= 5:
        print(f"⚠️ {slot}: 土日({['月','火','水','木','金','土','日'][dow]}曜)は平日テンプレ対象外 → スキップ")
        return (None, None) if return_race else None
    slot_idx = {'morning': 0, 'weekday': 1, 'evening': 2}[slot]

    # 金曜は土曜重賞(昼) / 日曜G1(夜) で分岐
    if dow == 4 and slot == 'weekday':
        from post_x import get_weekend_graded_races
        all_races = get_weekend_graded_races(conn)
        sat, sun = split_weekend_races(all_races)
        race = sat[0] if sat else (all_races[0] if all_races else None)
    elif dow == 4 and slot == 'evening':
        from post_x import get_weekend_graded_races
        all_races = get_weekend_graded_races(conn)
        sat, sun = split_weekend_races(all_races)
        race = sun[0] if sun else (all_races[0] if all_races else None)
    else:
        race = get_todays_race_fn(conn, slot=slot_idx)

    if not race:
        return (None, None) if return_race else None

    # フォールバックで「先週末のレース」が返ってきたら投稿しない(混乱回避)
    rd = parse_race_date(race)
    if rd and rd < today_d:
        print(f"⚠️ {slot}: 取得したレース({race.get('race_name','')})の日付が過去({rd}) → 投稿スキップ")
        return (None, None) if return_race else None

    venue = race['venue']
    surface = race['surface']
    distance = race['distance']
    stats = get_course_stats_fn(venue, surface, distance)
    if not stats:
        return (None, race.get('race_id')) if return_race else None

    # 種牡馬・母父(top=10にしてからクロス参照)
    sires = []
    damsires = []
    try:
        sires = get_sire_top(conn, venue, surface, distance, min_runs=5, top=15)
        damsires = get_damsire_top(conn, venue, surface, distance, min_runs=5, top=15)
    except Exception:
        pass

    # 出走馬の血統(クロス参照用)
    entries = []
    try:
        entries = get_entry_pedigree(conn, race['race_id'])
    except Exception:
        pass

    # 追い切り評価
    workouts = []
    try:
        workouts = get_workouts_for_race(conn, race['race_id'])
    except Exception:
        pass

    # v8: dow × slot で 15 専用ビルダーに dispatch
    # 各 builder は「注目馬1頭+数値根拠+行動指針」の3要素を必ず含む
    tweet = _dispatch_v8(dow, slot, race, stats, entries, sires, damsires,
                         workouts, conn, today_d, hashtags_fn)
    # フォールバック: v8 builder が None なら旧テンプレ
    if not tweet:
        if slot == 'morning':
            tweet = build_morning_tweet(race, stats, sires, damsires, entries, today_d, hashtags_fn, dow=dow)
        elif slot == 'weekday':
            tweet = build_weekday_tweet(race, stats, sires, damsires, entries, today_d, hashtags_fn, dow)
        else:
            tweet = build_evening_tweet(race, stats, sires, damsires, entries, workouts,
                                        today_d, hashtags_fn, dow, conn=conn)

    if return_race:
        return tweet, race.get('race_id')
    return tweet


def _dispatch_v8(dow, slot, race, stats, entries, sires, damsires,
                 workouts, conn, today_d, hashtags_fn):
    """v8 投稿: dow(0-4) × slot('morning'|'weekday'|'evening') で15専用ビルダーへ"""
    # 月曜 (週始)
    if dow == 0:
        if slot == 'morning':
            try:
                from post_x import get_weekend_graded_races
                graded = get_weekend_graded_races(conn)
            except Exception:
                graded = [race]
            return build_mon_morning(graded, today_d, hashtags_fn)
        elif slot == 'weekday':
            return build_mon_weekday(race, stats, entries, sires, today_d, hashtags_fn)
        else:
            return build_mon_evening(race, conn, entries, sires, damsires, today_d, hashtags_fn)
    # 火曜
    elif dow == 1:
        if slot == 'morning':
            return build_tue_morning(race, sires, damsires, entries, today_d, hashtags_fn)
        elif slot == 'weekday':
            return build_tue_weekday(race, conn, entries, sires, damsires, today_d, hashtags_fn)
        else:
            return build_tue_evening(race, conn, entries, sires, damsires, today_d, hashtags_fn)
    # 水曜
    elif dow == 2:
        if slot == 'morning':
            return build_wed_morning(race, conn, entries, sires, damsires, today_d, hashtags_fn)
        elif slot == 'weekday':
            return build_wed_weekday(race, conn, today_d, hashtags_fn)
        else:
            return build_wed_evening(race, conn, entries, sires, damsires, today_d, hashtags_fn)
    # 木曜
    elif dow == 3:
        if slot == 'morning':
            return build_thu_morning(race, sires, damsires, entries, today_d, hashtags_fn)
        elif slot == 'weekday':
            return build_thu_weekday(race, conn, entries, sires, damsires, today_d, hashtags_fn)
        else:
            return build_thu_evening(race, conn, entries, sires, damsires, today_d, hashtags_fn)
    # 金曜
    elif dow == 4:
        if slot == 'morning':
            return build_fri_morning(race, conn, entries, sires, damsires, today_d, hashtags_fn)
        elif slot == 'weekday':
            return build_fri_weekday(race, conn, entries, sires, damsires, today_d, hashtags_fn)
        else:
            try:
                from post_x import get_weekend_graded_races
                graded = get_weekend_graded_races(conn)
            except Exception:
                graded = [race]
            return build_fri_evening(graded, today_d, hashtags_fn)
    return None


# ═══════════════════════════════════════════════════════════════
# v8: 15 slot 専用ビルダー (注目馬+数値根拠+行動指針 を必ず含む)
# ═══════════════════════════════════════════════════════════════

def _format_spotlight_line(sp, with_reasons=True, max_reasons=1):
    """spotlight 1頭を1行で表示。圧縮版:理由は1個のみ表示"""
    num = sp.get('num', 0)
    name = sp.get('name', '?')
    score = sp.get('score', 0)
    if with_reasons and sp.get('reasons'):
        rs = sp['reasons'][0] if sp['reasons'] else ''
        # 理由の括弧内文字列を 12 文字以内に
        if len(rs) > 14:
            rs = rs[:13] + '…'
        return f"{num}番 {name}({score}・{rs})"
    return f"{num}番 {name}(score{score})"


def _race_label_short(race):
    """短いラベル。例: 'ヴィクトリアマイル(G1) 東京芝1600m'"""
    grade = race.get('grade') or ''
    g = f"({grade})" if grade else ''
    return f"{race.get('race_name','')}{g} {race.get('venue','')}{race.get('surface','')}{race.get('distance','')}m"


def get_note_article_url(race):
    """指定レースのnote記事URLを取得。
    優先順位:
      1. 環境変数 NOTE_ARTICLES (JSON: {"race_id": "url"})
      2. 環境変数 NOTE_<RACE_ID>
      3. ローカル設定 docs/data/note_articles.json
    どれも無ければ None を返す(投稿には URL 行を入れない)。
    """
    import os, json
    race_id = race.get('race_id') if isinstance(race, dict) else None
    if not race_id:
        return None
    # 1. JSON 環境変数
    raw = os.getenv('NOTE_ARTICLES', '')
    if raw:
        try:
            m = json.loads(raw)
            if race_id in m and m[race_id]:
                return m[race_id]
        except json.JSONDecodeError:
            pass
    # 2. 個別環境変数
    indiv = os.getenv(f'NOTE_{race_id}', '')
    if indiv:
        return indiv
    # 3. ローカル設定ファイル
    try:
        cfg_path = os.path.join(os.path.dirname(__file__), 'docs', 'data', 'note_articles.json')
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                cfg = json.load(f)
            if race_id in cfg and cfg[race_id]:
                return cfg[race_id]
    except (IOError, json.JSONDecodeError):
        pass
    return None


def _phrase_when(race, today_d):
    """日付フレーズ。例: '5/18(日)', '今週末(日)'"""
    rd = parse_race_date(race)
    if not rd or not today_d:
        return ''
    delta = (rd - today_d).days
    wd = WEEKDAY_LABELS[rd.weekday()]
    if delta == 1: return f'明日({wd})'
    if delta == 2: return f'明後日({wd})'
    if 3 <= delta <= 6: return f'今週末({wd})'
    return f"{rd.month}/{rd.day}({wd})"


def _entry_for_sire(entries, sire_name):
    """出走馬の中で父が一致する馬を返す"""
    return [e for e in (entries or []) if e.get('sire') == sire_name]


# ─── 月曜 朝: 今週末ラインナップ + 最注目重賞 ───

def build_mon_morning(graded_races, today_d, hashtags_fn):
    """週始め:今週末の重賞を一覧で紹介、AIが最注目するレースを1つ提示"""
    if not graded_races:
        return None
    top = graded_races[0]  # G1 > G2 > G3 順

    parts = [f"📅 今週末の重賞ラインナップ\n"]
    # 最大3件 + 短縮表記
    for r in graded_races[:3]:
        grade = r.get('grade') or ''
        rd = parse_race_date(r)
        wd = WEEKDAY_LABELS[rd.weekday()] if rd else '?'
        parts.append(f"・{wd}曜{rd.day if rd else '?'}日 {r.get('race_name','')}({grade}) {r.get('venue','')}{r.get('distance','')}m")

    parts.append(f"\n🎯 AI最注目:{top.get('race_name','')}({top.get('grade','')})")
    parts.append(f"火-木で深層分析を毎日配信🔔")
    parts.append('')
    parts.append(hashtags_fn(top))
    return '\n'.join(parts)


# ─── 月曜 昼: レース#1 のコース傾向 + 該当馬 ───

def build_mon_weekday(race, stats, entries, sires, today_d, hashtags_fn):
    """月曜昼:該当レースの最重要コース傾向を1つに絞り、該当する出走馬を提示"""
    if not race or not stats:
        return None
    when = _phrase_when(race, today_d)
    label = _race_label_short(race)

    # 最強の傾向データを選ぶ:上がり3F最速 or 脚質
    parts = [f"🔍 {when} {label}\n"]
    parts.append("【コース傾向の核心(過去6年)】")

    # 上がり3F最速の威力
    last3_top = None
    try:
        l3 = stats.get('last3f_stats') or []
        if l3:
            # 上がり最速馬の複勝率
            top_l3 = max(l3, key=lambda x: x.get('top3_rate', 0))
            if top_l3.get('top3_rate', 0) >= 50:
                last3_top = top_l3
                parts.append(f"🚀 上がり最速馬の複勝率:{top_l3.get('top3_rate')}%")
    except Exception:
        pass

    # 脚質 best
    try:
        rs = stats.get('running_style_stats') or []
        if rs:
            best = max(rs, key=lambda x: x.get('top3_rate', 0))
            parts.append(f"💨 {best.get('style','')}が複勝率{best.get('top3_rate')}%")
    except Exception:
        pass

    # 該当する出走馬 (父コースTOPの産駒)
    parts.append("")
    if entries and sires:
        cs = cross_reference_sires(sires, entries, key='sire', limit=2)
        if cs:
            parts.append("【データに合致する出走馬】")
            for s in cs[:2]:
                names = '・'.join(s['entries'][:1])
                parts.append(f"🧬{s['name']}産駒({names}):複勝率{s['top3']}%")
            parts.append("\n→ 今夜AI注目馬TOP3を配信🔔")
        else:
            parts.append("→ 詳細は今夜のAI注目馬TOP3配信で🔔")

    parts.append('')
    parts.append(hashtags_fn(race))
    return '\n'.join(parts)


# ─── 月曜 夜: レース#1 AI注目馬TOP3 ───

def build_mon_evening(race, conn, entries, sires, damsires, today_d, hashtags_fn):
    """月曜夜:8軸スコアでTOP3を発表"""
    if not race:
        return None
    when = _phrase_when(race, today_d)
    label = _race_label_short(race)
    spots = get_ai_spotlight_top(conn, race, sires, damsires, entries, max_horses=3)

    parts = [f"⭐ {when} {label} AI注目馬TOP3\n"]
    parts.append("【8軸スコア:血統×末脚×斤量×状態×同コース×馬齢×重賞×騎手】")
    parts.append("")
    if spots:
        for i, sp in enumerate(spots, 1):
            mark = ['◎', '○', '▲'][i-1]
            parts.append(f"{mark} {_format_spotlight_line(sp, max_reasons=2)}")
    else:
        parts.append("(出走馬データ取得中、明日朝以降に再配信)")

    parts.append("\n📊 週中で枠順・追い切り反映、木曜夜に最終版発表")
    parts.append('')
    parts.append(hashtags_fn(race))
    return '\n'.join(parts)


# ─── 火曜 朝: レース#2 血統データ + 該当馬 ───

def build_tue_morning(race, sires, damsires, entries, today_d, hashtags_fn):
    """火曜朝:コース×父TOPと該当出走馬"""
    if not race:
        return None
    when = _phrase_when(race, today_d)
    label = _race_label_short(race)

    parts = [f"🧬 {when} {label} 父血統分析\n"]
    parts.append("【当コース複勝率TOP3父(過去6年)】")
    parts.append("")
    if sires:
        for s in (sires or [])[:3]:
            parts.append(f"・{s['name']}:{s['top3']}%({s['runs']}走)")
    parts.append("")

    if entries and sires:
        cs = cross_reference_sires(sires, entries, key='sire', limit=3)
        if cs:
            parts.append("【該当する出走馬】")
            for s in cs[:3]:
                names = '・'.join(s['entries'][:1])
                parts.append(f"🎯{names}(父{s['name']})")
            parts.append("\n→ 今日12:30に8軸スコアで本格評価")
        else:
            parts.append("該当出走馬なし → 12:30に別軸で評価")

    parts.append('')
    parts.append(hashtags_fn(race))
    return '\n'.join(parts)


# ─── 火曜 昼: レース#2 AI注目馬TOP3 ───

def build_tue_weekday(race, conn, entries, sires, damsires, today_d, hashtags_fn):
    """火曜昼:レース#2の AI注目馬TOP3"""
    if not race:
        return None
    when = _phrase_when(race, today_d)
    grade = race.get('grade') or ''
    g = f"({grade})" if grade else ''

    parts = [f"🎯 {when}{race.get('race_name','')}{g} AI注目馬TOP3"]
    parts.append("(8軸スコア:血統×末脚×コース×状態 等)")
    parts.append("")
    spots = get_ai_spotlight_top(conn, race, sires, damsires, entries, max_horses=3)
    if spots:
        for i, sp in enumerate(spots, 1):
            mark = ['◎', '○', '▲'][i-1]
            parts.append(f"{mark} {_format_spotlight_line(sp)}")
    else:
        parts.append("(出走馬データ取得中)")

    parts.append("\n💡 今夜:深層分析note記事を公開")
    parts.append('')
    parts.append(hashtags_fn(race))
    return '\n'.join(parts)


# ─── 火曜 夜: note 記事誘導 ───

def build_tue_evening(race, conn, entries, sires, damsires, today_d, hashtags_fn):
    """火曜夜:詳細分析 + AI 本命チラ見せ(noteURLあれば誘導)"""
    if not race:
        return None
    when = _phrase_when(race, today_d)
    label = _race_label_short(race)
    spots = get_ai_spotlight_top(conn, race, sires, damsires, entries, max_horses=2)
    note_url = get_note_article_url(race)

    if note_url:
        # URLあり → note誘導テンプレ
        parts = [f"📝 {when} {label} 深層分析note公開\n"]
        parts.append("【記事の中身】")
        parts.append("・過去6年データで判明した勝ち馬の型")
        parts.append("・コース×血統TOP10 + AI 8軸スコア")
        parts.append("")
        if spots:
            parts.append(f"◎本命候補:{_format_spotlight_line(spots[0])}")
            parts.append("")
        parts.append(f"▶ {note_url}")
    else:
        # URLなし → AI注目馬TOP2 を別フォーマットで提示
        parts = [f"📊 {when} {label} AI予想ハイライト\n"]
        if spots:
            parts.append("【現時点の AI 注目馬】")
            marks = ['◎', '○']
            for i, sp in enumerate(spots[:2]):
                parts.append(f"{marks[i]} {_format_spotlight_line(sp)}")
            parts.append("")
        parts.append("→ 木曜夜に最終予想、土曜朝に印付き完全予想配信🔔")
    parts.append('')
    parts.append(hashtags_fn(race))
    return '\n'.join(parts)


# ─── 水曜 朝: コース×騎手 cross (新規) ───
# 注: 枠順は金曜抽選なので水曜には出ない。代わりに「騎手×当該コース」を発表

def build_wed_morning(race, conn, entries, sires, damsires, today_d, hashtags_fn):
    """水曜朝:当該コースで実績のある騎手を抽出 + 出走馬の中で該当鞍上"""
    if not race:
        return None
    when = _phrase_when(race, today_d)
    label = _race_label_short(race)
    venue = race.get('venue', '')
    surface = race.get('surface', '')
    distance = race.get('distance', 0)

    parts = [f"🏇 {when} {label} 騎手コース適性\n"]

    # コース×騎手 TOP3 (過去6年, min_runs=10)
    jockey_top = []
    if conn:
        try:
            rows = conn.execute("""
                SELECT j.jockey_name AS name, COUNT(*) AS runs,
                       ROUND(100.0*SUM(CASE WHEN r.finish_position<=3 THEN 1 ELSE 0 END)/COUNT(*),1) AS top3
                FROM results r JOIN races ra ON r.race_id=ra.race_id
                JOIN jockeys j ON r.jockey_id=j.jockey_id
                WHERE ra.venue=? AND ra.surface=? AND ra.distance=?
                  AND r.finish_position>0 AND ra.race_date>='2020-01-01'
                GROUP BY j.jockey_name HAVING runs>=10
                ORDER BY top3 DESC, runs DESC LIMIT 5
            """, (venue, surface, distance)).fetchall()
            jockey_top = [dict(r) for r in rows]
        except Exception:
            pass

    if jockey_top:
        parts.append("【当コース複勝率TOP騎手(過去6年)】")
        for j in jockey_top[:3]:
            parts.append(f"・{j['name']}:{j['top3']}%({j['runs']}走)")

    # 出走馬の中で該当鞍上をクロス
    if entries:
        # entries に jockey が無ければ取得
        if 'jockey' not in (entries[0] or {}):
            try:
                ent_full = get_entry_with_jockey(conn, race.get('race_id', ''))
                entries = ent_full or entries
            except Exception:
                pass
        top_names = {j['name'] for j in jockey_top[:5]}
        matched = []
        for e in entries:
            jk = e.get('jockey', '') or ''
            if jk and jk in top_names:
                matched.append((e.get('num') or e.get('horse_number', 0),
                                e.get('name', '?'), jk))
        if matched:
            parts.append("")
            parts.append("【該当する出走馬】")
            for num, name, jk in matched[:3]:
                parts.append(f"🎯{num}番 {name}(鞍上{jk})")

    parts.append("\n→ 木曜に出走馬確定、金曜に枠順抽選")
    parts.append('')
    parts.append(hashtags_fn(race))
    return '\n'.join(parts)


# ─── 水曜 昼: 追い切り情報 ───

def build_wed_weekday(race, conn, today_d, hashtags_fn):
    """水曜昼:出走馬の追い切り評価から注目馬"""
    if not race:
        return None
    when = _phrase_when(race, today_d)
    label = _race_label_short(race)
    race_id = race.get('race_id', '')

    parts = [f"🏃 {when} {label} 追い切り評価\n"]

    workouts = []
    if conn and race_id:
        try:
            workouts = conn.execute("""
                SELECT w.horse_number, h.horse_name, w.evaluation_grade, w.evaluation_text
                FROM workouts w LEFT JOIN horses h ON w.horse_id=h.horse_id
                WHERE w.race_id=? ORDER BY w.horse_number
            """, (race_id,)).fetchall()
        except Exception:
            pass

    a_horses = [w for w in workouts if w['evaluation_grade'] == 'A']
    b_horses = [w for w in workouts if w['evaluation_grade'] == 'B']

    if a_horses:
        parts.append("【A評価の出走馬】")
        for w in a_horses[:4]:
            txt = w['evaluation_text'][:20] if w['evaluation_text'] else ''
            parts.append(f"🏅A {w['horse_number']}番 {w['horse_name']}({txt})")
    elif b_horses:
        parts.append("【B評価注目馬】")
        for w in b_horses[:3]:
            txt = w['evaluation_text'][:20] if w['evaluation_text'] else ''
            parts.append(f"◯B {w['horse_number']}番 {w['horse_name']}({txt})")
    else:
        parts.append("(追い切り評価データ収集中)")

    parts.append("\n→ A評価馬は調教師の自信表れ、複勝率10pt上振れ傾向")
    parts.append('')
    parts.append(hashtags_fn(race))
    return '\n'.join(parts)


# ─── 水曜 夜: 危険な人気馬警告 ───

def build_wed_evening(race, conn, entries, sires, damsires, today_d, hashtags_fn):
    """水曜夜:1-3人気だがAI評価低い"危険人気馬"を警告"""
    if not race:
        return None
    when = _phrase_when(race, today_d)
    label = _race_label_short(race)

    parts = [f"⚠️ {when} {label} 危険な人気馬警告\n"]
    dangerous = get_dangerous_favorites(entries, conn, race, max_horses=2)
    if dangerous:
        for d in dangerous:
            parts.append(f"🚨 {d['num']}番 {d['name']}({d['pop']}人気想定だが…)")
            parts.append(f"  → AI score {d['score']}と低評価")
            parts.append("")
        parts.append("【代わりに狙うべき馬】")
        spots = get_ai_spotlight_top(conn, race, sires, damsires, entries, max_horses=2)
        for sp in spots:
            num = sp.get('num', 0)
            name = sp.get('name', '?')
            pop = sp.get('pop', 0)
            parts.append(f"⭐{num}番 {name}(想定{pop}人気・score{sp.get('score',0)})")
    else:
        parts.append("人気と AI 評価は概ね一致、堅い決着の可能性")

    parts.append('')
    parts.append(hashtags_fn(race))
    return '\n'.join(parts)


# ─── 木曜 朝: 最終血統 cross + 出走確定 ───

def build_thu_morning(race, sires, damsires, entries, today_d, hashtags_fn):
    """木曜朝:出走確定 + 最終血統cross"""
    return build_tue_morning(race, sires, damsires, entries, today_d, hashtags_fn)  # 火曜朝と同等


# ─── 木曜 昼: 8軸最終ランキング TOP6 ───

def build_thu_weekday(race, conn, entries, sires, damsires, today_d, hashtags_fn):
    """木曜昼:8軸スコアで最終ランキング TOP4 (本命〜単穴)"""
    if not race:
        return None
    when = _phrase_when(race, today_d)
    grade = race.get('grade') or ''
    g = f"({grade})" if grade else ''
    parts = [f"🏆 {when}{race.get('race_name','')}{g} 最終8軸TOP4"]
    parts.append("")
    spots = get_ai_spotlight_top(conn, race, sires, damsires, entries, max_horses=4)
    if spots:
        marks = ['◎', '○', '▲', '△']
        for i, sp in enumerate(spots):
            mark = marks[i] if i < len(marks) else '・'
            parts.append(f"{mark} {_format_spotlight_line(sp)}")
    else:
        parts.append("(出走馬データ取得中)")

    parts.append("\n→ 金曜:オッズ妙味の過小評価馬を発表")
    parts.append('')
    parts.append(hashtags_fn(race))
    return '\n'.join(parts)


# ─── 木曜 夜: 最終注目馬 + note 完成版誘導 ───

def build_thu_evening(race, conn, entries, sires, damsires, today_d, hashtags_fn):
    """木曜夜:最終注目馬3頭(noteURLあれば誘導)"""
    if not race:
        return None
    when = _phrase_when(race, today_d)
    label = _race_label_short(race)
    spots = get_ai_spotlight_top(conn, race, sires, damsires, entries, max_horses=3)
    note_url = get_note_article_url(race)

    parts = [f"📊 {when} {label} AI完全予想\n"]
    # spots が取れた場合のみ最終3頭を出す
    if spots:
        parts.append("【最終3頭(8軸スコア)】")
        marks = ['◎', '○', '▲']
        for i, sp in enumerate(spots[:3]):
            parts.append(f"{marks[i]} {_format_spotlight_line(sp)}")
        parts.append("")
    if note_url:
        parts.append("【詳細分析記事】")
        parts.append(f"▶ {note_url}")
    else:
        parts.append("→ 明日金曜:過小評価馬発見")
        parts.append("→ 土曜朝に印付き完全予想配信🔔")
    parts.append('')
    parts.append(hashtags_fn(race))
    return '\n'.join(parts)


# ─── 金曜 朝: 枠順抽選結果 + 評価変化 ───
# 金曜午前が JRA 枠順抽選のタイミング

def build_fri_morning(race, conn, entries, sires, damsires, today_d, hashtags_fn):
    """金曜朝:枠順抽選結果を踏まえた評価変化(出走2日前)"""
    if not race:
        return None
    when = _phrase_when(race, today_d)
    label = _race_label_short(race)
    venue = race.get('venue', '')
    surface = race.get('surface', '')
    distance = race.get('distance', 0)

    parts = [f"🎲 {when} {label} 枠順抽選結果\n"]

    # コース別 枠順ベスト/ワースト
    if conn:
        try:
            best = conn.execute("""
                SELECT post_position, COUNT(*) t,
                       100.0*SUM(CASE WHEN finish_position<=3 THEN 1 ELSE 0 END)/COUNT(*) rate
                FROM results r JOIN races ra ON r.race_id=ra.race_id
                WHERE ra.venue=? AND ra.surface=? AND ra.distance=?
                  AND r.finish_position>0 AND ra.race_date>='2020-01-01'
                GROUP BY post_position HAVING t>=20
                ORDER BY rate DESC LIMIT 1
            """, (venue, surface, distance)).fetchone()
            if best:
                parts.append("【枠順の鉄則(過去6年)】")
                parts.append(f"⭐{best['post_position']}番枠:複勝率{best['rate']:.1f}%でベスト")
                parts.append("")
        except Exception:
            pass

    # 出走馬の post_position が登録されていれば、有利/不利を判定
    spots = get_ai_spotlight_top(conn, race, sires, damsires, entries, max_horses=3)
    if spots:
        parts.append("【AI注目馬の枠順】")
        for sp in spots[:3]:
            num = sp.get('num', 0)
            name = sp.get('name', '?')
            score = sp.get('score', 0)
            parts.append(f"・{num}番 {name}(score{score})")

    parts.append("\n→ 今日12:30に推奨買い目、明日朝に完全予想配信🔔")
    parts.append('')
    parts.append(hashtags_fn(race))
    return '\n'.join(parts)


# ─── 金曜 昼: 馬券戦略 ───

def build_fri_weekday(race, conn, entries, sires, damsires, today_d, hashtags_fn):
    """金曜昼:過小評価馬(オッズ妙味) + 推奨買い目戦略"""
    if not race:
        return None
    when = _phrase_when(race, today_d)
    label = _race_label_short(race)
    spots = get_ai_spotlight_top(conn, race, sires, damsires, entries, max_horses=3)
    undervalued = get_undervalued_horses(entries, max_horses=1)

    parts = [f"💰 {when} {label} 馬券戦略\n"]
    if spots:
        marks = ['◎', '○', '▲']
        parts.append("【AI 軸馬】")
        for i, sp in enumerate(spots[:3]):
            parts.append(f"{marks[i]} {sp.get('num',0)}番 {sp.get('name','?')}")
        parts.append("")

    # 過小評価馬(オッズ妙味)
    if undervalued:
        u = undervalued[0]
        parts.append(f"💎妙味:{u['num']}番 {u['name']}(人気外+K系)")
        parts.append("")

    # 推奨買い目
    if spots and len(spots) >= 3:
        n1, n2, n3 = spots[0].get('num',0), spots[1].get('num',0), spots[2].get('num',0)
        parts.append("【推奨買い目】")
        parts.append(f"・単勝/複勝 ◎{n1}")
        parts.append(f"・馬連 {n1}-{n2}・{n1}-{n3}")
        parts.append(f"・三連複 {n1}-{n2}-{n3}")

    parts.append("\n→ 明日朝AI予測完了後、最終確定🔔")
    parts.append('')
    parts.append(hashtags_fn(race))
    return '\n'.join(parts)


# ─── 金曜 夜: 翌朝予想配信告知 ───

def build_fri_evening(graded_races, today_d, hashtags_fn):
    """金曜夜:明日のAI予想配信告知"""
    if not graded_races:
        return None

    parts = [f"🔔 明日土曜のAI予想配信お知らせ\n"]
    parts.append("【明日(土)の重賞】")
    for r in graded_races:
        rd = parse_race_date(r)
        if rd and rd.weekday() == 5:  # 土曜
            parts.append(f"・{r.get('race_name','')}({r.get('grade','')}) {r.get('venue','')}")
    parts.append("")
    parts.append("⏰ 土曜朝7:00-10:15に印付き完全予想を配信")
    parts.append("印:◎○▲△×注 + 推奨買い目")
    parts.append("")
    parts.append("(日曜分は別途配信)")
    parts.append("")
    parts.append("#競馬予想 #AI予想")
    return '\n'.join(parts)
