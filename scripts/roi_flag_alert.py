#!/usr/bin/env python3
"""ROI 監視の「要降格」フラグが連続した時に知らせる (#169)。weekly_retrain.yml から呼ぶ。

#169 (2026-11-05): roi_weekly_monitor (#95) は trio_focus_band と 注の単勝20〜50倍帯に
「🔻 要降格レビュー」を 8週以上連続で立てていたが、**どこにも届いていなかった**。
フラグを使っていたのは weekly_retrain の失敗/停滞 Issue の本文だけで、run が成功した週は
誰にも知らされず、strategy/betting.py も roi_weekly.json を読まない (band はそのまま
三連複を2倍額にし続けた)。
→ フラグが **N週 (既定2週) 続いたら** Issue を1本だけ作り、以降は同じ Issue にコメントで
   更新する (#161 の「開催日のやり残し」Issue と同じ集約方式。重複 Issue を作らない)。
   降格するかどうかはユーザーが判断する (自動では降格しない)。

連続の数え方:
- roi_weekly.json は週1回 (weekly_retrain) 作られる。ISO 週 (JST) ごとに1件を
  docs/data/roi_flag_history.json に記録し、同じ週の再実行は上書きする (二重に数えない)。
- **履歴はこのスクリプトの導入時点から数え始める**。それ以前の roi_weekly.json の
  フラグは、上書き後のオッズと仮の買い方で選んだ別のレースを測っていた
  (本番の band と 30/92R しか重ならない、#169 で M 班が母集団を修正) ので、
  git の過去版から連続を復元すると、修正前の誤ったフラグで初回から通知してしまう。
- roi_weekly.json に母集団の版 (schema_version / population の定義文) が書かれていれば、
  版が変わった所で連続を切る (母集団の違う週を同じ連続として数えない)。
  population の **件数** (days の日付一覧・races_with_results 等) は窓が1週ずれるたびに
  変わるので版に含めない (含めると連続が毎週切れて通知が一度も出ない。population_key 参照)。
- ユーザーが Issue を閉じたら、その時点で続いている連続は「判断済み」として以後通知しない
  (band は降格しない判断もあり得る枠なので、閉じた後に週1本ずつ Issue が増えないように)。
  新しい区分の連続や、一度外れて再び立った連続は新しく通知する。workflow が
  --issue-closed-at に「同じタイトルの閉じた Issue の closedAt」を渡す (開いている Issue が無い時だけ)。
- 観測が抜けた週 (run 失敗等) は連続を切らない。抜けは「回復した証拠」ではないため。

終了コード (workflow が分岐に使う):
  0 = 通知なし (連続なし / 今週は通知済み / 閉じた Issue で判断済みの連続だけ)
  1 = 通知する (--report に Issue 本文を書く)
  3 = 前回通知した区分のフラグが外れた (--report に「解消」コメントを書く)
  2 = 判定不能 (roi_weekly.json が無い・古い・壊れている。履歴は更新しない)
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
KEEP_WEEKS = 26

# 区分の意味 (Issue を読む人向け。定義は scripts/roi_weekly_monitor.py の segments)
SEGMENT_DESC = {
    "trio_focus_band": "◎2.0〜2.9倍 × 平場のレースで三連複を2倍額にする買い方 (#95)",
    "W1_S_2勝_中距離_単勝": "walk-forward の whitelist ① 単勝 (#31/#32)",
    "W2_函館ダート_単勝": "walk-forward の whitelist ② 単勝 (#31/#32)",
    "chu_単勝_7-20倍": "注 (妙味の穴) の単勝 7〜20倍帯 (#100、観察用・賭けない)",
    "chu_単勝_20-50倍": "注 (妙味の穴) の単勝 20〜50倍帯 (#100、観察用・賭けない)",
}


def is_demote(flag):
    """要降格かどうか。roi_weekly_monitor の flag は文字列 ('🔻 要降格レビュー (...)')。"""
    if flag is True:
        return True
    return isinstance(flag, str) and "降格" in flag


def iso_week(dt):
    y, w, _ = dt.astimezone(JST).isocalendar()
    return f"{y}-W{w:02d}"


def parse_time(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # roi_weekly_monitor は naive な isoformat を書く (CI では UTC)。
        # 鮮度判定 (72時間) にしか使わないので ±9時間のずれは問題にならない。
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        print(f"⚠️ {path} を読めない: {e}", file=sys.stderr)
        return None


def population_key(roi):
    """連続を切る「母集団の版」を roi_weekly.json から取り出す。

    #169 レビュー: M 班の roi_weekly_monitor は population に母集団の **定義文**
    (bets / band / chu_odds_band の文字列) と、その週の **件数** (days の日付一覧・
    races_with_results・races_funded・band_overlap 等) を一緒に入れる。件数は12週の窓が
    1週ずれるたびに必ず変わるので、population を丸ごと比べると毎週「版が変わった」と判定され、
    連続が必ず1週で切れて通知が一度も出なかった (M 形式の3週で実測)。
    → schema_version があればそれ、population が dict なら文字列の値 (定義文) だけを版とする。
       件数を履歴に溜めないため、保存するのもこの版だけ。"""
    v = roi.get("schema_version")
    if v not in (None, ""):
        return str(v)
    p = roi.get("population")
    if isinstance(p, dict):
        defs = {k: p[k] for k in sorted(p) if isinstance(p[k], str)}
        return json.dumps(defs, ensure_ascii=False, sort_keys=True) if defs else None
    if isinstance(p, str) and p:
        return p
    return None


def streaks(weeks):
    """weeks: {週: entry}。最新の週から遡って、各区分が何週続けて要降格かを返す。
    {区分: (連続数, 連続の始まりの週)}"""
    order = sorted(weeks)
    if not order:
        return {}
    latest = weeks[order[-1]]
    pop = latest.get("population")
    out = {}
    for seg in latest.get("flagged", {}):
        n, start = 0, None
        for w in reversed(order):
            e = weeks[w]
            if e.get("population") != pop or seg not in e.get("flagged", {}):
                break
            n, start = n + 1, w
        out[seg] = (n, start)
    return out


def fmt_yen(v):
    try:
        return f"{int(v):,}円"
    except (TypeError, ValueError):
        return "-"


def build_alert(streaking, entry, this_week, resolved, dismissed=()):
    lines = [f"## 🔻 要降格フラグが連続しています ({this_week} 時点)", "",
             "| 区分 | 連続 | 対象レース | 投資 | 回収 | 回収率 | 判定 |",
             "|---|---|---|---|---|---|---|"]
    for seg, (n, start) in sorted(streaking.items(), key=lambda x: -x[1][0]):
        s = entry["flagged"][seg]
        lines.append(f"| {seg} | {n}週 ({start}〜) | {s.get('races', '-')}R | {fmt_yen(s.get('spend'))} "
                     f"| {fmt_yen(s.get('return'))} | {s.get('roi_pct', '-')}% | {s.get('flag', '')} |")
    lines.append("")
    for seg in sorted(streaking):
        if seg in SEGMENT_DESC:
            lines.append(f"- `{seg}`: {SEGMENT_DESC[seg]}")
    if resolved:
        lines += ["", f"前回通知した区分のうち、今週フラグが外れたもの: {', '.join(sorted(resolved))}"]
    if dismissed:
        lines += ["", f"閉じた Issue で通知済みの連続 (続いているが、判断済みとして再通知の対象にしていない): "
                      f"{', '.join(sorted(dismissed))}"]
    lines += [
        "",
        "- **降格するかどうかはユーザーが判断する** (自動では降格しない)。本番の買い目 "
        "(strategy/betting.py) は roi_weekly.json を読まないので、フラグが立っても賭け方は変わらない。",
        "- 数字は scripts/roi_weekly_monitor.py の直近12週の集計 (重なる窓なので、連続は独立な証拠ではない)。"
        "判定前に区分の母集団と単日支配 (#26) を確認すること。",
        f"- 連続の記録: docs/data/roi_flag_history.json (導入した #169 から数え始めている)",
        "- この Issue を閉じると、その時点で続いている連続は「判断済み」として以後通知しない。"
        "新しい区分の連続や、一度外れて再び立った連続は新しい Issue で知らせる。",
    ]
    return "\n".join(lines)


def build_resolved(resolved, this_week):
    return "\n".join([
        f"## ✅ 要降格フラグが外れました ({this_week} 時点)", "",
        f"前回通知した区分のうち、今週フラグが外れたもの: {', '.join(sorted(resolved))}", "",
        "連続が途切れただけで、成績が回復したとは限らない (直近12週の重なる窓の集計)。"
        "この Issue を閉じるかどうかはユーザーが判断する。",
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--roi", default="docs/data/roi_weekly.json")
    ap.add_argument("--history", default="docs/data/roi_flag_history.json")
    ap.add_argument("--min-streak", type=int, default=2, help="何週続いたら通知するか")
    ap.add_argument("--max-age-hours", type=float, default=72,
                    help="roi_weekly.json がこれより古ければ判定不能 (監視ステップが失敗して先週の"
                         "ファイルが残っている時に、先週の値を今週として数えない)")
    ap.add_argument("--report", default="", help="Issue 本文 / コメントの書き出し先")
    ap.add_argument("--now", default="", help="判定時刻 (ISO8601, テスト用)")
    ap.add_argument("--issue-closed-at", default="",
                    help="同じタイトルの閉じた Issue の closedAt (開いている Issue が無い時だけ workflow が渡す)。"
                         "これより前に通知した連続は、ユーザーが判断済みとして通知しない")
    args = ap.parse_args()

    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    roi = load_json(args.roi)
    if not roi or not isinstance(roi.get("segments"), dict):
        print(f"❓ {args.roi} が無い/壊れている → 判定不能 (履歴は更新しない)")
        return 2
    gen = parse_time(roi.get("generated_at"))
    if gen is None or (now - gen) > timedelta(hours=args.max_age_hours):
        print(f"❓ {args.roi} が古い (generated_at={roi.get('generated_at')}) "
              f"→ 今週の監視が走っていない。判定不能 (履歴は更新しない)")
        return 2

    hist = load_json(args.history, default={})
    if hist is None:   # 壊れた履歴で上書きして連続を失わないよう、判定不能にする
        return 2
    weeks = hist.get("weeks") or {}
    this_week = iso_week(now)
    flagged = {}
    for seg, s in roi["segments"].items():
        if isinstance(s, dict) and is_demote(s.get("flag")):
            flagged[seg] = {k: s.get(k) for k in ("flag", "races", "spend", "return", "roi_pct")}
    entry = {
        "recorded_at": now.astimezone(JST).isoformat(timespec="seconds"),
        "roi_generated_at": roi.get("generated_at"),
        "population": population_key(roi),
        "flagged": flagged,
    }
    weeks[this_week] = entry
    weeks = {w: weeks[w] for w in sorted(weeks)[-KEEP_WEEKS:]}

    latest_week = max(weeks)
    if latest_week != this_week:
        # 時計のずれ等で履歴に「今週より後」の週がある。連続は最新の記録で数える。
        print(f"⚠️ 履歴に今週 ({this_week}) より後の週 ({latest_week}) がある → 最新の記録で判定")
    latest = weeks[latest_week]
    st = streaks(weeks)
    streaking = {seg: v for seg, v in st.items() if v[0] >= args.min_streak}
    prev_alerting = set(hist.get("alerting") or [])
    resolved = prev_alerting - set(streaking)

    for seg, (n, start) in sorted(st.items()):
        mark = "🔔" if seg in streaking else "  "
        s = latest["flagged"][seg]
        print(f"{mark} {seg}: 要降格 {n}週連続 (始まり {start}) / ROI {s.get('roi_pct')}% n={s.get('races')}")
    if not st:
        print(f"要降格フラグなし ({latest_week})")

    # #169 レビュー: 重複防止を「開いている Issue にコメントする」だけで行うと、ユーザーが
    # 「降格しない」と判断して Issue を閉じても、フラグが残る限り毎週新しい Issue が作られる。
    # 区分ごとに「今続いている連続を最初に通知した時刻」を覚えておき、閉じた Issue の closedAt
    # より前に通知済みの連続は判断済みとして黙る。連続が途切れたら記録は消える (再び立てば新規)。
    notified = {seg: v for seg, v in (hist.get("notified_streaks") or {}).items()
                if seg in streaking and isinstance(v, dict) and v.get("start") == streaking[seg][1]}
    closed_at = parse_time(args.issue_closed_at) if args.issue_closed_at else None
    dismissed = set()
    if closed_at is not None:
        for seg, v in notified.items():
            at = parse_time(v.get("at"))
            if at is not None and at <= closed_at:
                dismissed.add(seg)
    active = {seg: v for seg, v in streaking.items() if seg not in dismissed}

    code, body = 0, ""
    if streaking:
        same = set(streaking) == prev_alerting and hist.get("notified_week") == this_week
        if same:
            print(f"ℹ️ {this_week} は通知済み (同じ週の再実行) → 通知しない")
        elif not active:
            print(f"ℹ️ 続いている連続 ({', '.join(sorted(dismissed))}) はすべて、閉じた Issue "
                  f"(closedAt={args.issue_closed_at}) より前に通知済み → 判断済みとして通知しない")
        else:
            code, body = 1, build_alert(streaking, latest, latest_week, resolved, dismissed)
            hist["notified_week"] = this_week
            for seg, (n, start) in active.items():
                notified.setdefault(seg, {"start": start,
                                          "at": now.astimezone(JST).isoformat(timespec="seconds")})
    elif resolved:
        code, body = 3, build_resolved(resolved, this_week)

    hist.update({
        "note": "#169: ROI 監視の要降格フラグの連続記録。weekly_retrain が scripts/roi_flag_alert.py で更新する。"
                "ISO週 (JST) ごとに1件。導入時点から数え始める (それ以前のフラグは母集団が誤っていたため)。",
        "min_streak": args.min_streak,
        "alerting": sorted(streaking),
        "notified_streaks": notified,
        "weeks": weeks,
    })
    os.makedirs(os.path.dirname(os.path.abspath(args.history)), exist_ok=True)
    with open(args.history, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
        f.write("\n")
    if body and args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(body + "\n")
    if body:
        print("\n" + body)
    return code


if __name__ == "__main__":
    sys.exit(main())
