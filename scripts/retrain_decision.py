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

停滞通知 (#95) の判定材料として経過日数 (age, 整数日) と経過時間 (age_hours) も出す。
通知するかどうかは workflow 側で「今回モデルを更新しなかった かつ 14日以上」で決める
(旧実装は再学習**前**に測った age で判定していたため、通知は必ず再学習を実行した回に出て
毎回誤報だった。Issue #116 はモデル配備の5秒後に作られている)。

出力 (stdout, GITHUB_OUTPUT にそのまま追記できる key=value):
    retrain=true|false   今回再学習するか
    age=<int>            最終再学習からの経過日数 (切り捨て。API 不達なら 999)
    age_hours=<float>    経過時間 (API 不達なら空)
    api_ok=true|false    最終コミット時刻を取得できたか
    last_week / this_week  ISO 週 (JST) 例: 2026-W45
    reason=<text>        判定理由 (ログ用)
"""
import argparse
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


def decide(last_commit, now, event):
    """last_commit: 最終再学習コミットの時刻 (aware datetime か None)。
    now: 判定時刻 (aware)。event: github.event_name。"""
    out = {"this_week": iso_week(now)}
    if last_commit is None:
        out.update(api_ok=False, age=999, age_hours="", last_week="")
    else:
        sec = (now - last_commit).total_seconds()
        out.update(api_ok=True, age=int(sec // 86400), age_hours=round(sec / 3600, 1),
                   last_week=iso_week(last_commit))

    if event == "workflow_dispatch":
        out.update(retrain=True, reason="手動実行 (workflow_dispatch) は常に再学習")
    elif not out["api_ok"]:
        # 判定材料が無い時は再学習する側に倒す (#95: 沈黙停止の方が害が大きい)
        out.update(retrain=True, reason="最終再学習の時刻が取れない → 安全側で再学習")
    elif out["last_week"] != out["this_week"]:
        out.update(retrain=True,
                   reason=f"今週 ({out['this_week']}) はまだ再学習していない (前回 {out['last_week']})")
    else:
        out.update(retrain=False,
                   reason=f"今週 ({out['this_week']}) は再学習済み (前回から {out['age_hours']}時間)")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--last-commit", default="",
                    help="model_rank.pkl の最終コミット時刻 (GitHub API の committer.date)")
    ap.add_argument("--event", default="schedule", help="github.event_name")
    ap.add_argument("--now", default="", help="判定時刻 (ISO8601, テスト用。省略時は現在)")
    args = ap.parse_args()

    now = parse_iso(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        print(f"--now を解釈できない: {args.now!r}", file=sys.stderr)
        return 2
    d = decide(parse_iso(args.last_commit), now, args.event)

    for k in ("retrain", "age", "age_hours", "api_ok", "last_week", "this_week", "reason"):
        v = d[k]
        if isinstance(v, bool):
            v = "true" if v else "false"
        print(f"{k}={v}")
    mark = "🧠 再学習する" if d["retrain"] else "⏭️ 再学習しない"
    print(f"{mark}: {d['reason']} / 経過 {d['age']}日 ({d['age_hours']}時間)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
