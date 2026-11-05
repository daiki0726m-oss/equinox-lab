#!/usr/bin/env python3
"""買い目構造 (strategy/betting.py の honor) の点数と予算の単体テスト (#169).

  python3 scripts/test_bet_structures.py        (失敗があれば exit 1)

#169 で見つかった欠陥: 構造ごとの予算が「ワイド5点前提」の固定式 (band amt×25 /
D amt×29 / A・G amt×11) のままで、#146/#149 でワイドを6点にした後は各構造の
最後の1点が黙って落ちていた (A 6→5点、band 10→9点、D 24→23点)。
ここでは仕様 (docs/BETTING_SPEC.md) の点数と金額をそのまま固定する。
点数を変える改修をしたら、このテストと BETTING_SPEC.md を同時に直すこと。
"""
import contextlib
import io
import os
import sys
from itertools import combinations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# フラグは本番の既定 (config/prediction_flags.yml) と同じ値で固定する
os.environ["BET_STRUCTURE_ADAPTIVE"] = "1"
os.environ["BET_D_NO_CHU"] = "1"
os.environ["BET_GATE_FIXED"] = "1"       # 投資ゲートの閾値を基準表に依存させない
os.environ.pop("BET_KEEP_D", None)
os.environ.pop("CONFIDENCE_SIZING", None)

from strategy.betting import BettingStrategy  # noqa: E402

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r} want {want!r}")


MARKS = {1: "◎", 2: "○", 3: "▲", 4: "△", 5: "△", 6: "☆", 7: "注"}


def make_preds(n, axis_odds, real_odds=True, with_value=True, lgb_swap=False):
    """n頭の予測。馬番1=◎、2-5=○▲△△、6=☆、7=注、8以降は無印。

    lgb_swap=True: 無印の8番を LGB の複勝2位にする (A の相手が印と別になることの確認用)。
    """
    preds = []
    for i in range(1, n + 1):
        mark = MARKS.get(i, "")
        if not with_value and mark == "☆":
            mark = ""
        pw = max(0.30 - 0.02 * (i - 1), 0.01)
        pt3 = max(0.20 - 0.012 * (i - 1), 0.01)
        if lgb_swap and i == 8:
            pt3 = 0.19
        odds = axis_odds if i == 1 else round(3.0 + 2.5 * i, 1)
        if mark == "☆":
            odds = 9.0
        if mark == "注":
            odds = 25.0
        preds.append({
            "horse_number": i, "horse_name": f"馬{i}", "mark": mark,
            "pred_win": pw, "pred_top3": pt3,
            "odds_win": odds if real_odds else round(0.8 / pw, 1),
            "odds_place": 1.5, "_has_real_odds": real_odds,
        })
    return preds


def run(preds, confidence="A", race_name="3歳以上2勝クラス"):
    s = BettingStrategy()
    with contextlib.redirect_stdout(io.StringIO()):
        res = s.generate_bets(preds, confidence=confidence,
                              race_info={"race_name": race_name})
    honor = [b for b in res["bets"] if b.get("honor")]
    count = {}
    for b in honor:
        count[b["type"]] = count.get(b["type"], 0) + 1
    return res, honor, count


def unit(honor, t):
    return sorted({b["amount"] for b in honor if b["type"] == t})


# ── 1. 構造A (◎3倍台・12頭): ワイド6 + 三連複◎軸×LGB複勝上位4頭 6点 = 1,200円 ──
res, honor, cnt = run(make_preds(12, 3.5))
check("A 点数", cnt, {"ワイド": 6, "三連複": 6})
check("A 単価", (unit(honor, "ワイド"), unit(honor, "三連複")), ([100], [100]))
check("A 合計", res["total_amount"], 1200)
check("A 残り予算が負にならない", res["remaining"] >= 0, True)
check("A EV層なし", len(res["bets"]) - len(honor), 0)

# A の相手は印でなく LGB の複勝上位4頭 (#121)
res, honor, cnt = run(make_preds(12, 3.5, lgb_swap=True))
pool = sorted({h for b in honor if b["type"] == "三連複" for h in b["horse_numbers"]} - {1})
check("A 相手=LGB複勝上位4頭 (無印8番を含む)", pool, [2, 3, 4, 8])
check("A 6点 (lgb_swap)", cnt.get("三連複"), 6)

# ── 2. band 11頭以上 (◎2倍台×A×平場): ワイド6 + 三連複 ☆○▲△△ 10点×2倍 = 2,600円 ──
res, honor, cnt = run(make_preds(12, 2.5))
check("band11+ 判定", res["trio_focus_band"], True)
check("band11+ 点数", cnt, {"ワイド": 6, "三連複": 10})
check("band11+ 単価", (unit(honor, "ワイド"), unit(honor, "三連複")), ([100], [200]))
check("band11+ 合計", res["total_amount"], 2600)
want_trio = sorted(tuple(sorted((1, a, b))) for a, b in combinations([6, 2, 3, 4, 5], 2))
got_trio = sorted(tuple(sorted(b["horse_numbers"])) for b in honor if b["type"] == "三連複")
check("band11+ 三連複の組 (◎軸 ☆○▲△△ 5頭流し)", got_trio, want_trio)
check("band11+ 馬連なし", "馬連" in cnt, False)
check("band11+ EV層なし (honor が予算を使い切る)", len(res["bets"]) - len(honor), 0)

# ── 3. band 10頭以下: 構造A の分岐が先 → LGB上位4頭 6点×2倍 (現状仕様を固定) ──
res, honor, cnt = run(make_preds(10, 2.5))
check("band10- 判定", res["trio_focus_band"], True)
check("band10- 点数", (cnt.get("ワイド"), cnt.get("三連複")), (6, 6))
check("band10- 単価", unit(honor, "三連複"), [200])
check("band10- honor合計", sum(b["amount"] for b in honor), 1800)
check("band10- 予算の上限 2,500円", res["budget"], 2500)

# ── 4. 構造D (◎2倍台×11頭以上で band 不成立 = 未勝利): 三連単F 24点 + ワイド6 ──
res, honor, cnt = run(make_preds(12, 2.5), race_name="2歳未勝利")
check("D band不成立", res["trio_focus_band"], False)
check("D 点数 (NO_CHU=1)", cnt, {"ワイド": 6, "三連単": 24})
tri = [b["horse_numbers"] for b in honor if b["type"] == "三連単"]
check("D 1着は ◎☆ だけ", sorted({h[0] for h in tri}), [1, 6])
check("D 2着は ◎☆○▲", sorted({h[1] for h in tri}), [1, 2, 3, 6])
check("D 3着に注(7番)が入らない", any(h[2] == 7 for h in tri), False)
check("D 全点の合計", res["total_amount"], 3000)

os.environ["BET_D_NO_CHU"] = "0"
res, honor, cnt = run(make_preds(12, 2.5), race_name="2歳未勝利")
check("D 点数 (NO_CHU=0)", cnt.get("三連単"), 30)
os.environ["BET_D_NO_CHU"] = "1"

res, honor, cnt = run(make_preds(12, 2.5, with_value=False), race_name="2歳未勝利")
check("D ☆なし (相手5頭) は18点", cnt.get("三連単"), 18)

# ── 5. 構造G (◎1倍台): 三連複2頭軸 4点 + ワイド6 ──
res, honor, cnt = run(make_preds(12, 1.8))
check("G 点数", cnt, {"ワイド": 6, "三連複": 4})
common = set.intersection(*(set(b["horse_numbers"]) for b in honor if b["type"] == "三連複"))
check("G 2頭軸 = ◎+☆", sorted(common), [1, 6])

# ── 6. 実オッズ無し (木金の事前予測): 構造なし → 1,000円上限で 馬連6+ワイド4 ──
res, honor, cnt = run(make_preds(12, 0, real_odds=False))
check("構造なし 点数", cnt, {"馬連": 6, "ワイド": 4})
check("構造なし honor合計", sum(b["amount"] for b in honor), 1000)

# ── 7. 旧来の信頼度比例 (CONFIDENCE_SIZING=1, S=200円/点) でも点が欠けない ──
os.environ["CONFIDENCE_SIZING"] = "1"
res, honor, cnt = run(make_preds(12, 3.5), confidence="S")
check("A (S×200円) 点数", cnt, {"ワイド": 6, "三連複": 6})
check("A (S×200円) 合計", res["total_amount"], 2400)
os.environ.pop("CONFIDENCE_SIZING")

# ── 8. #30 と band の優先順位: band が優先 (2.5-2.9倍)、3.0-3.9倍は見送り ──
s = BettingStrategy()
for odds, want_band in ((2.8, True), (3.4, False)):
    preds = make_preds(12, odds)
    with contextlib.redirect_stdout(io.StringIO()):
        sb, reason = s.should_bet_race(preds, confidence="A",
                                       race_info={"race_name": "3歳以上1勝クラス"})
    check(f"#30 ◎{odds}倍 は見送り判定", sb, False)
    check(f"#30 ◎{odds}倍 の理由", reason.startswith("1勝戦×◎オッズ"), True)
    check(f"#30 ◎{odds}倍 band={want_band}", s.trio_focus_band(preds, "A"), want_band)

txt = BettingStrategy.band_override_reason("1勝戦×◎オッズ2.8倍は妙味中間帯 (loss layer 11.5%)")
check("band 理由文は投資が先", txt.startswith("trio-focus band で三連複/ワイドのみ投資"), True)
check("band 理由文に上書きした判定を残す", "通常の判定なら見送り: 1勝戦×◎オッズ2.8倍" in txt, True)

# ── 9. BET_KEEP_D=0 は D だけでなく band(11頭以上) の資金の乗る買い目も変える (現状仕様を固定) ──
# 変えるなら docs/BETTING_SPEC.md §4・§8 と同時に直すこと。
os.environ["BET_KEEP_D"] = "0"
res, honor, cnt = run(make_preds(12, 2.5), race_name="2歳未勝利")
check("KEEP_D=0 D→A 点数", cnt, {"ワイド": 6, "三連複": 6})
res, honor, cnt = run(make_preds(12, 2.5))
check("KEEP_D=0 band11+ 判定は band のまま", res["trio_focus_band"], True)
check("KEEP_D=0 band11+ 三連複は LGB上位4頭 6点", cnt, {"ワイド": 6, "三連複": 6})
check("KEEP_D=0 band11+ 三連複は2倍額", unit(honor, "三連複"), [200])
check("KEEP_D=0 band11+ honor合計", sum(b["amount"] for b in honor), 1800)
check("KEEP_D=0 band11+ 予算の上限 2,500円 (残りが EV層へ)", res["budget"], 2500)
os.environ.pop("BET_KEEP_D")

if FAILS:
    print(f"❌ {len(FAILS)} 件失敗")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("✅ 買い目構造の点数・金額はすべて仕様どおり")
