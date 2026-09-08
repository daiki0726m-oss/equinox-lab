"""レース条件別の実測値を読む単一情報源 (#151)

`docs/data/race_baseline.json` (scripts/build_race_baseline.py が生成) を読み、
読者に出す「実測値」と、内部で使う信頼度の閾値を提供する。

なぜラベルでなく実測値を出すのか
--------------------------------
「信頼度S(鉄板級)」というラベルは、検証の結果
  - ◎を選ぶモデル (点数表) と連動しておらず (◎がAI勝率1位なのは42%)
  - 1番人気オッズ+頭数+クラスに足しても予測力の増分は AUC +0.0009 (p=0.455)
だった。ラベルの言い換えでなく、**そのレース条件で実際に何が起きたか**を
そのまま出す方が、印を見て自分で買い目を組む読者の役に立つ。

出す数字はすべて過去の実測であって、このレースの予言ではない。
"""
import json
import os
import threading

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "docs", "data", "race_baseline.json")
_LOCK = threading.Lock()
_CACHE = {"mtime": None, "data": None}

# 基準表が無い/壊れている時の従来値 (#36/#120 で使っていた固定閾値)
FALLBACK_THRESHOLDS = {"S": 45.0, "A": 30.0, "B": 20.0, "C": 13.0}


def load(path=None):
    """基準表を読む。ファイルの更新時刻が変わったら読み直す。"""
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


def _group_of(race_name):
    """平場 か 未勝利・新馬・障害 か。両者は実測値がはっきり違う
    (15-16頭の捕捉率: 平場 20.9% vs 未勝利等 36.4%) ので必ず分けて引く。"""
    try:
        from race_utils import is_ml_out_of_domain
        return "maiden" if is_ml_out_of_domain(race_name or "") else "flat"
    except Exception:
        return "flat"


def _bucket(section, n_horses, race_name=None):
    d = load()
    if not d or not n_horses:
        return None
    sec = d.get(section) or {}
    groups = sec.get("groups")
    buckets = (groups or {}).get(_group_of(race_name), {}).get("buckets") \
        if groups else sec.get("buckets")
    for b in buckets or []:
        if b.get("min", 0) <= n_horses <= b.get("max", 99):
            return b
    return None


def stats(n_horses, race_name=None):
    """この頭数・この層の実測値をまとめて返す。数字が揃わなければ None。

    返り値の例:
      {'label':'15-16頭', 'n':182, 'cap5':0.209, 'cap7':0.247,
       'trio_median':9150, 'trio_over10k':0.477}
    """
    cb = _bucket("capture", n_horses, race_name)
    pb = _bucket("payout", n_horses, race_name)
    if not cb or "cap5" not in cb:
        return None
    out = {"label": cb["label"], "n": cb["n"], "cap5": cb["cap5"],
           "cap7": cb.get("cap7"), "avg_captured": cb.get("avg_captured")}
    if pb and "trio_median" in pb:
        out["trio_median"] = pb["trio_median"]
        out["trio_over10k"] = pb.get("trio_over10k")
        out["payout_n"] = pb.get("n")
    return out


def line(n_horses, compact=True, with_payout=True, race_name=None):
    """読者向けの1行。数字が無ければ空文字 (呼び出し側は出さない)。

    compact=True  : X 投稿用 (短い)
    compact=False : Threads / ダッシュボード用 (母数と印7頭も添える)
    with_payout   : 三連複の配当を含めるか (compact 時に字数が足りない場合 False で呼ぶ)
    """
    s = stats(n_horses, race_name)
    if not s:
        return ""
    cap5 = f"{100 * s['cap5']:.0f}%"
    if compact:
        # X は280字 (全角2) の予算が厳しく、印7行と競合する。
        # 配当は「小頭数=当てやすいが安い / 多頭数=難しいが大きい」という
        # トレードオフの片翼なので入れたいが、入らない時は捕捉率を優先する。
        t = f"📐{n_horses}頭 印5頭で3着内が揃う実測{cap5}"
        if with_payout and s.get("trio_median"):
            t += f"・三連複中央値{s['trio_median']:,}円"
        return t
    t = (f"📐 この条件（{s['label']}）の実測\n"
         f"・印5頭で3着内3頭が全部揃う: {cap5}")
    if s.get("cap7") is not None:
        t += f"（穴2枠も入れた7頭なら{100 * s['cap7']:.0f}%）"
    t += f" ※過去{s['n']}レース"
    if s.get("trio_median"):
        t += (f"\n・三連複の配当: 中央値{s['trio_median']:,}円"
              f"／1万円超が{100 * s['trio_over10k']:.0f}%")
    return t


def advice(n_horses, race_name=None):
    """実測値から導く一言。数値そのものが言っていること以上は足さない。"""
    s = stats(n_horses, race_name)
    if not s:
        return ""
    c5 = s["cap5"]
    if c5 >= 0.35:
        return "印の中で決まりやすい条件。点数を絞る側"
    if c5 >= 0.22:
        return "印だけでは半端に届かないことが多い条件"
    return "印5頭では届きにくい条件。手広く取るか見送る側"


def confidence_thresholds():
    """信頼度の閾値。基準表が無ければ従来の固定値。

    固定値のままだと、印ロジックやモデルが変わった瞬間に分布とペアが外れる
    (#36/#120/#147 で三度再発)。基準表側で分位から引き直す。
    """
    d = load()
    cf = (d or {}).get("confidence") or {}
    th = cf.get("thresholds")
    if cf.get("available") and isinstance(th, dict) and all(k in th for k in "SABC"):
        return {k: float(th[k]) for k in "SABC"}
    return dict(FALLBACK_THRESHOLDS)


def grade(win_pct):
    """◎の表示勝率 → S/A/B/C/D。閾値は基準表から。"""
    th = confidence_thresholds()
    w = float(win_pct or 0)
    if w >= th["S"]:
        return "S"
    if w >= th["A"]:
        return "A"
    if w >= th["B"]:
        return "B"
    if w >= th["C"]:
        return "C"
    return "D"
