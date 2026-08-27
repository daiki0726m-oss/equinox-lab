"""Threads 専用の投稿コンポーザ (#122 2026-08-24)

背景: Threads へは X 用スレッドを 500字チャンクに再分割して丸ごと流していた (#103)。
その結果 6投稿×400字超・84頭の馬名が並ぶ「記号の壁」になり、一般にチェーンの
2投稿目以降は表示が激減するため情報の大半が読まれない状態だった。加えて最大の
差別化要素 (オッズ非依存の能力値評価) が2投稿目の末尾に埋もれていた。

方針:
  - **1投稿完結** (チェーンにしない) / 主役は1レース (他レースはダッシュボード導線)
  - **パターンをローテーション** — 毎回同じ型だと飽きられる (#49 の朝ローテと同じ思想)。
    データ条件を満たすパターンだけを候補にし、直近使用分を避けて選ぶ。
  - 数値は predictions cache 由来の実測のみ。**朝の人気/オッズは「想定」と明記**
    (確定は発走直前 — #118 の「オッズ確定！」と同じ誤りを繰り返さない)。
"""
import json
import os

MAX_LEN = 500                      # Threads の1投稿上限
HISTORY = os.path.join("docs", "data", ".threads_pattern_history.json")
DASH_CTA = "他レースのAI印はプロフィールのリンクから"
FREEZE = "印は投稿時点で凍結記録 (削除・後出しなし)"
# 朝の人気・オッズは確定ではない (発走直前まで動く)。#118 の「オッズ確定！」と同じ
# 誤りを繰り返さないため、想定であることを毎回明示する。
ASSUMED_NOTE = "※人気・オッズは投稿時点の想定（確定は発走直前）"
CHAKU_NOTE = "※[1-2-3-着外]＝1着・2着・3着・4着以下の回数"


# ── ヘルパー ────────────────────────────────────────────────
def _mark_map(horses):
    return {h.get("mark"): h for h in horses if h.get("mark")}


def _pop(h):
    """想定人気の表記 (朝時点なので確定ではない)。"""
    p = h.get("popularity") or 0
    return f"想定{p}番人気" if p else ""


def _odds(h):
    o = h.get("odds_win") or 0
    return f"{o:.1f}倍" if o else ""


def _label(h):
    parts = [x for x in (_pop(h), _odds(h)) if x]
    return f"（{'・'.join(parts)}）" if parts else ""


def _footer(cta=True, body=""):
    """末尾の注記。着度数を使った投稿にだけ読み方の1行を足す (初見の読者向け)。"""
    notes = []
    if "[" in body and "-" in body:
        notes.append(CHAKU_NOTE)
    notes.append(ASSUMED_NOTE)
    return ([DASH_CTA] if cta else []) + notes


def _race_title(race):
    """重賞は「レース名(G3)」、平場は「会場11R レース名」で識別できるようにする。"""
    name = race.get("race_name", "") or ""
    g = race.get("grade") or ""
    if g in ("G1", "G2", "G3", "OP", "L", "リステッド"):
        return f"{name}({g})" if g not in name else name
    venue = race.get("venue", "") or ""
    rno = race.get("race_number") or 0
    head = f"{venue}{rno}R " if venue and rno else ""
    return f"{head}{name}".strip()


def _ability_rank(horses):
    """オッズ非依存の能力値ランキング (上位のみ)。"""
    scored = [h for h in horses if (h.get("ability_score") or 0) > 0]
    return sorted(scored, key=lambda h: -(h.get("ability_score") or 0))


# ── 着度数 (競馬慣習の [1着-2着-3着-着外] 表記、#55) ──────────
def _chaku(p1, p2, p3, total):
    return f"[{p1}-{p2}-{p3}-{max(total - p1 - p2 - p3, 0)}]"


def course_records(conn, race, horse_numbers, years=6):
    """指定馬の「鞍上・父・母父の当コース着度数」を引く (#125)。

    率だけだと「13走で38%」が何を意味するか読者に伝わらないため、競馬慣習の
    着度数 [1-2-3-着外] を併記する。対象は本命・注など数頭なのでコストは軽い。
    """
    from datetime import datetime
    venue = race.get("venue", "") or ""
    surface = race.get("surface", "") or ""
    distance = race.get("distance") or 0
    if not (venue and surface and distance):
        return {}
    cur = conn.cursor()
    y = datetime.now().year
    out = {}
    ent = {}
    cur.execute("""SELECT res.horse_number, j.jockey_name, h.sire, h.damsire
                   FROM results res
                   LEFT JOIN jockeys j ON res.jockey_id = j.jockey_id
                   LEFT JOIN horses h ON res.horse_id = h.horse_id
                   WHERE res.race_id = ?""", (race.get("race_id"),))
    for hn, jk, sire, damsire in cur.fetchall():
        ent[hn] = ((jk or "").strip(),
                   (sire or "").split("(")[0].strip(),
                   (damsire or "").split("(")[0].strip())

    def _tally(sql, params, min_n):
        cur.execute(sql, params)
        r = cur.fetchone()
        if not r or not r[0] or r[0] < min_n:
            return None
        n, p1, p2, p3 = r[0], r[1] or 0, r[2] or 0, r[3] or 0
        return {"n": n, "chaku": _chaku(p1, p2, p3, n),
                "pct": 100.0 * (p1 + p2 + p3) / n}

    counts = ("COUNT(*), SUM(CASE WHEN res.finish_position=1 THEN 1 ELSE 0 END), "
              "SUM(CASE WHEN res.finish_position=2 THEN 1 ELSE 0 END), "
              "SUM(CASE WHEN res.finish_position=3 THEN 1 ELSE 0 END)")
    for hn in horse_numbers:
        jk, sire, damsire = ent.get(hn, ("", "", ""))
        rec = {"jockey": jk, "sire": sire, "damsire": damsire}
        if jk:
            rec["jockey_rec"] = _tally(
                f"""SELECT {counts} FROM races r JOIN results res ON r.race_id=res.race_id
                    JOIN jockeys j ON res.jockey_id=j.jockey_id
                    WHERE r.venue=? AND r.surface=? AND r.distance=? AND r.race_date>=?
                      AND j.jockey_name=? AND res.finish_position>0""",
                (venue, surface, distance, f"{y-3}-01-01", jk), 8)
        for key, col, val in (("sire_rec", "h.sire", sire), ("dam_rec", "h.damsire", damsire)):
            if val:
                rec[key] = _tally(
                    f"""SELECT {counts} FROM races r JOIN results res ON r.race_id=res.race_id
                        JOIN horses h ON res.horse_id=h.horse_id
                        WHERE r.venue=? AND r.surface=? AND r.distance=? AND r.race_date>=?
                          AND {col}=? AND res.finish_position>0""",
                    (venue, surface, distance, f"{y-years}-01-01", val), 6)
        out[hn] = rec
    return out


# ── 穴予兆スコアの内部表記を、ロジックを知らない読者向けに翻訳 (#125) ──
# volatility.compute_anasanee_score が返す文字列は "騎手変更(+1)" のような内部表記で、
# 加点値もそのまま出ていた。読者には意味が伝わらないので自然文に開く。
def _plain_ana(reason):
    import re as _re
    r = _re.sub(r"\(\+\d+\)", "", str(reason or "")).strip()
    if not r:
        return None
    m = _re.match(r"前走(\d+)着$", r)
    if m:
        return f"前走は{m.group(1)}着に敗れ、人気を落としての一戦"
    m = _re.match(r"\+?(-?\d+)m大幅(延長|短縮)$", r)
    if m:
        return f"前走から距離が{abs(int(m.group(1)))}m{m.group(2)}"
    if r == "前走追込":
        return "前走は後方から差す競馬 — 展開が向けば一発がある脚質"
    if r == "前走差し":
        return "前走は差す競馬 — 展開次第で浮上できる脚質"
    m = _re.match(r"\+?(-?\d+)kg大幅(減|増)$", r)
    if m:
        return f"前走から馬体重{m.group(1)}kg"
    if r == "騎手変更":
        return "前走から乗り替わり"
    m = _re.match(r"中休み(\d+)日$", r)
    if m:
        return f"前走から{m.group(1)}日空けての出走"
    m = _re.match(r"(.+)産駒$", r)
    if m:
        return f"父{m.group(1)}は人気薄での好走が目立つ系統"
    m = _re.match(r"(.+)騎手$", r)
    if m:
        return f"{m.group(1)}騎手は人気薄をよく持ってくる"
    return r


def _rank_of(horses, horse, key):
    """出走馬中の順位 (降順)。値が無ければ None。"""
    vals = [(h.get("horse_number"), h.get(key) or 0) for h in horses]
    if not any(v for _, v in vals):
        return None
    ordered = sorted(vals, key=lambda x: -x[1])
    for i, (hn, _v) in enumerate(ordered, 1):
        if hn == horse.get("horse_number"):
            return i
    return None


def eval_points(horse, horses, stats=None, limit=3):
    """その馬を「何で評価したか」を具体的に2-3点返す (#124)。

    汎用の説明文 (「複勝評価に対し市場が低い＝配当が大きい」等) は毎回同じで情報量ゼロ
    (#94 の再発) なので使わない。順位・実測率など、その馬固有の数字だけを出す。
    stats は post_sections._score_entries_by_course の結果を horse_number で引ける dict。
    """
    n = len(horses)
    pop = horse.get("popularity") or 0
    pts = []

    t3_rank = _rank_of(horses, horse, "pred_top3_pct")
    if t3_rank and pop and t3_rank + 2 <= pop:
        pts.append(f"AIの複勝評価は{n}頭中{t3_rank}位（市場は想定{pop}番人気）")

    ab_rank = _rank_of(horses, horse, "ability_score")
    if ab_rank and ab_rank <= 3 and pop >= 4:
        pts.append(f"オッズを一切見ない能力評価でも{ab_rank}位")

    si = horse.get("si_avg") or 0
    si_rank = _rank_of(horses, horse, "si_avg")
    if si and si_rank and si_rank <= 3:
        pts.append(f"スピード指数{si:.0f}は{n}頭中{si_rank}位")

    # 当コースの実績は着度数 [1着-2着-3着-着外] で示す (#55: 率だけだと母数が伝わらない)
    st = (stats or {}).get(horse.get("horse_number")) or {}
    jr = st.get("jockey_rec")
    if st.get("jockey") and jr and jr["pct"] >= 25:
        pts.append(f"鞍上{st['jockey']}は当コース {jr['chaku']} 複勝率{jr['pct']:.0f}%")
    sr = st.get("sire_rec")
    if st.get("sire") and sr and sr["pct"] >= 30:
        pts.append(f"父{st['sire']}産駒は当コース {sr['chaku']} 複勝率{sr['pct']:.0f}%")
    dr = st.get("dam_rec")
    if st.get("damsire") and dr and dr["pct"] >= 35:
        pts.append(f"母父{st['damsire']}は当コース {dr['chaku']} 複勝率{dr['pct']:.0f}%")

    for r in (horse.get("anasanee_reasons") or []):
        plain = _plain_ana(r)
        if plain and not any(plain[:5] in p for p in pts):
            pts.append(plain)
            break
    return pts[:limit]


def _top_reason(h):
    """◎の推奨理由から表示に向く1行を選ぶ (紙面転記は既に排除済み #117)。"""
    for r in h.get("reasons") or []:
        if any(k in r for k in ("市場", "妙味", "過剰", "乖離")):
            return r
    for r in h.get("reasons") or []:
        if "SI" in r:
            return r.replace("SI", "スピード指数")   # 業界略語は開いて出す (新規読者向け)
    rs = h.get("reasons") or []
    return rs[0] if rs else ""


# ── パターン定義 ────────────────────────────────────────────
# 各 build_* は (text|None) を返す。None = このレースでは条件を満たさず不成立。

def _p_ability_gap(race, horses, n_other, stats=None):
    """能力値 (オッズ非依存) が市場と食い違う時だけ成立する、うち固有の切り口。"""
    rank = _ability_rank(horses)
    if len(rank) < 3:
        return None
    top = rank[0]
    if (top.get("popularity") or 0) < 4:
        return None                      # 市場と一致 = 語る価値がない
    mk = _mark_map(horses)
    honmei = mk.get("◎")
    if not honmei:
        return None
    lines = [f"{_race_title(race)}、AIと市場が食い違っています。", ""]
    lines.append("オッズを一切見ない「能力値」で並べると──")
    for i, h in enumerate(rank[:3], 1):
        lines.append(f"{i}位 {h.get('horse_name','?')}{_label(h)}")
    lines.append("")
    lines.append(f"市場が{_odds(top)}をつけた馬を、AIは最上位に置きました。")
    lines.append("")
    lines.append(f"本命は ◎{honmei.get('horse_name','?')}{_label(honmei)}。")
    lines.append(f"ただし3連系の紐には{top.get('horse_name','?')}を入れます。")
    lines.append("")
    lines += _footer(body="\n".join(lines))
    return "\n".join(lines)


def _p_minimal(race, horses, n_other, stats=None):
    """本命1頭 + 評価根拠 + 相手/穴。最も読みやすい万人向け。"""
    mk = _mark_map(horses)
    honmei = mk.get("◎")
    if not honmei:
        return None
    pts = eval_points(honmei, horses, stats, limit=2) or [_top_reason(honmei)]
    lines = [f"【{_race_title(race)}】AIの本命", "",
             f"◎ {honmei.get('horse_name','?')}{_label(honmei)}", ""]
    lines += [f"・{p}" for p in pts if p]
    lines += [""]
    aite = [mk[m].get("horse_name", "") for m in ("○", "▲") if m in mk]
    if aite:
        lines.append("相手 " + " / ".join(aite))
    chu = mk.get("注")
    if chu:
        lines.append(f"穴  {chu.get('horse_name','?')}{_label(chu)}")
    lines += [""] + _footer(body="\n".join(lines))
    return "\n".join(lines)


def _p_transparency(race, horses, n_other, stats=None):
    """凍結記録と全成績公開を前面に — フォロー動機を直接作る型 (#115)。"""
    mk = _mark_map(horses)
    honmei, chu = mk.get("◎"), mk.get("注")
    if not honmei:
        return None
    lines = [f"{_race_title(race)}、AIの結論。", "",
             f"◎ {honmei.get('horse_name','?')}{_label(honmei)}"]
    if chu:
        lines.append(f"⚡ 穴は {chu.get('horse_name','?')}{_label(chu)}")
    lines += ["",
              f"この印は投稿と同時に凍結記録されます。削除も後出しもできません。",
              "外れた週も含めて、成績は毎週そのまま公開します。",
              "",
              "あなたの本命は？",
              "",
              ASSUMED_NOTE]
    return "\n".join(lines)


def _p_upset(race, horses, n_other, stats=None):
    """荒れ履歴のあるレース限定。事前に「荒れる」と言える強みを出す (#96/#114)。"""
    uh = race.get("upset_hist") or {}
    if uh.get("label") not in ("荒れやすい", "紐荒れ"):
        return None
    mk = _mark_map(horses)
    honmei, chu = mk.get("◎"), mk.get("注")
    if not honmei:
        return None
    kind = "荒れやすい" if uh["label"] == "荒れやすい" else "勝ち馬は堅いが2-3着が荒れる"
    lines = [f"{_race_title(race)}は{kind}レースです。", "",
             f"同レース過去{uh.get('n','?')}年: 勝ち馬の平均{uh.get('avg_win_pop',0):.1f}番人気"
             f"・二桁人気が馬券内に入った年 {uh.get('big_rate',0)*100:.0f}%", "",
             f"本命 ◎{honmei.get('horse_name','?')}{_label(honmei)} は動かしませんが、",
             "手を広げて構えるのが過去の傾向に沿った買い方です。"]
    if chu:
        lines += ["", f"穴で拾うなら {chu.get('horse_name','?')}{_label(chu)}"]
    lines += [""] + _footer(body="\n".join(lines))
    return "\n".join(lines)


def _p_chu_value(race, horses, n_other, stats=None):
    """注 (妙味longshot) を主役にした夢枠。評価根拠はその馬固有の数字で示す (#124)。"""
    mk = _mark_map(horses)
    chu, honmei = mk.get("注"), mk.get("◎")
    if not chu or not honmei or (chu.get("odds_win") or 0) < 7:
        return None
    pts = eval_points(chu, horses, stats)
    if not pts:
        return None          # 具体的な根拠が出せないなら、この型は使わない
    lines = [f"{_race_title(race)}、AIが妙味とみた1頭。", "",
             f"⚡ {chu.get('horse_name','?')}{_label(chu)}", ""]
    lines += [f"・{p}" for p in pts]
    lines += ["",
              f"本命は ◎{honmei.get('horse_name','?')}{_label(honmei)}。",
              "軸は堅く、紐で夢を見る形。", ""]
    lines += _footer(body="\n".join(lines))
    return "\n".join(lines)


def _p_confidence(race, horses, n_other, stats=None):
    """信頼度が高い日だけ「今日はここ」と言い切る型。"""
    if race.get("confidence") not in ("S", "A"):
        return None
    mk = _mark_map(horses)
    honmei = mk.get("◎")
    if not honmei:
        return None
    disp = honmei.get("pred_win_display_pct") or 0
    if disp < 25:
        return None
    grade = "今週で最も自信のある" if race.get("confidence") == "S" else "自信のある"
    aite = [mk[m].get("horse_name", "") for m in ("○", "▲") if m in mk]
    lines = [f"今日、AIが{grade}レースは {_race_title(race)}。", "",
             f"◎ {honmei.get('horse_name','?')}{_label(honmei)}",
             f"AI勝率 {disp:.0f}% — 出走馬の中で頭ひとつ抜けています。", ""]
    if aite:
        lines += ["相手 " + " / ".join(aite), ""]
    lines += [f"※{FREEZE}", ""]
    lines += _footer(body="\n".join(lines))
    return "\n".join(lines)


PATTERNS = [
    ("ability_gap", _p_ability_gap),
    ("minimal", _p_minimal),
    ("upset", _p_upset),
    ("transparency", _p_transparency),
    ("chu_value", _p_chu_value),
    ("confidence", _p_confidence),
]


# ── ローテーション ──────────────────────────────────────────
def _load_history():
    try:
        with open(HISTORY, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"recent": []}


def _save_history(name):
    h = _load_history()
    recent = [x for x in h.get("recent", []) if x != name]
    recent.insert(0, name)
    h["recent"] = recent[:3]          # 直近3回は繰り返さない
    try:
        os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
        with open(HISTORY, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def build_threads_predict_post(race, n_other_races=0, force_pattern=None, record=True,
                               date_str=None, stats=None):
    """1レース1投稿の Threads 用テキストを返す (成立しなければ (None, None))。

    ローテーションは **日付から決定的に** 決める。GitHub Actions は run ごとに新規
    checkout するため、履歴ファイル依存だと毎回リセットされて同じ型ばかり出てしまう
    (#56 の「push されない lock は無いのと同じ」と同型の罠)。日付起点なら状態不要で
    日替わりになり、履歴ファイルは「同じ日に複数回投稿した時の重複回避」に留める。
    """
    horses = race.get("horses") or []
    if not horses:
        return None, None
    candidates = []
    for name, fn in PATTERNS:
        if force_pattern and name != force_pattern:
            continue
        try:
            text = fn(race, horses, n_other_races, stats)
        except Exception:
            text = None
        if text and len(text) <= MAX_LEN:
            candidates.append((name, text))
    if not candidates:
        return None, None
    if force_pattern:
        return candidates[0]
    # 日付起点のオフセットで「成立した候補の中を」循環させる。
    # PATTERNS の固定インデックスで回すと、条件の厳しい型 (ability_gap 等) が不成立の日は
    # 常に次の型 (minimal) に落ちて結果的に毎回同じになる — 実際 3日連続 minimal が出た。
    # 候補集合の長さで回すことで、成立する型が2つ以上ある限り必ず日替わりになる。
    try:
        offset = int(str(date_str or "")[-4:] or 0)
    except Exception:
        offset = 0
    recent = _load_history().get("recent", [])
    idx = offset % len(candidates)
    rotated = candidates[idx:] + candidates[:idx]
    # 同日中の複数回投稿でのみ直近使用を避ける (日跨ぎは offset が担保)
    name, text = next((c for c in rotated if c[0] not in recent[:1]), rotated[0])
    if record:
        _save_history(name)
    return name, text
