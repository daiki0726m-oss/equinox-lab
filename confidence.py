"""信頼度判定モジュール (v2: 6軸合成スコア)

予測結果から S/A/B/C/D の信頼度ラベルを返す共通ロジック。
predict.py / generate_note.py / 将来の他モジュールから利用される
single source of truth。

## 設計思想 (v2 — 旧 grade-bifurcated logic を破棄)

「信頼度 = AI が ◎(本命)を当てる(複勝圏内に入る)確信度」と定義。
6軸の独立シグナルを 0-1 にnormalize し、重み付き合計で 0-1 の
composite score を算出 → 5段階に bucket 化する。

旧 v1 では「重賞=緩い閾値、平場=厳しい閾値」と grade で二分していた
ため、G1 の薄い予測(◎勝率15%)が S、平場の濃い予測(◎勝率36%)が A
という直感に反する反転が起きていた。v2 では絶対値ベースの統一スコアに。

## 6軸シグナル

| 軸 | 重み | 説明 | 満点条件 |
|---|---|---|---|
| win_score | 0.22 | ◎の予測勝率(%) | 35%以上 |
| top3_score | 0.20 | ◎の予測複勝率(%) | 70%以上 |
| gap_score | 0.18 | ◎vs○の勝率gap(%) | 12pt以上 |
| conc_score | 0.15 | 上位3頭の勝率合計(%) | 75%以上 |
| pop_score | 0.10 | ◎の人気(市場との一致) | 1人気=1.0 |
| size_score | 0.15 | 頭数(少ないほど有利) | 8頭以下 |

重賞(G1/G2/G3)は混戦が前提なので合計から **-0.03** の補正。

## ラベル化 (composite 0-1)

| composite | rating | 意味 |
|---|---|---|
| >= 0.72 | S | ◎複勝圏入り highly likely (推定70%+) |
| >= 0.58 | A | ◎複勝圏入り likely (50-70%) |
| >= 0.44 | B | ◎複勝圏入り decent (30-50%) |
| >= 0.30 | C | ◎複勝圏入り uncertain (15-30%) |
| < 0.30  | D | 全シグナル弱 — 見送り推奨 |
"""

from typing import Iterable, Tuple, Optional


# ── 6軸の重み (sum = 1.0) ──
# v3 (2026-05-16): 過去 470R 分析で「S が A より低い」現象を発見。
# 根本原因は「AI 過信 + 少頭数バイアス + 人気外馬を高評価しすぎ」。
# 信頼度ラベルを「AI と市場の一致度 = 堅さ」に再定義し、市場シグナルを重視。
# (◎自体は Contrarian で人気外も拾うが、信頼度ラベルは「堅さ」を示す)
WEIGHTS = {
    'win': 0.15,    # ◎の予測勝率 (AI 過信を抑える: 0.22→0.15)
    'top3': 0.18,   # ◎の予測複勝率 (やや下げ)
    'gap': 0.15,    # ◎vs○のgap (やや下げ)
    'conc': 0.10,   # 上位3頭の合計勝率 (やや下げ)
    'pop': 0.30,    # ◎の市場人気 (大幅 UP: 0.10→0.30、AI と市場の一致度を重視)
    'size': 0.12,   # 頭数 (少頭数バイアスを弱める: 0.15→0.12)
}

# 各シグナルの満点(満点=1.0となる絶対値)
# v7 (2026-05-16): ML popularity 弱化 + retrain で AI 勝率が圧縮された結果、
# 旧 NORMS (35/70/12/75) では新モデル予測で S/A が全て消える事態に。
# 新モデルの分布(◎勝率上限~18%、gap~4pt)に合わせて約半分に再キャリブレーション。
NORMS = {
    'win': 18.0,    # 勝率18%で満点 (新モデルの実上限に対応)
    'top3': 40.0,   # 複勝率40%で満点
    'gap': 5.0,     # 5pt差で満点 (新モデルでは差が圧縮)
    'conc': 50.0,   # top3合計50%で満点
}

# 重賞補正(混戦前提)
GRADED_PENALTY = 0.03

# composite → rating の閾値
RATING_THRESHOLDS = [
    (0.72, "S"),
    (0.58, "A"),
    (0.44, "B"),
    (0.30, "C"),
]
DEFAULT_RATING = "D"

GRADE_LABELS = {
    "S": "本命突出",
    "A": "軸馬有力",
    "B": "やや有力",
    "C": "標準",
    "D": "混戦",
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _is_graded(grade: Optional[str]) -> bool:
    g = (grade or "").strip()
    return g in ("G1", "G2", "G3")


def _pop_score(pop: Optional[int]) -> float:
    """人気→pop_score (◎が1人気なら市場と一致 = 信頼度↑)。
    人気不明(0/None/負の値) は中庸 0.5 を返す(ペナルティしない)。
    """
    if pop is None or pop <= 0:
        return 0.5
    table = {1: 1.0, 2: 0.85, 3: 0.7, 4: 0.55, 5: 0.45, 6: 0.4}
    return table.get(pop, 0.25)


def _size_score(n: int) -> float:
    """頭数→size_score (少頭数ほど予想しやすい)。
    8頭以下=満点、18頭=0.5、それ以上は 0.4 で下限。
    """
    return _clamp(1.0 - max(0, n - 8) * 0.05, lo=0.4, hi=1.0)


def compute_composite(
    top1_win: float,
    top2_win: float,
    top1_top3: float,
    top3_sum: float,
    n_horses: int,
    top1_popularity: Optional[int],
    grade: Optional[str] = None,
) -> dict:
    """6軸合成スコアを計算して返す。

    Args:
        top1_win: ◎(予測トップ)の勝率(%、0-100)
        top2_win: ○(予測2位)の勝率(%、0-100)
        top1_top3: ◎の複勝率(%、0-100)
        top3_sum: 上位3頭の勝率合計(%、0-100)
        n_horses: 出走頭数
        top1_popularity: ◎の市場人気(1=1番人気)
        grade: レースグレード('G1'/'G2'/'G3'/'OP'/...)

    Returns:
        {'composite': float, 'breakdown': {...軸別 0-1 score}, 'is_graded': bool}
    """
    n = max(int(n_horses), 1)

    breakdown = {
        'win': _clamp(top1_win / NORMS['win']),
        'top3': _clamp(top1_top3 / NORMS['top3']),
        'gap': _clamp((top1_win - top2_win) / NORMS['gap']),
        'conc': _clamp(top3_sum / NORMS['conc']),
        'pop': _pop_score(top1_popularity),
        'size': _size_score(n),
    }

    composite = sum(WEIGHTS[k] * breakdown[k] for k in WEIGHTS)

    is_graded = _is_graded(grade)
    if is_graded:
        composite -= GRADED_PENALTY

    return {
        'composite': _clamp(composite, lo=0.0, hi=1.0),
        'breakdown': breakdown,
        'is_graded': is_graded,
    }


def grade_from_composite(composite: float) -> str:
    """composite (0-1) → S/A/B/C/D ラベル"""
    for thr, label in RATING_THRESHOLDS:
        if composite >= thr:
            return label
    return DEFAULT_RATING


def evaluate(
    top_win_pct: float,
    n_horses: int,
    top3_sum_pct: float,
    grade: Optional[str] = None,
    *,
    second_win_pct: Optional[float] = None,
    top_top3_pct: Optional[float] = None,
    top_popularity: Optional[int] = None,
) -> dict:
    """主要エントリポイント。互換のため第1〜4引数は v1 と同じシグネチャ。

    新しい3軸を活かすには second_win_pct / top_top3_pct / top_popularity を
    キーワード引数で渡すこと。省略時は中庸推定で計算する(下記)。

    Args:
        top_win_pct: ◎の勝率(%、0-100)
        n_horses: 出走頭数
        top3_sum_pct: 上位3頭の勝率合計(%、0-100)
        grade: レースグレード
        second_win_pct: ○(予測2位)の勝率(%、省略時は top3_sum_pct から推定)
        top_top3_pct: ◎の複勝率(%、省略時は top_win_pct*2.2 で推定)
        top_popularity: ◎の市場人気(1〜N、省略時は中庸 0.5扱い=None)

    Returns:
        {'confidence': 'S'/'A'/'B'/'C'/'D',
         'score': float (0-1, composite),
         'reason': str, 'is_graded': bool, 'breakdown': {...}}
    """
    # 省略値の推定
    if second_win_pct is None:
        # top3_sum から ◎ を引いて 2/3 が ○ と仮定
        rest = max(0.0, top3_sum_pct - top_win_pct)
        second_win_pct = rest * 0.6
    if top_top3_pct is None:
        # 経験則: ◎複勝率 ≒ 勝率 × 2.0 ~ 2.5
        top_top3_pct = min(95.0, top_win_pct * 2.2)

    result = compute_composite(
        top1_win=top_win_pct,
        top2_win=second_win_pct,
        top1_top3=top_top3_pct,
        top3_sum=top3_sum_pct,
        n_horses=n_horses,
        top1_popularity=top_popularity,
        grade=grade,
    )
    rating = grade_from_composite(result['composite'])
    label = GRADE_LABELS[rating]
    br = result['breakdown']
    reason = (
        f"◎勝率{top_win_pct:.1f}% / 複勝{top_top3_pct:.1f}% / "
        f"gap{(top_win_pct - second_win_pct):.1f}pt / "
        f"上位3計{top3_sum_pct:.1f}% / "
        f"人気{top_popularity if top_popularity else '?'} / "
        f"{n_horses}頭 → {result['composite']:.2f} ({label})"
    )
    return {
        'confidence': rating,
        'score': round(result['composite'], 3),
        'reason': reason,
        'is_graded': result['is_graded'],
        'breakdown': {k: round(v, 3) for k, v in br.items()},
    }


def evaluate_from_horses(
    horses: Iterable[dict],
    grade: Optional[str] = None,
    win_key: str = "pred_win_pct",
    top3_key: str = "pred_top3_pct",
    pop_key: str = "popularity",
) -> dict:
    """horses(辞書のリスト)から自動的に top1/top2/top3 を計算して評価する。

    Args:
        horses: 各馬の予測dict。win_key/top3_key/pop_key を持つ前提
        grade: レースグレード
        win_key: 勝率フィールド名
        top3_key: 複勝率フィールド名
        pop_key: 市場人気フィールド名
    """
    horses_list = list(horses) if horses else []
    if not horses_list:
        return evaluate(0.0, 1, 0.0, grade)

    sorted_h = sorted(
        horses_list,
        key=lambda h: float(h.get(win_key, 0) or 0),
        reverse=True,
    )
    top1 = sorted_h[0]
    top2 = sorted_h[1] if len(sorted_h) >= 2 else top1

    top1_win = float(top1.get(win_key, 0) or 0)
    top2_win = float(top2.get(win_key, 0) or 0)
    top1_top3 = float(top1.get(top3_key, 0) or 0)
    top3_sum = sum(float(h.get(win_key, 0) or 0) for h in sorted_h[:3])

    pop = top1.get(pop_key, 0)
    try:
        pop = int(pop)
    except (TypeError, ValueError):
        pop = 0

    return evaluate(
        top_win_pct=top1_win,
        n_horses=len(horses_list),
        top3_sum_pct=top3_sum,
        grade=grade,
        second_win_pct=top2_win,
        top_top3_pct=top1_top3,
        top_popularity=pop if pop > 0 else None,
    )


# ─── 後方互換 (旧 API) ───
def is_graded_race(grade: Optional[str]) -> bool:
    """v1 互換ヘルパー"""
    return _is_graded(grade)


def compute_score(top_win_pct: float, n_horses: int, top3_sum_pct: float) -> Tuple[float, float]:
    """v1 互換: 旧 score (0-200程度) を擬似的に再現する。

    新ロジックでは 6軸 composite(0-1) が真のスコアだが、外部から
    旧 (score, relative_ratio) を期待されている呼び出しもあるため、
    composite を 100倍した値と均等比を返す。
    """
    n = max(int(n_horses), 1)
    even_pct = 100.0 / n
    relative = top_win_pct / even_pct if even_pct > 0 else 0.0
    # composite を簡易計算(top2/top3pct/popularity を省略するため精度低)
    rough = compute_composite(
        top1_win=top_win_pct,
        top2_win=max(0.0, top3_sum_pct - top_win_pct) * 0.6,
        top1_top3=min(95.0, top_win_pct * 2.2),
        top3_sum=top3_sum_pct,
        n_horses=n,
        top1_popularity=None,
        grade=None,
    )
    return rough['composite'] * 100.0, relative


def grade_from_score(score: float, is_graded: bool) -> str:
    """v1 互換: score(0-100) → ラベル。

    score は compute_score の戻り値前提。100倍 composite なので
    閾値は RATING_THRESHOLDS * 100 で再計算。
    """
    composite = score / 100.0
    if is_graded:
        composite -= GRADED_PENALTY
    return grade_from_composite(composite)
