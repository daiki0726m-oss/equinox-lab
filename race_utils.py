"""レース種別の共通判定 (#123 2026-08-24)

障害レースの判定が「race_name に '障害' か 'ジャンプ' を含むか」の文字列チェックで
各所に散らばっていたため、**「新潟JS」「東京JS」等のジャンプステークス 73レースが
すべて素通り**していた (JRA の障害重賞・OP は "○○JS" 表記)。障害は ML の学習データ外
(#95) で S 評価が付くと最大額が張られるため、投資ガードの穴として実害がある。
判定を1箇所に集約し、全ての呼び出し元がこれを使う。
"""

_JUMP_WORDS = ("障害", "ジャンプ")


def is_jump_race(race_name, distance=0, surface=""):
    """障害 (ジャンプ) レースか。

    - "障害" / "ジャンプ" を含む   … 未勝利・OP クラス戦、中山大障害、中山グランドジャンプ
    - 名前が "JS" で終わる          … 新潟JS / 東京JS / イルミネーションJS 等 (73レース)
      ただし "YJS" (ヤングジョッキーズシリーズ) は平地なので除外する
    """
    name = (race_name or "").strip()
    if not name:
        return False
    if any(w in name for w in _JUMP_WORDS):
        return True
    if name.endswith("JS") and not name.endswith("YJS") and "YJS" not in name:
        return True
    return False


def is_ml_out_of_domain(race_name):
    """ML の学習が効かない層 (未勝利・新馬・障害) — 投資見送りの共通条件 (#28/#95)。"""
    name = (race_name or "")
    return ("未勝利" in name) or ("新馬" in name) or is_jump_race(name)
