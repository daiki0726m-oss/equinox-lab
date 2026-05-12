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
