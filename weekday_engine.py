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


def build_morning_tweet(race, stats, sires, damsires, entries, today_d, hashtags_fn):
    """朝テンプレ: コース概要をコンパクトに(280字以内、データ範囲明示)"""
    if not race or not stats:
        return None
    day_phrase = race_day_phrase(race, today_d, with_paren=True)
    parts = [
        f"📊 {day_phrase}の{race.get('race_name','')}{('('+race['grade']+')') if race.get('grade') else ''}",
        _course_scope_label(race, stats),
        "",
    ]
    f = sec_frame(stats, depth='brief')
    if f:
        parts.append(f)
    p = sec_pace(stats)
    if p:
        parts.append(p)
    l3 = sec_last3f(stats, depth='brief')
    if l3:
        parts.append(l3)
    # 出走馬血統が揃っている場合は1行だけクロス参照を入れる
    if entries:
        cs = cross_reference_sires(sires or [], entries, key='sire', limit=1)
        if cs:
            s = cs[0]
            names = '・'.join(s['entries'][:2])
            parts.append(f"🧬 {s['name']}産駒({names})は当コース複勝{s['top3']}%")
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

    if dow == 0:  # 月曜昼: 枠順詳細
        body = ["", "【枠順別 複勝率】"]
        fs = stats.get('frame_stats') or []
        if fs:
            best = max(fs, key=lambda x: x.get('top3_rate', 0))
            worst = min(fs, key=lambda x: x.get('top3_rate', 0))
            for f in fs:
                mark = ' ⬆️' if f == best else (' ⬇️' if f == worst else '')
                body.append(f"  {f['frame']}枠: {f['top3_rate']}%({f['runs']}走){mark}")
        body.append("")
        body.append("→ 来週のレースに向けたコース傾向")

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

    elif dow == 2:  # 水曜昼: 人気別成績
        ps = stats.get('popularity_stats') or []
        body = ["", "【人気別 成績】(コース全レース)"]
        for p in ps:
            body.append(f"  {p['label']}: 複勝{p['top3_rate']}% / 回収{p.get('recovery', 0)}%")
        body.append("")
        if any(p.get('recovery', 0) >= 80 for p in ps):
            body.append("→ 妙味のある人気帯あり🔥")
        else:
            body.append("→ 上位人気中心の堅いコース")

    elif dow == 3:  # 木曜昼: 末脚詳細
        body = ["", "【上がり3F バケット別】(コース全レース)"]
        l3 = sec_last3f(stats, depth='full')
        if l3:
            body.append(l3)
        body.append("")
        body.append("→ 末脚の切れ味が勝敗を分ける")

    elif dow == 4:  # 金曜昼: 土曜重賞詳細(出走馬血統クロス)
        body = ["", f"【{day_phrase}のメイン: {race.get('race_name','')}】"]
        f = sec_frame(stats, depth='brief')
        if f:
            body.append(f)
        l3 = sec_last3f(stats, depth='brief')
        if l3:
            body.append(l3)
        cs = cross_reference_sires(sires or [], entries or [], key='sire', limit=2)
        if cs:
            body.append("出走馬の注目種牡馬:")
            for s in cs:
                names = '・'.join(s['entries'][:2])
                body.append(f"  {s['name']}({names}): 複勝{s['top3']}% ({s['runs']}走中)")
        body.append("")
        pa = predict_announce_phrase(race, today_d)
        if pa:
            body.append(pa)

    else:
        body = []

    parts = [head] + body + ["", hashtags_fn(race)]
    return "\n".join(parts)


def build_evening_tweet(race, stats, sires, damsires, entries, workouts, today_d, hashtags_fn, dow):
    """夜テンプレ: 出走馬とのクロス参照を含む"""
    if not race or not stats:
        return None
    day_phrase = race_day_phrase(race, today_d, with_paren=True)
    grade_label = f"({race['grade']})" if race.get('grade') else ""
    head = f"🌙 {day_phrase}の{race.get('race_name','')}{grade_label}\n{_course_scope_label(race, stats)}"

    if dow == 0:  # 月曜夜: コース全体の傾向
        body = ["", "【コース傾向】"]
        ff = sec_frame(stats, depth='full')
        if ff:
            body.append(ff)
        p = sec_pace(stats)
        if p:
            body.append(p)

    elif dow == 1:  # 火曜夜: 出走馬の種牡馬実績
        body = ["", "【出走馬の父×コース実績】"]
        cs = cross_reference_sires(sires or [], entries or [], key='sire', limit=3)
        if cs:
            for s in cs:
                names = '・'.join(s['entries'][:3])
                body.append(f"  {s['name']}({names})")
                body.append(f"   → {s['runs']}走中複勝{s['top3']}%")
        else:
            body.append("  該当する種牡馬データなし")
        cd = cross_reference_sires(damsires or [], entries or [], key='damsire', limit=1)
        if cd:
            d = cd[0]
            names = '・'.join(d['entries'][:2])
            body.append(f"母父注目: {d['name']}({names}) 複勝{d['top3']}%")

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
        body = ["", f"【{race.get('race_name','')} 総合傾向】"]
        f = sec_frame(stats, depth='brief')
        if f:
            body.append(f)
        l3 = sec_last3f(stats, depth='brief')
        if l3:
            body.append(l3)
        cs = cross_reference_sires(sires or [], entries or [], key='sire', limit=1)
        if cs:
            s = cs[0]
            names = '・'.join(s['entries'][:2])
            body.append(f"出走馬の父注目: {s['name']}({names}) 複勝{s['top3']}%")
        ws = workout_section(workouts, max_show=2)
        if ws:
            body.append("")
            body.append(ws)
        body.append("")
        pa = predict_announce_phrase(race, today_d)
        if pa:
            body.append(pa)

    elif dow == 4:  # 金曜夜: 日曜G1詳細(出走馬クロス + 追い切り)
        body = ["", "【勝ちパターン】"]
        f = sec_frame(stats, depth='brief')
        if f:
            body.append(f)
        l3 = sec_last3f(stats, depth='brief')
        if l3:
            body.append(l3)
        cs = cross_reference_sires(sires or [], entries or [], key='sire', limit=2)
        if cs:
            body.append("")
            body.append("【出走馬の父×コース実績】")
            for s in cs:
                names = '・'.join(s['entries'][:2])
                body.append(f"  {s['name']}({names}): 複勝{s['top3']}% ({s['runs']}走)")
        # 追い切り評価
        ws = workout_section(workouts, max_show=3)
        if ws:
            body.append("")
            body.append(ws)
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
        tweet = build_morning_tweet(race, stats, sires, damsires, entries, today_d, hashtags_fn)
    elif slot == 'weekday':
        tweet = build_weekday_tweet(race, stats, sires, damsires, entries, today_d, hashtags_fn, dow)
    else:
        tweet = build_evening_tweet(race, stats, sires, damsires, entries, workouts, today_d, hashtags_fn, dow)

    if return_race:
        return tweet, race.get('race_id')
    return tweet
