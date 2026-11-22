#!/usr/bin/env python3
"""weekend_prefetch の対象日の決定 (#173 の heredoc を #179 で移設)。

workflow の「週末2日分の予測を生成」step が `DECISION=$(python3 scripts/prefetch_targets.py)` で呼び、
KEY="..." の行を eval する。対象日の指定 (env PREFETCH_DATES / --dates) が空なら移設前と完全に
同じ判定・同じ出力 (+ REQUESTED="0"):
  今日から見て「次の土曜・日曜」(今日が土日ならその日と翌日) のうち、次の日は外す (#173):
    - 予想が確定済みの日 (race_utils.prediction_locked_for #172)
    - 今日で、当日の最後の作り直し (predict.FINAL_REBUILD_JST = 9:00) を過ぎた日
#179: 土日以外の開催日 (月曜祝日・順延) の前夜予想のため、raceday_clock が dates=YYYYMMDD を渡す。
  その時はその日付だけを候補にする (YYYYMMDD 以外・今日〜今日+2日の外は警告して捨てる)。
  確定済み・当日 9:00 以降を外す規則は同じ。

テスト用: --now 2026-11-22T15:41 (JST) / --final-hm 0900 (predict を import しない)
"""
import argparse
import contextlib
import datetime
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

JST = datetime.timezone(datetime.timedelta(hours=9))


def parse_requested(text, today):
    """'20261123 20261124' / '20261123,20261124' → [date]。不正値・範囲外は stderr に警告して捨てる。"""
    out = []
    for tok in re.split(r"[\s,]+", (text or "").strip()):
        if not tok:
            continue
        if not re.fullmatch(r"\d{8}", tok):
            print(f"⚠️ #179: 対象日 '{tok}' は YYYYMMDD でない → 捨てる", file=sys.stderr)
            continue
        try:
            d = datetime.date(int(tok[:4]), int(tok[4:6]), int(tok[6:]))
        except ValueError:
            print(f"⚠️ #179: 対象日 '{tok}' は日付として読めない → 捨てる", file=sys.stderr)
            continue
        if not (today <= d <= today + datetime.timedelta(days=2)):
            print(f"⚠️ #179: 対象日 {tok} は今日〜2日後の外 → 捨てる", file=sys.stderr)
            continue
        if d not in out:
            out.append(d)
    return out


def decide(now, fr, requested=""):
    """now: JST の aware datetime、fr: (時, 分) = 当日の最後の作り直し。戻り値 KEY→値 の dict。"""
    from race_utils import prediction_locked_for, PREDICTION_LOCK_LABEL
    d = now.date()
    today = d.strftime("%Y%m%d")
    req = parse_requested(requested, d) if (requested or "").strip() else None
    if req is None:
        cands = [c for c in (d + datetime.timedelta(days=i) for i in range(3))
                 if c.weekday() in (5, 6)][:2]
    else:
        cands = req
    dates, locked, final = [], [], []
    for c in cands:
        ds = c.strftime("%Y%m%d")
        if prediction_locked_for(ds, now):
            locked.append(ds)
        elif ds == today and (now.hour, now.minute) >= fr:
            final.append(ds)
        else:
            dates.append(ds)
    return {
        "DATES": " ".join(dates),
        "LOCKED": " ".join(locked),
        "FINAL_SKIP": " ".join(final),
        "LOCK_LABEL": PREDICTION_LOCK_LABEL,
        "FINAL_LABEL": "%d:%02d" % fr,
        "FINAL_HM": "%02d%02d" % fr,
        "TODAY": today,
        "BEFORE21": "1" if now.hour < 21 else "0",
        # 今日の分をもう作らない回 (土曜の夕方〜夜の回 = 日曜分だけを作る)
        "TODAY_EXCLUDED": "1" if today in locked + final else "0",
        # #179: 対象日を指定された回 (土日以外の開催日の前夜予想)。出走馬が少なければ先に集める
        "REQUESTED": "1" if req is not None else "0",
    }


KEYS = ("DATES", "LOCKED", "FINAL_SKIP", "LOCK_LABEL", "FINAL_LABEL", "FINAL_HM",
        "TODAY", "BEFORE21", "TODAY_EXCLUDED", "REQUESTED")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="JST の現在時刻 (ISO8601) — テスト用")
    ap.add_argument("--final-hm", help="当日の最後の作り直し HHMM — テスト用 (predict を import しない)")
    ap.add_argument("--dates", default=None, help="対象日 (既定は env PREFETCH_DATES)")
    a = ap.parse_args(argv)
    if a.now:
        now = datetime.datetime.fromisoformat(a.now)
        now = now.replace(tzinfo=JST) if now.tzinfo is None else now.astimezone(JST)
    else:
        now = datetime.datetime.now(JST)
    if a.final_hm:
        fr = (int(a.final_hm[:-2]), int(a.final_hm[-2:]))
    else:
        # predict の import はモデル周りまで読む (数秒)。import 時の出力が eval に混ざらないよう stderr へ
        with contextlib.redirect_stdout(sys.stderr):
            from predict import FINAL_REBUILD_JST
        fr = tuple(FINAL_REBUILD_JST)
    requested = a.dates if a.dates is not None else os.environ.get("PREFETCH_DATES", "")
    out = decide(now, fr, requested)
    for k in KEYS:
        print('%s="%s"' % (k, out[k]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
