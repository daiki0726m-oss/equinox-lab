"""期待値 (実測) を読む単一情報源 (#163)

`docs/data/ev_table.json` (scripts/build_ev_table.py が生成) を読み、
「同じくらいのオッズの馬を過去に100円ずつ買い続けたらどうなったか」を返す。

なぜ AI勝率 × オッズ でないのか
-------------------------------
AI勝率 × オッズ の期待値は実際の回収と合わなかった (2026-09-29 検証)。
「期待値1.2超え」の馬は AI の見込みの約1/3しか勝たず、勝ち数は市場のオッズどおりだった。
週次の再学習で勝率の尺度が動くので、数字の意味も事前に保証できない。
ここで返す値は過去の実測なので、投稿印で答え合わせすると一致する
(◎の複勝: 的中 実測170/予想168、回収 実測83円/予想84円、7/11〜9/27)。

何で決まるか: オッズ・頭数・レース区分 (平場 / 未勝利・新馬) と、印ごとに実測した
「想定オッズ → 締切オッズ」の動き。AI の勝率は使っていない。印の種類が効くのは
締切までのオッズの動きだけで、☆・注 は伸びやすいぶん同じ想定オッズでも的中率が低めに出る。
読者が券種と点数を決める材料 (当たりやすさ と 当たった時の額) を、水増しなしで出すためのもの。
"""
import json
import os
import threading

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "docs", "data", "ev_table.json")
_LOCK = threading.Lock()
_CACHE = {"mtime": None, "data": None}

MIN_FIELD_PLACE = 5      # 4頭以下は複勝が発売されない
MARK_ORDER = ["◎", "○", "▲", "△", "×", "☆", "注"]


def load(path=None):
    p = path or _PATH
    try:
        m = os.path.getmtime(p)
    except OSError:
        return None
    with _LOCK:
        if _CACHE["mtime"] == m and _CACHE["data"] is not None:
            return _CACHE["data"]
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError):
            return None
        _CACHE["mtime"], _CACHE["data"] = m, data
        return data


def since_label():
    """表示用の集計期間 (「2020年以降」)。期間は毎週伸びるので年数は書かない。"""
    d = load() or {}
    y = d.get("start_year") or str((d.get("period") or ["2020"])[0])[:4]
    return f"{y}年以降"


def role_of(mark):
    """印の役割。締切までのオッズの動きは役割で大きく違う
    (◎○▲△ は平均約1割下がる / ☆・注 は想定オッズで選ぶので伸びやすい)。"""
    if mark == "注":
        return "chu"
    if mark == "☆":
        return "value"
    return "capture"


def _band(d, o):
    edges, labels = d["edges"], d["labels"]
    i = 0
    for k, e in enumerate(edges):
        if o >= e:
            i = k
    return labels[min(i, len(labels) - 1)]


def layer_of(race_name):
    """平場 / 未勝利・新馬 / 障害 (build_ev_table.layer_of と同じ規則)。"""
    try:
        from race_utils import is_jump_race
        if is_jump_race(race_name or ""):
            return "jump"
    except Exception:
        pass
    n = race_name or ""
    if "未勝利" in n or "新馬" in n:
        return "maiden"
    return "flat"


def expect(odds, n_runners, mark="◎", final_odds=False, race_name=None):
    """想定オッズ odds の馬の実測。表が無い・オッズが無効・障害なら None。

    final_odds=True: 締切間際〜確定のオッズを渡す時。締切までの動きは混ぜずに表をそのまま引く。
    race_name: 層 (平場 / 未勝利・新馬) の判定に使う。省略時は平場。
    n_runners: 出走予定頭数。7頭以下なら「複勝2着まで」の表を引く (表側は実際の払戻箇所で分けている)。

    返り値 (率は 0-1、払戻と期待値は100円あたりの円):
      {'place_rate', 'place_pay', 'place_ev', 'win_rate', 'win_pay', 'win_ev'}
    place_* は複勝が発売されない頭数 (4頭以下) では None。
    """
    d = load()
    try:
        o = float(odds or 0)
        n = int(n_runners or 0)
    except (TypeError, ValueError):
        return None
    if not d or o <= 1.0 or o >= 999 or n <= 1:
        return None
    layer = layer_of(race_name)
    if layer == "jump":
        return None                # 障害は実測表を持たない
    tab = ((d.get("table") or {}).get(layer) or {}).get(
        "small" if n <= d.get("small_field", 7) else "big") or {}
    if final_odds:
        ratios = [1.0]
    else:
        drift = (d.get("drift") or {}).get(role_of(mark)) or {}
        ent = drift.get(_band(d, o)) or drift.get("all")
        ratios = (ent or {}).get("q") or [1.0]
    acc = {"wr": 0.0, "we": 0.0, "pr": 0.0, "pe": 0.0}
    k_win = k_plc = 0
    for r in ratios:
        row = tab.get(_band(d, o * r))
        if not row:
            continue
        if row.get("win_pay") is not None:
            acc["wr"] += row["win_rate"]
            acc["we"] += row["win_rate"] * row["win_pay"]
            k_win += 1
        if row.get("place_rate") is not None and row.get("place_pay") is not None:
            acc["pr"] += row["place_rate"]
            acc["pe"] += row["place_rate"] * row["place_pay"]
            k_plc += 1
    if not k_win:
        return None
    out = {"win_rate": acc["wr"] / k_win, "win_ev": acc["we"] / k_win}
    out["win_pay"] = out["win_ev"] / out["win_rate"] if out["win_rate"] else None
    if k_plc and n >= MIN_FIELD_PLACE:
        out["place_rate"] = acc["pr"] / k_plc
        out["place_ev"] = acc["pe"] / k_plc
        out["place_pay"] = out["place_ev"] / out["place_rate"] if out["place_rate"] else None
    else:
        out["place_rate"] = out["place_ev"] = out["place_pay"] = None
    return out


# ── 表示用の丸め (投稿・ダッシュボード・note で共通。ダッシュボードは下の fmt を使う) ──
def fmt_odds(o):
    return f"{o:.1f}" if o < 10 else f"{o:.0f}"


def fmt_yen(v):
    """的中時の平均払戻。200円未満は1円単位、それ以上は10円単位。"""
    if v is None:
        return "-"
    return f"{v:.0f}" if v < 200 else f"{int(round(v / 10) * 10):,}"


def fmt_ev(v):
    """期待値 (100円あたり) は5円単位。細かい数字は測定の精度を超える。"""
    return "-" if v is None else f"{int(5 * round(v / 5))}"


def fmt_rate(v):
    """10%未満は小数1桁 (4%×1,810円 のように掛け算が合わなく見えるのを防ぐ)。"""
    if v is None:
        return "-"
    p = 100 * v
    return f"{p:.0f}" if p >= 10 else f"{p:.1f}"


def line(odds, n_runners, mark="◎", minimal=False, race_name=None, tiny=False):
    """X 投稿用の1行。数字が出せなければ空文字 (呼び出し側は出さない)。

    標準: 💴◎想定19倍 複勝的中19%×平均420円→期待値約80円   (X加重 ~49)
    短縮: 💴◎複勝的中19%×420円→期待値80円                    (~34, minimal=True)
    最短: 💴複勝19%→期待値80円                                (~21, tiny=True。平均払戻は省く)
    短縮・最短は、印7行と ⚡/💡 が字数を争う時だけ使う。凡例が載らない日もあるので、
    どの形にも「複勝 (単勝)」と「期待値」の語を残す (#163 レビュー)。
    4頭以下は複勝が無いので単勝で出す。
    """
    e = expect(odds, n_runners, mark, race_name=race_name)
    if not e:
        return ""
    if e.get("place_rate"):
        kind, r, pay, ev = "複勝", e["place_rate"], e["place_pay"], e["place_ev"]
    else:
        kind, r, pay, ev = "単勝", e["win_rate"], e["win_pay"], e["win_ev"]
    if tiny:
        return f"💴{kind}{fmt_rate(r)}%→期待値{fmt_ev(ev)}円"
    if minimal:
        return f"💴{mark}{kind}的中{fmt_rate(r)}%×{fmt_yen(pay)}円→期待値{fmt_ev(ev)}円"
    return (f"💴{mark}想定{fmt_odds(float(odds))}倍 {kind}的中{fmt_rate(r)}%"
            f"×平均{fmt_yen(pay)}円→期待値約{fmt_ev(ev)}円")


def race_payload(horses, n_runners=None, final_odds=False, race_name=None, odds_label=None):
    """ダッシュボード用: 印馬ごとの実測。表が無い時だけ None。

    horses は mark / horse_number / horse_name / odds_win / popularity を持つ dict の列。
    popularity が 0 の馬は推定オッズ (発売前) なので出さない — 推定オッズは
    AI勝率から逆算した値で、市場の実測表に当てると循環する。
    出せる馬が1頭もない時は marks=[] と skip (理由) を返す (画面で理由を出すため)。
    odds_label: 画面に出すオッズの呼び方 (想定 / 現在 / 確定)。
    """
    d = load()
    if not d:
        return None
    n = n_runners or len(horses or [])
    label = odds_label or ("確定" if final_odds else "想定")
    base = {"marks": [], "period": d.get("period"), "since": since_label(),
            "n_races": d.get("n_races"), "final_odds": bool(final_odds), "odds_label": label}
    if layer_of(race_name) == "jump":
        return dict(base, skip="jump")
    order = {m: i for i, m in enumerate(MARK_ORDER)}
    marked = sorted((h for h in horses or [] if h.get("mark") in order),
                    key=lambda h: order[h["mark"]])
    if not any(h.get("mark") == "◎" for h in marked):
        return dict(base, skip="no_honmei")
    out = []
    for h in marked:
        o = h.get("odds_win") or 0
        if not (h.get("popularity") or 0) or not o:
            continue
        e = expect(o, n, h["mark"], final_odds=final_odds, race_name=race_name)
        if not e:
            continue
        out.append({
            "mark": h["mark"], "horse_number": h.get("horse_number"),
            "horse_name": h.get("horse_name"), "odds": o,
            "place_rate": _r(e["place_rate"], 4), "place_pay": _r(e["place_pay"], 1),
            "place_ev": _r(e["place_ev"], 1),
            "win_rate": _r(e["win_rate"], 4), "win_pay": _r(e["win_pay"], 1),
            "win_ev": _r(e["win_ev"], 1),
            # 丸めは Python 側で1回だけ (JS で丸め直すと X/Threads と数字がずれる — #163 レビュー)
            "fmt": {
                "odds": fmt_odds(float(o)),
                "place_rate": fmt_rate(e["place_rate"]), "place_pay": fmt_yen(e["place_pay"]),
                "place_ev": fmt_ev(e["place_ev"]),
                "win_rate": fmt_rate(e["win_rate"]), "win_pay": fmt_yen(e["win_pay"]),
                "win_ev": fmt_ev(e["win_ev"]),
            },
        })
    if not out:
        return dict(base, skip="no_odds")
    return dict(base, marks=out)


def _r(v, nd):
    return None if v is None else round(float(v), nd)
