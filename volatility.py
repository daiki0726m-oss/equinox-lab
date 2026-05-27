"""人気薄(穴)を見抜くためのスコアリングモジュール v1 (2026-05-17)

過去 11265 レースの統計分析 (popularity >= 10 が3着内に絡む確率) を元に、
以下3つのスコアを計算する:

- race_volatility: レース全体の「荒れやすさ」(コース・頭数・クラス等)
- anasanee_score: 個別馬の「穴予兆」(前走凡走・距離変更・脚質等)
- jockey_trust: 騎手別の上位人気信頼度補正

集計の根拠は CLAUDE.md とコミット履歴参照。
"""
from __future__ import annotations
from typing import Dict, List, Optional
from database import get_db


# ───────────────────────────────────────────────
# 定数: 過去 11265R 分析結果から抽出
# ───────────────────────────────────────────────

# 穴出し率 (10+人気の3着内率) が高い騎手 (ベース 5.16% に対して 2倍前後)
JOCKEY_ANA_BONUS = {
    "秋山": 2, "黒岩": 2, "小野寺": 2, "藤岡佑": 2, "大江圭": 2,
    "横山武": 2, "西村淳": 2, "戸崎圭": 2, "浜中": 1, "草野": 1,
}

# 1-3人気での3着内率が高い "信頼" 騎手 (ベース 53% に対して 60%+)
JOCKEY_TOP_TRUST_HIGH = {
    "モレイラ": 3, "ルメール": 2, "川田": 2, "福永": 2,
    "池添": 1, "レーン": 1, "高田": 1, "森一": 1,
}

# 1-3人気で飛ばしやすい騎手 (ベース 53% に対して 45%以下)
JOCKEY_TOP_TRUST_LOW = {
    "藤田菜": -2, "永島": -2, "小沢": -2, "北村宏": -1,
    "斎藤": -1, "Ｍデムーロ": -1, "藤岡康": -1,
}

# 穴を出しやすいサイアー (10+人気の3着内率がベース 5.16% の 1.7倍以上)
SIRE_ANA_BONUS = {
    "ドレフォン": 2, "シニスターミニスター": 2, "ロードカナロア": 2,
    "ドゥラメンテ": 2, "カリフォルニアクローム": 2,
    "イスラボニータ": 1, "ミッキーアイル": 1, "トーセンラー": 1,
}


# ───────────────────────────────────────────────
# A. race_volatility (レース全体の波乱度)
# ───────────────────────────────────────────────

def compute_race_volatility(race_info: dict) -> dict:
    """レース情報から波乱度スコアを計算。

    返り値: {score: int, factors: list[str], conf_adjust: int}
    - score: 0-10 程度
    - conf_adjust: 信頼度に対する補正 (負なら下げる、正なら上げる)
    """
    score = 0
    factors = []

    n = race_info.get("horse_count", 0) or 0
    if n >= 16:
        score += 3
        factors.append(f"{n}頭立て(+3)")
    elif n <= 8:
        score -= 2
        factors.append(f"{n}頭立て(-2)")

    grade = (race_info.get("grade") or "").upper()
    if grade in ("G1", "G2", "G3"):
        score += 3
        factors.append(f"{grade}重賞(+3)")

    dist = race_info.get("distance", 0) or 0
    surf = race_info.get("surface", "")
    if surf == "芝" and dist <= 1200:
        score += 2
        factors.append("芝1200m以下(+2)")
    elif surf == "芝" and dist >= 2500:
        score += 1
        factors.append(f"芝{dist}m長距離(+1)")

    venue = race_info.get("venue", "")
    if venue in ("福島", "小倉", "新潟"):
        score += 1
        factors.append(f"{venue}ローカル(+1)")
    elif venue in ("札幌", "阪神"):
        score -= 1
        factors.append(f"{venue}堅め(-1)")

    cond = race_info.get("track_condition", "")
    if cond in ("不", "不良"):
        score += 1
        factors.append("不良馬場(+1)")

    # スコア → 信頼度補正
    # 注: -2 にすると G1+18頭で D まで落ち、本命堅実決着と乖離するため最大 -1。
    # 少頭数は「構造的に穴がない」だけで本命が来やすい訳ではないので格上げしない。
    if score >= 4:
        conf_adjust = -1  # 1段階下げ
    else:
        conf_adjust = 0

    # ── race_class 補正 (2026-05-27) ───────────────
    # 5/9-5/25 backtest の cross-tab (race_class × confidence × 三連複◎軸 ROI):
    #   未勝利・新馬: S=39% / A=38% / B=55% / C=64% / D=77% ← 全層 loss layer + 逆転現象
    #   1勝クラス:    A=189% / B=24% / C=38% ← 期待通り A 高 ROI
    #   2勝クラス:    A=234% ← 小サンプル
    #   OP/特別:     S=154% / B=182% ← 機能してる
    #
    # 未勝利・新馬は ML が unknown horse をうまく学習できておらず、confidence の予測力
    # が逆転している (高 confidence ほど ROI 低い)。1勝以上では機能してるので
    # confidence 軸そのものは正しい。
    #
    # → 未勝利・新馬は -2 段 (S→B, A→C) で過大投資を防ぐ。
    #    実質的に未勝利戦の bet weighting (S=200円/A=150円) を発動しない設計。
    name = race_info.get("race_name", "") or ""
    if "未勝利" in name or "新馬" in name:
        conf_adjust += -2
        factors.append("未勝利/新馬(-2)")

    # ── 1勝クラス × loss layer 補正 (2026-05-27 #30) ───────────────
    # 2025 historical backtest 分析結果 (1勝×A の 128 races, ROI 24.9%):
    #   ダート: 15.5% (n=77) / 芝: 39.0% (n=51) ← ダートが loss 主犯
    #   阪神: 5.2% / 中京: 11.9% / 札幌: 0% / 福島: 15.7% (地方場で低 ROI)
    #   短距離 (~1400m): 11.4% (n=26) ← 短距離も loss
    # 1勝戦は ML model の精度がクラスで急に落ちる傾向あり (馬の素性データが少ない)
    # → 「1勝 × 上記条件」のうち 1 つでも該当すれば -1、2 つ以上で -2 補正
    # 注意: 上位クラス (2勝以上、OP/特別、G1-G3) は ML が機能するので適用しない
    if "1勝" in name:
        loss_signals = 0
        loss_factors = []
        if surf == "ダート":
            loss_signals += 1
            loss_factors.append("ダート")
        if venue in ("阪神", "中京", "札幌"):
            loss_signals += 1
            loss_factors.append(f"{venue}場")
        if dist > 0 and dist <= 1400:
            loss_signals += 1
            loss_factors.append("短距離")
        if loss_signals > 0:
            penalty = -min(loss_signals, 2)  # 最大 -2 でキャップ
            conf_adjust += penalty
            factors.append(f"1勝×{'+'.join(loss_factors)}({penalty})")

    return {"score": score, "factors": factors, "conf_adjust": conf_adjust}


# ───────────────────────────────────────────────
# B. anasanee_score (個別馬の穴予兆)
# ───────────────────────────────────────────────

def fetch_horse_history(race_id: str, horse_numbers: List[int]) -> Dict[int, dict]:
    """各馬の前走情報・血統・斤量変化をバッチ取得。

    返り値: {horse_number: {prev_pos, dist_diff, prev_style, weight_change,
                            sire, jk_changed, days_since_prev}}
    """
    out: Dict[int, dict] = {}
    if not horse_numbers:
        return out

    with get_db() as conn:
        # 今回レースの情報
        cur = conn.execute(
            "SELECT distance, surface, race_date FROM races WHERE race_id = ?",
            (race_id,),
        ).fetchone()
        if not cur:
            return out
        cur_dist = cur["distance"]
        cur_date = cur["race_date"]

        # 今回出走馬の horse_id と current jockey/weight_change を取得
        cur_runners = conn.execute(
            """SELECT res.horse_number, res.horse_id, res.jockey_id,
                      res.weight_change, h.sire
               FROM results res LEFT JOIN horses h ON res.horse_id = h.horse_id
               WHERE res.race_id = ? AND res.horse_number IN ({})""".format(
                ",".join(["?"] * len(horse_numbers))
            ),
            [race_id] + list(horse_numbers),
        ).fetchall()

        for r in cur_runners:
            hn = r["horse_number"]
            horse_id = r["horse_id"]
            cur_jk = r["jockey_id"]
            wc = r["weight_change"]
            sire = r["sire"] or ""

            # 前走情報 (距離/着順/騎手は直近)
            prev = conn.execute(
                """SELECT res2.finish_position, res2.jockey_id, res2.passing_order,
                          r2.distance, r2.race_date, r2.horse_count
                   FROM results res2 JOIN races r2 ON res2.race_id = r2.race_id
                   WHERE res2.horse_id = ? AND r2.race_date < ?
                   ORDER BY r2.race_date DESC LIMIT 1""",
                (horse_id, cur_date),
            ).fetchone()

            # 脚質判定用に passing_order が入っている最直近の前走を別途取得
            # (直近前走で passing_order が NULL のケースが頻発するため)
            prev_style_row = conn.execute(
                """SELECT res2.passing_order, r2.horse_count
                   FROM results res2 JOIN races r2 ON res2.race_id = r2.race_id
                   WHERE res2.horse_id = ? AND r2.race_date < ?
                     AND res2.passing_order IS NOT NULL AND res2.passing_order != ''
                   ORDER BY r2.race_date DESC LIMIT 1""",
                (horse_id, cur_date),
            ).fetchone()

            if prev:
                prev_pos = prev["finish_position"]
                dist_diff = cur_dist - prev["distance"] if prev["distance"] else 0
                jk_changed = (cur_jk != prev["jockey_id"]) if cur_jk and prev["jockey_id"] else False

                # 前走脚質 (passing_order の最初の数字 / 頭数で正規化)
                # 直近前走で passing_order が空のケースが多いため、別取得した
                # prev_style_row (passing_order あり) を使う。
                style = None
                po = prev_style_row["passing_order"] if prev_style_row else None
                hc_style = prev_style_row["horse_count"] if prev_style_row else None
                if po and "-" in po:
                    try:
                        first_corner = int(po.split("-")[0])
                        hc = hc_style or 10
                        ratio = first_corner / hc
                        if first_corner == 1:
                            style = "逃げ"
                        elif first_corner <= 4:
                            style = "先行"
                        elif ratio <= 0.5:
                            style = "中団前"
                        elif ratio <= 0.8:
                            style = "差し"
                        else:
                            style = "追込"
                    except (ValueError, ZeroDivisionError):
                        pass

                # 中休み (日数差)
                try:
                    from datetime import datetime
                    d1 = datetime.strptime(cur_date, "%Y-%m-%d")
                    d2 = datetime.strptime(prev["race_date"], "%Y-%m-%d")
                    days_since = (d1 - d2).days
                except Exception:
                    days_since = None
            else:
                prev_pos = None
                dist_diff = 0
                jk_changed = False
                style = None
                days_since = None

            out[hn] = {
                "prev_pos": prev_pos,
                "dist_diff": dist_diff,
                "prev_style": style,
                "weight_change": wc,
                "sire": sire,
                "jk_changed": jk_changed,
                "days_since_prev": days_since,
                "jockey_id": cur_jk,
            }

    return out


def compute_anasanee_score(feat: dict, jockey_name: str = "") -> dict:
    """個別馬の穴予兆スコアを計算。

    feat: fetch_horse_history の返り値の1要素
    jockey_name: 今回騎手名 (None も受容)

    返り値: {score: int, reasons: list[str]}
    """
    # 2026-05-27: 5/31 ダービー dry-run で発見した NoneType bug 対処
    # jockey_name=None で呼ばれると `key in None` で TypeError
    jockey_name = jockey_name or ""
    feat = feat or {}
    score = 0
    reasons = []

    # 前走着順
    pp = feat.get("prev_pos")
    if pp is not None:
        if pp >= 10:
            score += 4
            reasons.append(f"前走{pp}着(+4)")
        elif pp >= 6:
            score += 1
            reasons.append(f"前走{pp}着(+1)")

    # 距離変更
    dd = feat.get("dist_diff", 0) or 0
    if dd >= 400:
        score += 3
        reasons.append(f"+{dd}m大幅延長(+3)")
    elif dd <= -400:
        score += 1
        reasons.append(f"{dd}m大幅短縮(+1)")

    # 前走脚質 (追込・差しは紛れやすい)
    st = feat.get("prev_style")
    if st == "追込":
        score += 3
        reasons.append("前走追込(+3)")
    elif st == "差し":
        score += 1
        reasons.append("前走差し(+1)")

    # 馬体重変化
    wc = feat.get("weight_change")
    if wc is not None:
        if wc <= -10:
            score += 2
            reasons.append(f"{wc}kg大幅減(+2)")
        elif wc >= 10:
            score += 1
            reasons.append(f"+{wc}kg大幅増(+1)")

    # 騎手変更
    if feat.get("jk_changed"):
        score += 1
        reasons.append("騎手変更(+1)")

    # 中休み (1-3ヶ月)
    ds = feat.get("days_since_prev")
    if ds is not None and 35 < ds <= 90:
        score += 1
        reasons.append(f"中休み{ds}日(+1)")

    # 穴出しサイアー
    sire = feat.get("sire", "") or ""
    for key, bonus in SIRE_ANA_BONUS.items():
        if key in sire:
            score += bonus
            reasons.append(f"{key}産駒(+{bonus})")
            break

    # 穴出し騎手
    for key, bonus in JOCKEY_ANA_BONUS.items():
        if key in jockey_name:
            score += bonus
            reasons.append(f"{key}騎手(+{bonus})")
            break

    return {"score": score, "reasons": reasons}


# ───────────────────────────────────────────────
# C. jockey_trust (上位人気騎乗時の信頼度補正)
# ───────────────────────────────────────────────

def compute_jockey_trust(jockey_name: str, popularity: int) -> int:
    """上位人気 (1-3人気) 時の騎手信頼度補正。

    返り値: -3 ~ +3 (◎の信頼度評価に加算)
    """
    if not popularity or popularity > 3 or not jockey_name:
        return 0

    for key, bonus in JOCKEY_TOP_TRUST_HIGH.items():
        if key in jockey_name:
            return bonus
    for key, penalty in JOCKEY_TOP_TRUST_LOW.items():
        if key in jockey_name:
            return penalty

    return 0
