#!/usr/bin/env python3
"""公開予測 JSON (docs/data/predictions_YYYYMMDD.json) の「予測あり」判定 (#169)。

成果物ゲート (posting_watchdog の check_posting_health / 平日開催の raceday_safety_net /
翌朝の check_raceday_completeness / auto_post_x の export 後チェック) は、これまで
total_races (レース数) だけを見ていた。2026-11-01 00:35 に仮馬番のまま23レースを予測した
JSON が公開された時も、02:25 と 05:30 の watchdog は「予測あり (ok)」と判定し、
作り直しを起動しなかった (6時間18分、出走しない馬に◎が付いたまま)。

export_predictions.py がレースごとに numbers_confirmed (馬番が枠順確定後のもので、
予測の馬番↔馬名が DB と一致しているか) を書くので、ゲートはそれが False のレースを
「予測あり」と数えない。判定はこの1か所に置き、各ゲートは import して使う。

標準ライブラリだけで動く (watchdog は keiba.db も pandas も持たない #150)。
"""
import json
import os


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def iter_races(d):
    """export_predictions の形式 {venues: {場名: [race, ...]}} (旧形式の list / races も許容)。"""
    if not d:
        return
    if isinstance(d, list):
        yield from (r for r in d if isinstance(r, dict))
        return
    venues = d.get("venues")
    if isinstance(venues, dict):
        for rs in venues.values():
            for r in rs or []:
                if isinstance(r, dict):
                    yield r
        return
    for r in d.get("races") or []:
        if isinstance(r, dict):
            yield r


def summarize(d):
    """{total, ready, unconfirmed, unknown, unconfirmed_labels}。

    numbers_confirmed が無いレース (#169 より前に書かれた JSON) は判定材料が無いので
    unknown とし、ready には数える (古い JSON を一斉に「予測なし」扱いにして
    作り直しの dispatch を乱発しないため)。
    """
    out = {"total": 0, "ready": 0, "unconfirmed": 0, "unknown": 0, "unconfirmed_labels": []}
    races = list(iter_races(d))
    if not races and isinstance(d, dict) and isinstance(d.get("total_races"), int):
        # venues を持たない最小形式: レース数だけ分かる (判定材料なし)
        out["total"] = out["ready"] = out["unknown"] = d["total_races"]
        return out
    for r in races:
        out["total"] += 1
        nc = r.get("numbers_confirmed")
        if nc is False:
            out["unconfirmed"] += 1
            out["unconfirmed_labels"].append(
                f"{r.get('venue', '')}{r.get('race_number', '')}R")
            continue
        if nc is None:
            out["unknown"] += 1
        out["ready"] += 1
    # export_predictions が「馬番未確定で前の版も無い」ため公開しなかったレース (#169)。
    # JSON に載っていないので数え漏らすと、ゲートが作り直しを起動しない
    for w in (d.get("numbers_withheld") or []) if isinstance(d, dict) else []:
        if isinstance(w, dict):
            out["total"] += 1
            out["unconfirmed"] += 1
            out["unconfirmed_labels"].append(
                f"{w.get('venue', '')}{w.get('race_number', '')}R")
    return out


def ready_race_count(d):
    """「予測あり」と数えてよいレース数 (馬番未確定のレースを除く)。"""
    return summarize(d)["ready"]


def published_unconfirmed(d):
    """JSON に載っている (= 公開される) 馬番未確定のレース数。withheld は公開されないので含めない。

    auto_post_x の export 後チェックが origin の版と比べる時に使う (#169)。
    """
    return sum(1 for r in iter_races(d) if r.get("numbers_confirmed") is False)


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--unconfirmed":
        # 使い方: prediction_gate.py --unconfirmed <path | ->  → 公開される未確定レース数を出力
        _src = sys.argv[2]
        _d = json.load(sys.stdin) if _src == "-" else load(_src)
        if _d is None:
            sys.exit(1)
        print(published_unconfirmed(_d))
        sys.exit(0)
    for p in sys.argv[1:]:
        s = summarize(load(p))
        print(f"{os.path.basename(p)}: {s['ready']}/{s['total']}R 予測あり "
              f"(馬番未確定 {s['unconfirmed']}R {s['unconfirmed_labels'][:6]} / 判定材料なし {s['unknown']}R)")
