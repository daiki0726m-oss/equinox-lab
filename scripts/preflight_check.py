#!/usr/bin/env python3
"""データ完整性 pre-flight check (2026-05-24 導入)

X 投稿・予測・結果取り込み等の前に「データが揃っているか」を確認。
不足があれば自動修復 (fetch_weekend_races / predict 等を発火) する。

過去事故の教訓 (CLAUDE.md 過去のミス事例参照):
- 5/23 R1-R8 欠落で結果反映ゼロ
- 5/24 出走馬未登録(scraper バグ)で予測 0件
- これらは pre-flight でレース件数 / 出走馬件数 / 予測件数を
  チェックすれば事前に検知できた事象。

CLI:
  python scripts/preflight_check.py YYYYMMDD [--auto-fix] [--quiet]

返り値:
  0 = 全項目 OK
  1 = 警告あり (auto-fix で対応済 or 軽微)
  2 = 致命的 (auto-fix 不可)

各 workflow の予測・投稿の直前で呼び出すことを推奨。
"""
from __future__ import annotations
import sys
import os
import argparse
import subprocess

# database import path 解決
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import get_db, init_db  # noqa: E402


# 想定値 (土日開催・3場各12レース)
EXPECTED_RACES_WEEKEND = 30  # 36 が標準だが、開催数で変動するため緩めの最小値
EXPECTED_MIN_RUNNERS_PER_RACE = 5  # 障害戦等で少ない場合あり


def check_races(conn, date_iso: str) -> tuple[int, list[str]]:
    """races テーブルにそのDateのレースが揃っているか"""
    issues = []
    row = conn.execute(
        "SELECT COUNT(*) as c FROM races WHERE race_date = ?", (date_iso,)
    ).fetchone()
    n_races = row["c"] or 0
    if n_races == 0:
        issues.append(f"❌ races テーブルに {date_iso} のレースが 1件もない")
        return 2, issues
    if n_races < EXPECTED_RACES_WEEKEND:
        issues.append(
            f"⚠️ races 件数が少ない: {n_races}件 (期待 {EXPECTED_RACES_WEEKEND}件以上)"
        )
    return (1 if issues else 0), issues


def check_runners(conn, date_iso: str) -> tuple[int, list[str]]:
    """各レースで出走馬が DB に登録されているか"""
    issues = []
    rows = conn.execute(
        """SELECT r.race_id, r.venue, r.race_number,
                  (SELECT COUNT(*) FROM results WHERE race_id = r.race_id) as n_runners
           FROM races r WHERE r.race_date = ?""",
        (date_iso,)
    ).fetchall()
    if not rows:
        return 2, ["❌ races が無いため runners チェック不可"]

    empty = [r for r in rows if (r["n_runners"] or 0) == 0]
    too_few = [r for r in rows if 0 < (r["n_runners"] or 0) < EXPECTED_MIN_RUNNERS_PER_RACE]
    if empty:
        issues.append(
            f"❌ 出走馬未登録レース {len(empty)}件: "
            + ", ".join(f"{r['venue']}{r['race_number']}R" for r in empty[:5])
        )
        return 2, issues
    if too_few:
        issues.append(
            f"⚠️ 出走馬が極端に少ないレース {len(too_few)}件 "
            f"(障害戦等の可能性): "
            + ", ".join(
                f"{r['venue']}{r['race_number']}R({r['n_runners']}頭)" for r in too_few[:5]
            )
        )
    return (1 if issues else 0), issues


def check_predictions(conn, date_iso: str) -> tuple[int, list[str]]:
    """予測キャッシュが揃っているか"""
    issues = []
    row = conn.execute(
        """SELECT COUNT(*) as n FROM predictions_cache pc
           JOIN races r ON pc.race_id = r.race_id
           WHERE r.race_date = ?""",
        (date_iso,)
    ).fetchone()
    n_pred = row["n"] or 0
    n_races_row = conn.execute(
        "SELECT COUNT(*) as c FROM races WHERE race_date = ?", (date_iso,)
    ).fetchone()
    n_races = n_races_row["c"] or 0
    if n_races == 0:
        return 2, ["❌ races 無し"]
    if n_pred == 0:
        issues.append(f"❌ 予測キャッシュ 0件 (races: {n_races}件)")
        return 2, issues
    if n_pred < n_races:
        issues.append(f"⚠️ 予測キャッシュ不足: {n_pred}/{n_races}")
    return (1 if issues else 0), issues


def auto_fix_runners(date_yyyymmdd: str) -> bool:
    """出走馬不足を自動修復 (scrape_shutuba 経由)"""
    print(f"🔧 自動修復: 出走馬を取得中 ({date_yyyymmdd})...")
    try:
        result = subprocess.run(
            ["python3", "-c", f"""
from scraper import NetkeibaScraper
from database import get_db
s = NetkeibaScraper()
ids = s.get_race_list_by_date('{date_yyyymmdd}')
for rid in ids:
    try:
        data = s.scrape_shutuba(rid)
        if data:
            s.save_race_to_db(data)
    except Exception as e:
        print(f'⚠️ {{rid}}: {{e}}')
print(f'✅ {{len(ids)}}レース処理')
"""],
            capture_output=True, text=True, timeout=600
        )
        print(result.stdout[-500:])
        if result.returncode != 0:
            print(f"❌ 自動修復失敗: {result.stderr[-200:]}")
            return False
        return True
    except Exception as e:
        print(f"❌ 自動修復例外: {e}")
        return False


def auto_fix_predictions(date_yyyymmdd: str) -> bool:
    """予測キャッシュ不足を自動修復"""
    print(f"🔧 自動修復: 予測実行中 ({date_yyyymmdd})...")
    try:
        result = subprocess.run(
            ["python3", "predict.py", "predict", "--date", date_yyyymmdd, "--force"],
            capture_output=True, text=True, timeout=900
        )
        if result.returncode != 0:
            print(f"❌ 予測失敗: {result.stderr[-200:]}")
            return False
        print("✅ 予測完了")
        return True
    except Exception as e:
        print(f"❌ 予測例外: {e}")
        return False


def main():
    ap = argparse.ArgumentParser(description="データ完整性 pre-flight check")
    ap.add_argument("date", help="YYYYMMDD")
    ap.add_argument("--auto-fix", action="store_true", help="不足を自動修復")
    ap.add_argument("--quiet", action="store_true", help="OK時は出力しない")
    args = ap.parse_args()

    if len(args.date) != 8:
        print(f"❌ 日付フォーマット不正: {args.date}", file=sys.stderr)
        sys.exit(2)

    date_iso = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:8]}"
    init_db()

    all_issues = []
    overall_severity = 0  # 0=OK, 1=warn, 2=critical

    with get_db() as conn:
        # 1. races
        sev, issues = check_races(conn, date_iso)
        overall_severity = max(overall_severity, sev)
        all_issues.extend(issues)

        # 2. runners
        sev, issues = check_runners(conn, date_iso)
        overall_severity = max(overall_severity, sev)
        all_issues.extend(issues)
        runners_critical = sev == 2

        # 3. predictions
        sev, issues = check_predictions(conn, date_iso)
        overall_severity = max(overall_severity, sev)
        all_issues.extend(issues)
        pred_critical = sev == 2

    # Auto-fix
    if args.auto_fix and overall_severity == 2:
        print(f"\n🛠 Auto-fix mode: 致命的問題を修復します...\n")
        if runners_critical:
            if auto_fix_runners(args.date):
                print("✅ 出走馬の自動修復成功")
            else:
                print("❌ 出走馬の自動修復失敗")
        if pred_critical or runners_critical:
            # 出走馬入ったら予測も再実行
            if auto_fix_predictions(args.date):
                print("✅ 予測の自動修復成功")
                overall_severity = 0
            else:
                print("❌ 予測の自動修復失敗")

    if not args.quiet or all_issues:
        print(f"\n━━━━━━ Pre-flight check: {args.date} ━━━━━━")
        if all_issues:
            for iss in all_issues:
                print(f"  {iss}")
        else:
            print("✅ 全項目 OK")
        print(f"\n総合: {'✅ OK' if overall_severity == 0 else '⚠️ 警告' if overall_severity == 1 else '❌ 致命的'}")

    sys.exit(overall_severity)


if __name__ == "__main__":
    main()
