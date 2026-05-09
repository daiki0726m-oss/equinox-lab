"""信頼度判定モジュール

予測結果から S/A/B/C/D の信頼度ラベルを返す共通ロジック。
predict.py / generate_note.py / 将来の他モジュールから利用される単一の Source of Truth。

設計:
  score = 本命勝率(%) × 均等比 + 上位3頭合計勝率(%) × 0.3
  - 絶対値・相対倍率・上位の固さ を全部反映

  重賞(G1/G2/G3): 混戦が前提 → 緩い閾値
    S>=30 / A>=22 / B>=16 / C>=10 / D<10

  平場(条件戦/未勝利/OP): 実力差が出やすい → 厳しい閾値
    S>=80 / A>=50 / B>=30 / C>=15 / D<15
"""

from typing import Iterable, Tuple, Optional


GRADED_THRESHOLDS = [(30, "S"), (22, "A"), (16, "B"), (10, "C")]
NORMAL_THRESHOLDS = [(80, "S"), (50, "A"), (30, "B"), (15, "C")]

GRADE_LABELS = {
    "S": "本命突出",
    "A": "軸馬有力",
    "B": "やや有力",
    "C": "標準",
    "D": "混戦",
}


def is_graded_race(grade: Optional[str]) -> bool:
    g = (grade or "").strip()
    return g in ("G1", "G2", "G3")


def compute_score(top_win_pct: float, n_horses: int, top3_sum_pct: float) -> Tuple[float, float]:
    """信頼度スコアと均等比を返す

    Args:
        top_win_pct: 本命の勝率(%)
        n_horses: 出走頭数
        top3_sum_pct: 上位3頭の勝率合計(%)
    Returns:
        (score, relative_ratio)
    """
    n = max(int(n_horses), 1)
    even_pct = 100.0 / n
    relative = top_win_pct / even_pct if even_pct > 0 else 0
    score = top_win_pct * relative + top3_sum_pct * 0.3
    return score, relative


def grade_from_score(score: float, is_graded: bool) -> str:
    """スコアから S/A/B/C/D ラベルを返す"""
    thresholds = GRADED_THRESHOLDS if is_graded else NORMAL_THRESHOLDS
    for thr, label in thresholds:
        if score >= thr:
            return label
    return "D"


def evaluate(
    top_win_pct: float,
    n_horses: int,
    top3_sum_pct: float,
    grade: Optional[str] = None,
) -> dict:
    """主要エントリポイント。

    Args:
        top_win_pct: 本命の勝率(%、0-100)
        n_horses: 出走頭数
        top3_sum_pct: 上位3頭の勝率合計(%、0-100)
        grade: レースグレード文字列('G1'/'G2'/'G3'/'OP'/'' 等)
    Returns:
        {
            'confidence': 'S'/'A'/'B'/'C'/'D',
            'score': float,
            'relative_ratio': float,  # 本命勝率 / 均等勝率
            'reason': str,  # 説明文
            'is_graded': bool,
        }
    """
    score, relative = compute_score(top_win_pct, n_horses, top3_sum_pct)
    is_graded = is_graded_race(grade)
    confidence = grade_from_score(score, is_graded)
    race_kind = "重賞" if is_graded else "平場"
    label = GRADE_LABELS[confidence]
    reason = (
        f"◎勝率{top_win_pct:.1f}%×均等比{relative:.2f} + 上位3計{top3_sum_pct:.1f}%"
        f" = {score:.1f} ({race_kind}基準) → {label}"
    )
    return {
        "confidence": confidence,
        "score": round(score, 1),
        "relative_ratio": round(relative, 2),
        "reason": reason,
        "is_graded": is_graded,
    }


def evaluate_from_horses(horses: Iterable[dict], grade: Optional[str] = None,
                         win_key: str = "pred_win_pct") -> dict:
    """horses(辞書のリスト)から自動的に top1, top3, n を計算して評価する

    Args:
        horses: 各馬の予測dict。win_key で勝率(%)を取れる前提
        grade: レースグレード
        win_key: 勝率フィールド名(predict.py内では 'pred_win'(0-1)を *100 する場合あり)
    """
    horses_list = list(horses) if horses else []
    if not horses_list:
        return evaluate(0, 1, 0, grade)
    sorted_h = sorted(horses_list, key=lambda h: h.get(win_key, 0), reverse=True)
    top1 = sorted_h[0].get(win_key, 0)
    top3_sum = sum(h.get(win_key, 0) for h in sorted_h[:3])
    return evaluate(top1, len(horses_list), top3_sum, grade)
