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


def _footer(cta=True):
    return ([DASH_CTA] if cta else []) + [ASSUMED_NOTE]


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

def _p_ability_gap(race, horses, n_other):
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
    lines += _footer()
    return "\n".join(lines)


def _p_minimal(race, horses, n_other):
    """本命1頭 + 根拠1行 + 相手/穴。最も読みやすい万人向け。"""
    mk = _mark_map(horses)
    honmei = mk.get("◎")
    if not honmei:
        return None
    reason = _top_reason(honmei)
    lines = [f"【{_race_title(race)}】AIの本命", "",
             f"◎ {honmei.get('horse_name','?')}{_label(honmei)}", ""]
    if reason:
        lines += [reason, ""]
    aite = [mk[m].get("horse_name", "") for m in ("○", "▲") if m in mk]
    if aite:
        lines.append("相手 " + " / ".join(aite))
    chu = mk.get("注")
    if chu:
        lines.append(f"穴  {chu.get('horse_name','?')}{_label(chu)}")
    lines += [""] + _footer()
    return "\n".join(lines)


def _p_transparency(race, horses, n_other):
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


def _p_upset(race, horses, n_other):
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
    lines += [""] + _footer()
    return "\n".join(lines)


def _p_chu_value(race, horses, n_other):
    """注 (妙味longshot) を主役にした夢枠。"""
    mk = _mark_map(horses)
    chu, honmei = mk.get("注"), mk.get("◎")
    if not chu or not honmei or (chu.get("odds_win") or 0) < 7:
        return None
    lines = [f"{_race_title(race)}、AIが妙味とみた1頭。", "",
             f"⚡ {chu.get('horse_name','?')}{_label(chu)}", "",
             "AIの複勝評価に対して市場の評価が低い、",
             "つまり「来る確率のわりに配当が大きい」と判定した馬です。", "",
             f"本命は ◎{honmei.get('horse_name','?')}{_label(honmei)}。",
             "軸は堅く、紐で夢を見る形。", ""] + _footer()
    return "\n".join(lines)


def _p_confidence(race, horses, n_other):
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
    lines += [f"※{FREEZE}", ""] + _footer()
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
                               date_str=None):
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
            text = fn(race, horses, n_other_races)
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
