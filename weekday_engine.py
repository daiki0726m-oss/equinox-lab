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
# DB直接集計: 種牡馬/母父
# ───────────────────────────────────────────

def get_sire_top(conn, venue, surface, distance, min_runs=5, top=3, year_min=2020):
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


def get_damsire_top(conn, venue, surface, distance, min_runs=5, top=3, year_min=2020):
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

def build_morning_tweet(race, stats, sires, damsires, today_d, hashtags_fn):
    """共通の朝テンプレ: コース概要をコンパクトに(280字以内)"""
    if not race or not stats:
        return None
    day_phrase = race_day_phrase(race, today_d, with_paren=True)
    parts = [
        f"📊 {day_phrase}の{race.get('race_name','')}{('('+race['grade']+')') if race.get('grade') else ''}",
        f"{race['venue']}{race['surface']}{race['distance']}m | 過去{stats.get('total_races','?')}R 分析",
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
    parts.append("")
    parts.append(predict_announce_phrase(race, today_d))
    parts.append("")
    parts.append(hashtags_fn(race))
    return "\n".join(parts)


def build_weekday_tweet(race, stats, sires, damsires, today_d, hashtags_fn, dow):
    """昼テンプレ: 曜日別に深掘りテーマを変える"""
    if not race or not stats:
        return None
    day_phrase = race_day_phrase(race, today_d, with_paren=True)
    grade_label = f"({race['grade']})" if race.get('grade') else ""
    head = f"🔍 {day_phrase}の{race.get('race_name','')}{grade_label}\n{race['venue']}{race['surface']}{race['distance']}m"

    if dow == 0:  # 月曜昼: 枠順詳細
        body = ["", "【枠順別 複勝率】"]
        fs = stats.get('frame_stats') or []
        for f in fs:
            mark = ''
            if fs:
                best = max(fs, key=lambda x: x.get('top3_rate', 0))
                worst = min(fs, key=lambda x: x.get('top3_rate', 0))
                if f == best:
                    mark = ' ⬆️'
                elif f == worst:
                    mark = ' ⬇️'
            body.append(f"  {f['frame']}枠: {f['top3_rate']}%{mark}")
        body.append("")
        body.append("→ コース別の枠データを毎週分析📊")

    elif dow == 1:  # 火曜昼: 母父TOP3
        body = ["", "【母父 複勝率TOP3 (5頭以上)】"]
        if damsires:
            for ds in damsires:
                body.append(f"  {ds['name']}: {ds['top3']}%({ds['runs']}頭)")
        else:
            body.append("  (データ不足)")
        body.append("")
        body.append("→ 母父は予想の盲点。チェック必須")

    elif dow == 2:  # 水曜昼: 過去5年勝者の人気分布
        ps = stats.get('popularity_stats') or []
        body = ["", "【人気別 成績(複勝率/回収率)】"]
        for p in ps:
            body.append(f"  {p['label']}: {p['top3_rate']}% / 回収{p.get('recovery', 0)}%")
        body.append("")
        if any(p.get('recovery', 0) >= 80 for p in ps):
            body.append("→ 妙味のある人気帯あり")
        else:
            body.append("→ 上位人気が中心の堅いコース")

    elif dow == 3:  # 木曜昼: 末脚使えた馬特集 (上がり3F詳細)
        body = ["", "【上がり3F バケット別】"]
        l3 = sec_last3f(stats, depth='full')
        if l3:
            body.append(l3)
        body.append("")
        body.append("→ 末脚の切れ味が勝敗を分ける")

    elif dow == 4:  # 金曜昼: 土曜重賞詳細
        body = ["", f"【土曜のメイン: {race.get('race_name','')}】"]
        body.append("過去6年の傾向:")
        f = sec_frame(stats, depth='brief')
        if f:
            body.append(f)
        l3 = sec_last3f(stats, depth='brief')
        if l3:
            body.append(l3)
        if sires:
            body.append("注目種牡馬:")
            for s in sires[:2]:
                body.append(f"  {s['name']}: {s['top3']}%({s['runs']}頭)")
        body.append("")
        body.append(predict_announce_phrase(race, today_d))

    else:
        body = []

    parts = [head] + body + ["", hashtags_fn(race)]
    return "\n".join(parts)


def build_evening_tweet(race, stats, sires, damsires, today_d, hashtags_fn, dow):
    """夜テンプレ: 月-木は曜日別、金は日曜G1詳細"""
    if not race or not stats:
        return None
    day_phrase = race_day_phrase(race, today_d, with_paren=True)
    grade_label = f"({race['grade']})" if race.get('grade') else ""
    head = f"🌙 {day_phrase}の{race.get('race_name','')}{grade_label}\n{race['venue']}{race['surface']}{race['distance']}m"

    if dow == 0:  # 月曜夜: コース全体の傾向
        body = ["", "【コース傾向(過去6年)】"]
        ff = sec_frame(stats, depth='full')
        if ff:
            body.append(ff)
        p = sec_pace(stats)
        if p:
            body.append(p)
        body.append("")
        body.append("→ このコースは"+ ("先行馬有利" if p and ('逃げ' in p or '先行' in p) else "差し馬の出番") if p else "")

    elif dow == 1:  # 火曜夜: 種牡馬深掘り
        body = ["", "【種牡馬TOP3(複勝率)】"]
        if sires:
            for s in sires:
                body.append(f"  {s['name']}: {s['top3']}%({s['runs']}頭)")
        else:
            body.append("  (DB不足、後日更新)")
        if damsires:
            body.append("")
            body.append("【母父TOP1】")
            ds = damsires[0]
            body.append(f"  {ds['name']}: {ds['top3']}%({ds['runs']}頭)")

    elif dow == 2:  # 水曜夜: 穴馬条件(人気別+回収率)
        body = ["", "【穴馬の条件(人気×回収率)】"]
        ps = stats.get('popularity_stats') or []
        anaba = [p for p in ps if p['label'] in ('7-9人気', '10人気以下')]
        for p in anaba:
            body.append(f"  {p['label']}: 複勝{p['top3_rate']}% / 回収{p.get('recovery', 0)}%")
        body.append("")
        if any(p.get('recovery', 0) >= 80 for p in anaba):
            body.append("→ 穴馬での妙味あり🔥")
        else:
            body.append("→ 堅実派向きのコース")

    elif dow == 3:  # 木曜夜: 全データまとめ
        body = ["", f"【{race.get('race_name','')} 総合傾向】"]
        f = sec_frame(stats, depth='brief')
        if f:
            body.append(f)
        p = sec_pace(stats)
        if p:
            body.append(p)
        l3 = sec_last3f(stats, depth='brief')
        if l3:
            body.append(l3)
        if sires:
            body.append(f"種牡馬No.1: {sires[0]['name']} {sires[0]['top3']}%")
        body.append("")
        body.append(predict_announce_phrase(race, today_d))

    elif dow == 4:  # 金曜夜: 日曜G1詳細
        body = ["", f"【日曜の最重要レース】"]
        body.append("過去6年の勝ちパターン:")
        f = sec_frame(stats, depth='brief')
        if f:
            body.append(f)
        l3 = sec_last3f(stats, depth='brief')
        if l3:
            body.append(l3)
        if sires:
            body.append("注目種牡馬:")
            for s in sires[:2]:
                body.append(f"  {s['name']} {s['top3']}%({s['runs']}頭)")
        if damsires:
            body.append(f"母父1位: {damsires[0]['name']} {damsires[0]['top3']}%")
        body.append("")
        body.append(predict_announce_phrase(race, today_d))

    else:
        body = []

    parts = [head] + body + ["", hashtags_fn(race)]
    return "\n".join(parts)


# ───────────────────────────────────────────
# 統合エントリポイント
# ───────────────────────────────────────────

def build_post_for_slot(slot, today_d, conn, get_todays_race_fn, get_course_stats_fn,
                        get_entry_jockeys_fn, hashtags_fn, jockey_filter_fn):
    """slot='morning'|'weekday'|'evening' のツイートを生成"""
    dow = today_d.weekday()
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
        return None

    # フォールバックで「先週末のレース」が返ってきたら投稿しない(混乱回避)
    rd = parse_race_date(race)
    if rd and rd < today_d:
        print(f"⚠️ {slot}: 取得したレース({race.get('race_name','')})の日付が過去({rd}) → 投稿スキップ")
        return None

    venue = race['venue']
    surface = race['surface']
    distance = race['distance']
    stats = get_course_stats_fn(venue, surface, distance)
    if not stats:
        return None

    # 種牡馬・母父
    sires = []
    damsires = []
    try:
        sires = get_sire_top(conn, venue, surface, distance, min_runs=5, top=3)
        damsires = get_damsire_top(conn, venue, surface, distance, min_runs=5, top=3)
    except Exception:
        pass

    if slot == 'morning':
        return build_morning_tweet(race, stats, sires, damsires, today_d, hashtags_fn)
    elif slot == 'weekday':
        return build_weekday_tweet(race, stats, sires, damsires, today_d, hashtags_fn, dow)
    else:
        return build_evening_tweet(race, stats, sires, damsires, today_d, hashtags_fn, dow)
