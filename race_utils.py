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
    - 名前が "J" で終わる           … 中山グランドJ / 阪神スプリングJ / 東京HJ / 小倉サマーJ /
      京都ハイJ / 阪神SJ 等の障害重賞 (#163 のレビューで発見、8名称26レースが平地扱いだった)。
      DB 上 "J" で終わるレース名は全て 3,110m 以上の障害で、平地は1件も無い。
    """
    name = (race_name or "").strip()
    if not name:
        return False
    if any(w in name for w in _JUMP_WORDS):
        return True
    if name.endswith("JS") and not name.endswith("YJS") and "YJS" not in name:
        return True
    if name.endswith("J"):
        return True
    return False


def is_ml_out_of_domain(race_name):
    """ML の学習が効かない層 (未勝利・新馬・障害) — 投資見送りの共通条件 (#28/#95)。"""
    name = (race_name or "")
    return ("未勝利" in name) or ("新馬" in name) or is_jump_race(name)


# ── 出走取消の判定 (#170) ─────────────────────────────────────
# #169 で取消馬を印・点数表・💡能力値1位・Threads の能力値ランキングから外したが、
# 判定が呼び出し元ごとの書き下し (scratched / 999.9倍 / 9999人気) で、推奨理由の
# 「能力評価N位」や Threads の「M頭中K位」は取消馬を順位の母集団に残したままだった
# (11/01 東京7R: 取消11番が能力1位のため全馬の能力順位が1つずつずれ、母数も「15頭中」)。
# 値の判定 (predict._is_scratch_odds と同じ) と馬1頭の判定をここに1つだけ置く。
SCRATCH_ODDS = 999.9    # JRA の表示上限。取消の目印に書く値 (SCRATCH_SENTINEL) の片方
SCRATCH_POP = 999       # 人気がこれ以上 = 取消 (オッズ API の番兵 9999 / 出馬表の取消行で書く 9999)
# #171: 取消と分かった馬 (出馬表の取消行・API の登録頭数>出走頭数) に results へ書く値。
# 999.9倍だけでは取消と区別できない (前日発売の直後は票が少なく、走る馬も 999.9倍になる。
# 11/06 19:05 の前夜予想で、11/07 の28レースに計94頭の「取消」が出た。実際の取消は0頭)。
# 取消の目印は人気 9999 の方で、オッズ 999.9 は LGB の特徴量を「最も人気薄」に保つための値。
SCRATCH_SENTINEL = (999.9, 9999)


def is_scratch_values(odds, popularity):
    """オッズ・人気の値が「出走取消」をあらわすか (#169 predict._is_scratch_odds の本体)。

    odds<0 (API の番兵 -3.0) / 人気>=999 (API の番兵 9999・出馬表の取消行で書く SCRATCH_SENTINEL)。
    変換できない値は 0 扱い (= 取消ではない)。
    #171: 「999.9倍以上」は取消の判定に使わない。999.9倍は票がほとんど無い馬の表示で、
    前日発売の直後や薄い市場では走る馬も普通にこの値になる (11/06 19:05 の前夜予想で
    11/07 の28レース・94頭を誤って取消扱いにし、印の候補から外した)。発売中の API は
    取消馬も 999.9倍・人気=頭数で返すので、値だけでは区別できない。取消は出馬表の取消行
    (scraper.scrape_shutuba / refresh_odds の取消照合) で確かめて SCRATCH_SENTINEL を書く。
    """
    try:
        o = float(odds or 0)
    except (TypeError, ValueError):
        o = 0.0
    try:
        pop = int(popularity or 0)
    except (TypeError, ValueError):
        pop = 0
    return o < 0 or pop >= SCRATCH_POP


def is_scratched_horse(h):
    """馬1頭 (predict の予測 dict / cache・公開 JSON の馬) が取消と分かっているか (#170)。

    - フラグ: cache・公開 JSON の "scratched"、predict 内部の "_scratched"
    - 値: odds_win (無ければ odds) と popularity を is_scratch_values で見る
      (予測の後に分かった取消 = refresh_odds が書いた番兵 9999人気 もここで拾う。#171 で
       999.9倍だけでは取消にしない)
    - predict の dict で _has_real_odds=False の馬は odds_win が推定値 (0.8/勝率) なので
      オッズを見ない (predict の印の段 #169 と同じ扱い)
    """
    if not h:
        return False
    if h.get("scratched") or h.get("_scratched"):
        return True
    odds = h.get("odds_win") if h.get("odds_win") is not None else h.get("odds")
    if "_has_real_odds" in h and not h.get("_has_real_odds"):
        odds = 0
    return is_scratch_values(odds, h.get("popularity"))


def running_horses(horses):
    """順位・頭数の母集団 = 取消と分かっている馬を除いた出走馬 (#170)。"""
    return [h for h in (horses or []) if not is_scratched_horse(h)]


def field_popularities(horses):
    """出走馬の中での人気順 {馬番: 人気} (#170)。running_horses で数えた AI の順位と比べる市場側。

    popularity は予測時点の値で、取消馬を含めて付いていることがある。DB の取消馬オッズが番兵
    -3.0 で API の人気も無い日 (8月以前の番兵時代) は、推定オッズで全頭を並べ直すので取消馬が
    中位に入る。AI の順位だけ出走馬で数えると、取消馬より人気が下の馬は AI の順位だけ繰り上がり、
    存在しない「乖離」が出る (202604030109: 取消2頭が9・10人気で、11人気以下の5頭すべてに
    「AIの複勝評価は13頭中N位（市場は想定N+2番人気）」が出た)。
    人気から「それより人気が上の取消馬の数」を引く。取消が無い・取消馬が最下位 (人気=頭数 /
    番兵 9999人気 = 現行) なら popularity の値のまま、同人気は同人気のまま、0 (未確定) は 0。
    取消馬はキーに含めない。cache・公開 JSON の popularity は書き換えない (投稿側が 9999 で判定する)。
    """
    def _p(h):
        try:
            return int(h.get("popularity") or 0)
        except (TypeError, ValueError):
            return 0
    hs = horses or []
    scr = [p for p in (_p(h) for h in hs if is_scratched_horse(h)) if p > 0]
    out = {}
    for h in hs:
        if is_scratched_horse(h):
            continue
        p = _p(h)
        ahead = sum(1 for s in scr if s < p) if p > 0 else 0
        # 引く数が無ければ元の値をそのまま返す (通常の経路は値も型も変えない)
        out[h.get("horse_number")] = (p - ahead) if ahead else (h.get("popularity") or 0)
    return out
