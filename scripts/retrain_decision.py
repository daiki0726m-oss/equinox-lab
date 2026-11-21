#!/usr/bin/env python3
"""週次再学習を「今回やるか」の判定 (#169)。weekly_retrain.yml の Check model age から呼ぶ。

#169 (2026-11-05): 旧判定は `AGE=$(( (NOW - LAST) / 86400 )) >= 7` で、経過日数を
**整数に切り捨てて** 7日以上かを見ていた。再学習の commit は run 開始の約27分後なので、
翌週の cron が前週より27分以上遅れて発火しない限り経過は 6.9日 → 6日 に切り捨てられ、
再学習が飛ばされる。cron の発火は 3〜7時間ぶれるため、7/20 以降の定期実行16回のうち
再学習したのは8回だけ = 「週次」が実際は隔週だった。手動で再学習した直後の週
(8/28→8/31, 9/1→9/7, 10/20→10/26) も同じ理由で飛ばされていた。

→ **ISO 週 (JST) で「今週まだ再学習していないか」** を判定する。
   ・経過時間の閾値 (例: 6.5日) だと、週の途中に手動で再学習した翌週月曜 (10/20→10/26 は
     6.33日) を依然として飛ばす。週次の約束は「毎週1回」なので、週番号で判定するのが素直。
   ・cron の遅延 (月曜 12:00 JST → 最大 19:00 前後) は同じ ISO 週の中に収まるので、
     遅延の大きさに判定が左右されない。
   ・最終再学習の時刻は #95 のとおり GitHub API の model_rank.pkl の最終コミット時刻を使う
     (actions/checkout は mtime を保存しないため、ファイルの mtime は使えない)。

#169 レビュー: 毎週再学習になったことで、**月曜の開催日 (祝日開催・順延)** にも必ず再学習が
走るようになった (旧判定は 2026年の月曜開催 9/21・10/12 をたまたま両方飛ばしていた)。cron の
実発火は JST 12:42〜19:23 にばらつくので、モデルの差し替え・投資ゲートの閾値の引き直し
(build_race_baseline)・keiba.db.gz の push (-X theirs) が、開催中の race_day_runner
(concurrency グループが別で直列化されない) や 17:30 の結果投稿と重なる。週次レポートも
月曜のレースが終わる前の途中経過で集計してしまう。
→ 定期実行 (schedule) で **JST の今日が開催日なら、再学習も週次レポートも延期** する (run=false)。
   火・水の予備 cron が「今週まだ再学習していなければやる」(水曜は 9/21・9/22 のように月火とも
   開催の週のため)。ISO 週の判定は同じ週に何度走っても結果が変わらないので、予備 cron を足しても
   二重に再学習しない。開催日でない普通の週の火・水は「今週は再学習済み」で何もしない
   (run=false = commit もしないので、リポジトリも増えない #153)。
   月曜の cron が GitHub 側で丸ごと飛ばされた週 (#36/#62) の取り戻しにもなる。
   開催日の判定は keiba.db の races (JRA の場コードのみ、#161) を正とし、読めなければ
   race_calendar.json。どちらも判定できなければ延期しない (再学習しない害の方が大きい #95)。

停滞通知 (#95) の判定材料として経過日数 (age, 整数日) と経過時間 (age_hours) も出す。
通知するかどうかは workflow 側で「今回モデルを更新しなかった かつ 14日以上」で決める
(旧実装は再学習**前**に測った age で判定していたため、通知は必ず再学習を実行した回に出て
毎回誤報だった。Issue #116 はモデル配備の5秒後に作られている)。

出力 (stdout, GITHUB_OUTPUT にそのまま追記できる key=value):
    run=true|false       今回この run で何かするか (false = 開催日で延期 / 予備 cron で今週は済み。
                         後続の step (週次レポート・commit) をすべて飛ばす)
    retrain=true|false   今回再学習するか
    race_day=true|false|unknown  JST の今日が開催日か (判定の材料)
    age=<int>            最終再学習からの経過日数 (切り捨て。API 不達なら 999)
    age_hours=<float>    経過時間 (API 不達なら空)
    api_ok=true|false    最終コミット時刻を取得できたか
    last_week / this_week  ISO 週 (JST) 例: 2026-W45
    reason=<text>        判定理由 (ログ用)
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

# 日本は夏時間が無いので固定オフセットで正確 (zoneinfo/tzdata に依存しない)
JST = timezone(timedelta(hours=9))


def parse_iso(s):
    """GitHub API の committer.date ('2026-11-02T10:52:02Z') を aware datetime に。
    空・'null'・解釈不能は None (= 取得失敗として扱う)。"""
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.lower() == "null":
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso_week(dt):
    """JST での ISO 週 ('2026-W45')。年またぎは ISO 年で数える (12/29 が翌年 W01 になりうる)。"""
    y, w, _ = dt.astimezone(JST).isocalendar()
    return f"{y}-W{w:02d}"


def race_day_status(day_iso, db_path="keiba.db", calendar_path="docs/data/race_calendar.json"):
    """JST の day_iso (YYYY-MM-DD) が JRA の開催日か。(True/False/None, 根拠)。

    weekly_retrain は先頭で keiba.db を展開しているので DB を正とする (race_calendar.json は
    1日1回の生成で、後から登録された順延開催を翌日まで載せない #161)。DB が読めなければ
    カレンダーに載っているかだけを見る (載っていなくても「開催日でない」とは断定しない)。"""
    if db_path and os.path.exists(db_path):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM races WHERE race_date = ? AND race_date != '' "
                    # #161: JRA の場コード (01-10) だけ。地方競馬の残骸行で開催日と誤認しない
                    "AND CAST(substr(race_id, 5, 2) AS INT) BETWEEN 1 AND 10 AND venue != ''",
                    (day_iso,)).fetchone()[0]
            finally:
                conn.close()
            return n > 0, f"keiba.db の races に {n}R"
        except sqlite3.Error as e:
            print(f"⚠️ keiba.db で開催日を判定できない: {e}", file=sys.stderr)
    try:
        with open(calendar_path, encoding="utf-8") as f:
            cal = json.load(f)
        if ((cal.get("dates") or {}).get(day_iso) or {}).get("races"):
            return True, "race_calendar.json に掲載"
        return None, "keiba.db が読めず、race_calendar.json にも無い (判定不能)"
    except (OSError, ValueError, AttributeError):
        return None, "keiba.db も race_calendar.json も読めない (判定不能)"


def decide(last_commit, now, event, race_day=None):
    """last_commit: 最終再学習コミットの時刻 (aware datetime か None)。
    now: 判定時刻 (aware)。event: github.event_name。
    race_day: JST の今日が開催日か (True/False/None=不明)。定期実行で True の時だけ延期する。"""
    out = {"this_week": iso_week(now), "race_day": race_day, "run": True}
    if last_commit is None:
        out.update(api_ok=False, age=999, age_hours="", last_week="")
    else:
        sec = (now - last_commit).total_seconds()
        out.update(api_ok=True, age=int(sec // 86400), age_hours=round(sec / 3600, 1),
                   last_week=iso_week(last_commit))

    today = now.astimezone(JST)
    if event == "workflow_dispatch":
        out.update(retrain=True, reason="手動実行 (workflow_dispatch) は常に再学習")
    elif race_day is True:
        # #169 レビュー: 開催中にモデル・閾値・DB を差し替えない。火・水の予備 cron に回す。
        out.update(run=False, retrain=False,
                   reason=f"今日 ({today.date()}) は開催日 → 再学習と週次レポートを延期 "
                          f"(火・水の予備 cron が今週分をやる)")
    elif not out["api_ok"]:
        # 判定材料が無い時は再学習する側に倒す (#95: 沈黙停止の方が害が大きい)
        out.update(retrain=True, reason="最終再学習の時刻が取れない → 安全側で再学習")
    elif out["last_week"] != out["this_week"]:
        out.update(retrain=True,
                   reason=f"今週 ({out['this_week']}) はまだ再学習していない (前回 {out['last_week']})")
    elif today.weekday() == 0:
        # 月曜の本番 cron: 週の途中で手動再学習済みでも、週次レポートは従来どおり出す
        out.update(retrain=False,
                   reason=f"今週 ({out['this_week']}) は再学習済み (前回から {out['age_hours']}時間)。"
                          f"週次レポートだけ更新する")
    else:
        # 火・水の予備 cron (または日付をまたいで遅延した月曜分): 今週は済んでいるので何もしない
        out.update(run=False, retrain=False,
                   reason=f"今週 ({out['this_week']}) は再学習済み (前回から {out['age_hours']}時間) "
                          f"→ 予備 cron は何もしない")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--last-commit", default="",
                    help="model_rank.pkl の最終コミット時刻 (GitHub API の committer.date)")
    ap.add_argument("--event", default="schedule", help="github.event_name")
    ap.add_argument("--now", default="", help="判定時刻 (ISO8601, テスト用。省略時は現在)")
    ap.add_argument("--db", default="keiba.db", help="開催日の判定に使う DB (#169 レビュー)")
    ap.add_argument("--calendar", default="docs/data/race_calendar.json",
                    help="DB が読めない時の開催日の判定材料")
    args = ap.parse_args()

    now = parse_iso(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        print(f"--now を解釈できない: {args.now!r}", file=sys.stderr)
        return 2
    race_day, why = race_day_status(now.astimezone(JST).date().isoformat(), args.db, args.calendar)
    d = decide(parse_iso(args.last_commit), now, args.event, race_day)

    for k in ("run", "retrain", "race_day", "age", "age_hours", "api_ok", "last_week", "this_week", "reason"):
        v = d[k]
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif v is None:
            v = "unknown"
        print(f"{k}={v}")
    mark = ("🧠 再学習する" if d["retrain"] else
            "📊 再学習せず週次レポートだけ" if d["run"] else "⏸️ この run では何もしない")
    print(f"{mark}: {d['reason']} / 経過 {d['age']}日 ({d['age_hours']}時間) / 開催日: {why}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
