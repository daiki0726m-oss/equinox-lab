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

# 同 scripts ディレクトリの幽霊馬除去ロジックを再利用 (#43/#44 残存幽霊の検出)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# #150: このモジュールは長らく **git 未追跡** で、CI では ModuleNotFoundError となり
# preflight の全チェック (レース件数・出走馬・予測キャッシュ・ML flat 検知・幽霊馬) が
# 2026-06 以降ずっと丸ごと死んでいた (workflow 側が出力を握り潰していたため無音)。
# ファイルは追跡対象に入れたうえで、万一欠けても他のチェックまで巻き添えにしない。
try:
    import remove_ghost_horses as rgh  # noqa: E402
except ImportError as _e:
    rgh = None
    print(f"⚠️ remove_ghost_horses が読み込めません ({_e}) → 幽霊馬チェックのみ無効化")


# 想定値
# #169: 旧 EXPECTED_RACES_WEEKEND=30 (固定) は廃止。2場開催 (24R、開催日の約3割) では毎回
# 警告が出て本物の欠けが埋もれ、3場開催では最大6R欠けても無警告だった。実際に起きた欠け
# (2025-01-25 中山9R / 2024-11-02 京都8R / 2025-06-28 小倉1-2R) は1件も見分けられていない。
# JRA は1場1日12レースなので、場ごとに 1〜12R がそろっているかで照合する。
RACES_PER_VENUE = 12
EXPECTED_MIN_RUNNERS_PER_RACE = 5  # 障害戦等で少ない場合あり

# JRA の場コード (race_id の5-6桁目) → 場名。カレンダー (場名) と DB (race_id) を突き合わせる用
JRA_VENUE_CODES = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
                   "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}


def _calendar_venues(date_iso: str) -> list[str]:
    """docs/data/race_calendar.json がその日の開催場として記録している場名 (無ければ空)。"""
    import json as _json
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(repo, "docs", "data", "race_calendar.json"), encoding="utf-8") as f:
            info = (_json.load(f).get("dates") or {}).get(date_iso) or {}
        return list(info.get("venues") or [])
    except Exception:
        return []


def find_missing_races(conn, date_iso: str) -> tuple[dict, list[str], list[str]]:
    """場ごとに 1〜12R の欠番を探す (#169)。

    返り値: (venues, missing_ids, missing_venues)
      venues         : {場コード: そろっているレース番号の集合}
      missing_ids    : 欠けているレースの race_id (同じ場・同じ日のレースの race_id から組み立てる。
                       JRA の race_id は「年+場+回+日+R」なので、末尾2桁を替えれば欠番の ID になる)
      missing_venues : カレンダーには開催場とあるのに DB に1レースも無い場 (場名)
    """
    date_yyyymmdd = date_iso.replace("-", "")
    rows = conn.execute(
        "SELECT race_id FROM races WHERE race_date = ? OR race_date = ?",
        (date_iso, date_yyyymmdd),
    ).fetchall()
    venues: dict[str, set] = {}
    prefixes: dict[str, dict] = {}
    for r in rows:
        rid = str(r["race_id"])
        code = rid[4:6]
        if code not in JRA_VENUE_CODES or not rid[-2:].isdigit():
            continue                     # 地方競馬の残骸行などは対象外
        venues.setdefault(code, set()).add(int(rid[-2:]))
        prefixes.setdefault(code, {}).setdefault(rid[:10], 0)
        prefixes[code][rid[:10]] += 1
    missing_ids = []
    for code, nums in sorted(venues.items()):
        prefix = max(prefixes[code].items(), key=lambda kv: kv[1])[0]
        for n in range(1, RACES_PER_VENUE + 1):
            if n not in nums:
                missing_ids.append(f"{prefix}{n:02d}")
    if missing_ids:
        # 別の日付で登録済みの ID は欠けではない (台風の順延で一部のレースだけ翌日に回った日など。
        # race_id は元の開催日のまま)。取り直しても日付は変わらないので auto-fix の対象から外す。
        # race_date が空の行 (週中の出馬表取得が作るスタブ) は欠けとして扱い、取り直しで日付を埋める
        q = ",".join("?" * len(missing_ids))
        elsewhere = {r[0] for r in conn.execute(
            f"SELECT race_id FROM races WHERE race_id IN ({q}) AND COALESCE(race_date, '') != '' "
            f"AND race_date NOT IN (?, ?)",
            (*missing_ids, date_iso, date_yyyymmdd))}
        missing_ids = [m for m in missing_ids if m not in elsewhere]
    have = {JRA_VENUE_CODES[c] for c in venues}
    missing_venues = [v for v in _calendar_venues(date_iso) if v not in have]
    return venues, missing_ids, missing_venues


def check_races(conn, date_iso: str) -> tuple[int, list[str], list[str]]:
    """races テーブルにその日のレースがそろっているか (#169: 開催場数×12、場ごとに欠番検査)。

    返り値: (severity, issues, missing_ids)。欠けは致命的 (2) として auto-fix の取得を起動する。
    """
    issues = []
    row = conn.execute(
        "SELECT COUNT(*) as c FROM races WHERE race_date = ?", (date_iso,)
    ).fetchone()
    n_races = row["c"] or 0
    if n_races == 0:
        issues.append(f"❌ races テーブルに {date_iso} のレースが 1件もない")
        return 2, issues, []
    venues, missing_ids, missing_venues = find_missing_races(conn, date_iso)
    n_expected = RACES_PER_VENUE * (len(venues) + len(missing_venues))
    if missing_ids:
        by_v: dict[str, list[int]] = {}
        for rid in missing_ids:
            by_v.setdefault(JRA_VENUE_CODES.get(rid[4:6], rid[4:6]), []).append(int(rid[-2:]))
        detail = " / ".join(f"{v} {','.join(str(n) for n in ns)}R" for v, ns in by_v.items())
        issues.append(f"❌ レースの欠け {len(missing_ids)}件 ({n_races}/{n_expected}R): {detail}")
    if missing_venues:
        issues.append(f"❌ カレンダーでは開催場なのに DB に1レースも無い場: "
                      f"{'・'.join(missing_venues)}")
    return (2 if (missing_ids or missing_venues) else 0), issues, missing_ids


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


def check_ml_output_sanity(conn, date_iso: str) -> tuple[int, list[str]]:
    """ML 出力の正常性 (#41 sanity gate, 2026-06-07).

    今日 6/7 に発生した「24R 全 D + ◎勝率 5-13% (均等近傍)」事故の再発防止。
    各レースの max(pred_win_pct) と確率分布から「ML が flat に近い出力」を検出。

    判定: 50% 以上のレースで max(pred_win_pct) < 12% なら critical (exit 2)。
          30% 以上なら warning (exit 1)。
    """
    import json as _json
    issues = []
    rows = conn.execute(
        """SELECT pc.race_id, pc.predictions_json
           FROM predictions_cache pc
           JOIN races r ON pc.race_id = r.race_id
           WHERE r.race_date = ?""",
        (date_iso,)
    ).fetchall()
    if not rows:
        return 0, []  # 予測なしは別の check で扱う

    n_total = len(rows)
    n_flat = 0
    flat_details = []
    for r in rows:
        try:
            preds = _json.loads(r["predictions_json"])
        except Exception:
            continue
        if not preds:
            continue
        # #95: flat 判定は表示チャネル優先 (12% 閾値は温度×3分布とペア、database.py と同一ロジック)
        max_pw = max((p.get("pred_win_display_pct") or p.get("pred_win_pct") or 0) for p in preds)
        if max_pw < 12.0:
            n_flat += 1
            flat_details.append((r["race_id"], max_pw))

    flat_ratio = n_flat / n_total if n_total else 0
    if flat_ratio >= 0.5:
        sample = ", ".join(f"{rid}({mx:.1f}%)" for rid, mx in flat_details[:3])
        issues.append(
            f"❌ ML flat output 検出: {n_flat}/{n_total} レース ({flat_ratio*100:.0f}%) "
            f"で max ◎勝率<12% — 例: {sample}. seal せず再 predict 必須"
        )
        return 2, issues
    elif flat_ratio >= 0.3:
        sample = ", ".join(f"{rid}({mx:.1f}%)" for rid, mx in flat_details[:3])
        issues.append(
            f"⚠️ ML 部分 flat: {n_flat}/{n_total} レース ({flat_ratio*100:.0f}%) "
            f"で max ◎勝率<12% — 例: {sample}"
        )
        return 1, issues
    return 0, []


def check_v2d_premise(conn, date_iso: str) -> list[str]:
    """V2d (馬場差を引いたスピード指数, #168) の前提が欠けた過去走を数える (#169, 表示のみ)。

    V2d は過去走の「その日・その場の 1〜12R が DB にそろい、その芝ダの平地レースが全部
    1〜3着の走破時計を持つ」日だけ馬場差 δ̂ を使い、そろわない日は旧指数で埋める
    (ml/speed_variant.race_table の complete)。これを監視する仕組みが無く、1レース欠けただけで
    その日の約150走が旧指数になっても誰も気づけなかった (今週 10/31・11/01 は 4走 = 0.21%)。
    推論と同じ race_table で判定し、該当があればログに出すだけ。印・severity は変えない
    (中止された日のように取り直しても直らない欠けがあり、毎回警告にすると #169 の
    「期待30件」と同じく本物が埋もれるため)。
    """
    try:
        import pandas as pd
        from ml import speed_variant as sv
    except Exception as e:  # pandas の無い環境では検査しない
        return [f"ℹ️ V2d の前提検査をスキップ ({e})"]
    try:
        fit_from = (sv.load_table() or {}).get("fit_from", sv.FIT_FROM)
    except Exception:
        fit_from = sv.FIT_FROM
    hids = [r[0] for r in conn.execute(
        "SELECT DISTINCT re.horse_id FROM results re JOIN races ra ON ra.race_id = re.race_id "
        "WHERE ra.race_date = ? AND COALESCE(re.horse_id, '') != ''", (date_iso,))]
    if not hids:
        return []
    runs = []   # (horse_id, race_id, race_date, venue) — DbSiVariant.row_features と同じ集め方
    for hid in hids:
        for r in conn.execute(
                "SELECT r.race_id, ra.race_date, ra.venue FROM results r "
                "JOIN races ra ON r.race_id = ra.race_id "
                "WHERE r.horse_id = ? AND r.finish_time_seconds > 0 AND ra.race_date < ? "
                "ORDER BY ra.race_date DESC LIMIT ?", (hid, date_iso, sv.WINDOW)):
            runs.append((hid, r[0], r[1], r[2]))
    if not runs:
        return []
    days = {(d, v) for _, _, d, v in runs}
    cols = ["race_id", "race_date", "venue", "surface", "distance", "grade", "race_name", "race_number"]
    race_rows = []
    dates = sorted({d for d, _ in days})
    for i in range(0, len(dates), 300):
        chunk = dates[i:i + 300]
        q = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT {', '.join(cols)} FROM races WHERE race_date IN ({q})", chunk):
            if (r[1], r[2]) in days:
                race_rows.append(tuple(r))
    races_df = pd.DataFrame(race_rows, columns=cols)
    ids = races_df["race_id"].tolist()
    res_rows = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        res_rows += [tuple(r) for r in conn.execute(
            f"SELECT r.race_id, r.horse_number, r.finish_position, r.finish_time_seconds, h.birth_year "
            f"FROM results r LEFT JOIN horses h ON r.horse_id = h.horse_id WHERE r.race_id IN ({q})", chunk)]
    results_df = pd.DataFrame(res_rows, columns=["race_id", "horse_number", "finish_position",
                                                 "finish_time_seconds", "birth_year"])
    rt = sv.race_table(races_df, results_df, apply_from=fit_from)
    bad = rt[rt["flat"] & ~rt["complete"]]
    if bad.empty:
        return []
    bad_ids = set(bad["race_id"])
    hit = [x for x in runs if x[1] in bad_ids]
    if not hit:
        return []
    # 日ごとの理由 (欠番 / 1〜3着の時計が無いレース)
    reasons = []
    for (d, v) in sorted({(x[2], x[3]) for x in hit}):
        g = rt[(rt["race_date"] == d) & (rt["venue"] == v)]
        nums = sorted(int(n) for n in g["race_number"])
        miss = sorted(set(range(1, sv.FULL_CARD + 1)) - set(nums))
        why = []
        if miss or len(nums) != sv.FULL_CARD:
            why.append(f"欠番 {','.join(str(n) for n in miss) or '-'}R ({len(nums)}R)")
        no_t3 = g[g["flat"] & ~g["has_t3"]]
        if len(no_t3):
            why.append("1〜3着の時計なし " + ",".join(f"{int(n)}R" for n in no_t3["race_number"]))
        n_runs = sum(1 for x in hit if x[2] == d and x[3] == v)
        reasons.append(f"{d} {v} ({n_runs}走): {' / '.join(why) or '判定不能'}")
    lines = [f"⚠️ V2d の前提が欠けた過去走 {len(hit)}/{len(runs)}走 "
             f"({len({x[0] for x in hit})}頭) — この走は旧指数で埋めて計算 (印は変えない #168)"]
    lines += [f"     {r}" for r in reasons[:10]]
    if len(reasons) > 10:
        lines.append(f"     … ほか {len(reasons) - 10}日")
    return lines


def check_ghost_horses(conn, date_iso: str, use_api: bool = False) -> tuple[int, list[str], bool]:
    """幽霊馬 (枠順確定前の仮馬番が finish=0 で残存) を検出 (#43/#44)。

    基準(A) 同一レース内 horse_id 重複: 常時チェック (ネットワーク不要・無料)。
            #43 の幽霊は実馬の horse_id を重複させるためこれで大半を検出できる。
    基準(B) DB 出走頭数 vs netkeiba API 実頭数の照合: use_api=True (--ghost-api) 時のみ。
            未開催レース限定で API を呼び、余剰行を検出する。高頻度 cron での
            API 連打を避けるため既定 OFF。

    🚨 finish_position=0 は除外/取消/競走中止の正規値でもあるため、開催済レースの
       finish=0 行は基準(A)の重複以外は触らない (remove_ghost_horses の安全設計に準拠)。

    返り値: (severity, issues, detected)。detected=True なら auto-fix 対象。
    """
    issues: list[str] = []
    if rgh is None:
        return 0, ["ℹ️ 幽霊馬チェックはスキップ (remove_ghost_horses 未配置)"], False
    date_yyyymmdd = date_iso.replace("-", "")
    races = conn.execute(
        "SELECT race_id, venue, race_number FROM races WHERE race_date = ? OR race_date = ?",
        (date_iso, date_yyyymmdd),
    ).fetchall()
    if not races:
        return 0, issues, False

    flagged = []  # (race, n_ghost, n_db_rows, api_count)
    for race in races:
        rid = race["race_id"]
        rows = rgh._race_rows(conn, rid)
        if not rows:
            continue
        has_finished = any((r.get("finish_position") or 0) > 0 for r in rows)

        api_nums = None
        if use_api and not has_finished:
            api_nums = rgh.fetch_real_horse_numbers(rid)

        ghosts_a, _amb = rgh.detect_criterion_a(rows, api_nums)
        already = {g["horse_number"] for g in ghosts_a}
        ghosts_b = []
        if use_api and not has_finished:
            ghosts_b = rgh.detect_criterion_b(rows, api_nums, already)

        n_ghost = len(ghosts_a) + len(ghosts_b)
        if n_ghost:
            flagged.append((race, n_ghost, len(rows), len(api_nums) if api_nums else None))

    if flagged:
        detail = ", ".join(
            f"{r['venue']}{r['race_number']}R(DB{nr}頭"
            + (f"/API{an}頭" if an is not None else "")
            + f"/幽霊{n})"
            for r, n, nr, an in flagged
        )
        issues.append(f"⚠️ 幽霊馬の疑い {len(flagged)}レース: {detail}")
        return 1, issues, True
    return 0, issues, False


def _is_today_or_future(date_yyyymmdd: str) -> bool:
    """対象日が JST の今日以降か。"""
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    return date_yyyymmdd >= today


def _ensure_entries(date_yyyymmdd: str) -> bool:
    """予測前の確定馬番の取り直し (#169)。判定は refresh_entries.ensure_confirmed_entries の1か所。"""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo not in sys.path:
        sys.path.insert(0, repo)
    try:
        from refresh_entries import ensure_confirmed_entries
    except Exception as e:
        print(f"🚫 refresh_entries を読み込めない ({e}) → 予測しない (#169)")
        return False
    return ensure_confirmed_entries(date_yyyymmdd)


def auto_fix_missing_races(date_yyyymmdd: str, missing_ids: list[str]) -> bool:
    """欠けているレースだけを取り直す (#169)。

    同じ場・同じ日の race_id から欠番の ID を組み立てて、結果ページ → 出馬表の順に取る
    (発走済みなら着順、未発走なら出走馬が入る)。一覧が取りこぼす日 (#161 の 9/21) でも、
    ID を直接引くので一覧に依存しない。返り値: 1件以上取れたか。
    """
    import time as _time
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from scraper import NetkeibaScraper
    print(f"🔧 自動修復: 欠けているレース {len(missing_ids)}件を取得中 ({date_yyyymmdd})...")
    s = NetkeibaScraper()
    got = 0
    for rid in missing_ids:
        try:
            data = s.scrape_race_result(rid)
            if data and data.get("results"):
                if not data.get("race_date"):
                    data["race_date"] = f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:8]}"
                s.save_race_to_db(data)
                got += 1
            else:
                data = s.scrape_shutuba(rid, race_date_hint=date_yyyymmdd)
                if data and data.get("entries"):
                    s.save_race_to_db(data)
                    got += 1
                else:
                    print(f"  ⚠️ {rid}: 結果・出馬表とも取れない (中止の可能性)")
        except Exception as e:
            print(f"  ⚠️ {rid}: {e}")
        _time.sleep(1.0)
    print(f"  → {got}/{len(missing_ids)}件を取得")
    return got > 0


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
        data = s.scrape_shutuba(rid, race_date_hint='{date_yyyymmdd}')  # #169: 日付を空で保存しない
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
    """予測キャッシュ不足を自動修復。

    #150c: **既に印を投稿した日は --force を付けない**。
    race_day_runner は 10分ごとに約50回この preflight を回すため、
    予測欠落や ML flat を検知すると 10:15 の投稿後でも --force で全レースを
    再予測しうる。それは「投稿時点で凍結した印」(#99/#101) を午後に
    書き換えることになり、公開済みの予想と結果報告の印が食い違う。
    投稿記録 (posted_marks サイドカー = clobber を生き延びるテキスト正本) が
    あれば、欠落レースの追加生成だけに留める。
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # #169: 過去日は予測しない。日付をまたいで遅れて発火した run (11/01 00:24 の
    # refresh_dashboard) や手動の過去日 preflight で、発走後に予測を作り直すと
    # 後出しの印になる (#99 の look-ahead)。過去日の予測を作り直すのは人の判断で行う。
    if not _is_today_or_future(date_yyyymmdd):
        print(f"⏭️ {date_yyyymmdd} は過去日 → 予測の自動修復はしない (後出しの印を作らない #99/#169)")
        return False
    # #169: 予測の直前に確定馬番を取り直し、失敗したら予測しない (既存の予測を残す)。
    # 11/01 00:35 はここを通らずに仮馬番のまま23レースを予測・公開した。
    if not _ensure_entries(date_yyyymmdd):
        print(f"🚫 {date_yyyymmdd}: 確定馬番を取り直せない → 予測しない (既存の予測を残す)")
        return False
    posted = os.path.join(repo, "docs", "data", f"posted_marks_{date_yyyymmdd}.json")
    cmd = ["python3", "predict.py", "predict", "--date", date_yyyymmdd]
    if os.path.exists(posted):
        print(f"🔒 {date_yyyymmdd} は印を投稿済み → --force を外して欠落分のみ生成")
    else:
        cmd.append("--force")
    print(f"🔧 自動修復: 予測実行中 ({date_yyyymmdd})...")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=900
        )
        if result.returncode != 0:
            print(f"❌ 予測失敗: {result.stderr[-200:]}")
            return False
        print("✅ 予測完了")
        return True
    except Exception as e:
        print(f"❌ 予測例外: {e}")
        return False


def auto_fix_ghosts(date_yyyymmdd: str) -> bool:
    """幽霊馬を除去し、該当日を再予測+再export (remove_ghost_horses 経由)。"""
    print(f"🔧 自動修復: 幽霊馬を除去中 ({date_yyyymmdd})...")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "remove_ghost_horses.py")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.exists(script):
        print(f"❌ {script} が存在しないため幽霊馬の自動修復ができません")
        return False
    try:
        result = subprocess.run(
            ["python3", script, "--date", date_yyyymmdd, "--apply", "--repredict"],
            cwd=repo, capture_output=True, text=True, timeout=900,
        )
        print(result.stdout[-800:])
        # remove_ghost_horses 返り値: 0=幽霊なし / 1=検出して削除 (どちらも正常)
        if result.returncode in (0, 1):
            return True
        print(f"❌ 幽霊除去失敗: {result.stderr[-200:]}")
        return False
    except Exception as e:
        print(f"❌ 幽霊除去例外: {e}")
        return False


def main():
    ap = argparse.ArgumentParser(description="データ完整性 pre-flight check")
    ap.add_argument("date", help="YYYYMMDD")
    ap.add_argument("--auto-fix", action="store_true", help="不足を自動修復")
    ap.add_argument("--quiet", action="store_true", help="OK時は出力しない")
    ap.add_argument("--ghost-api", action="store_true",
                    help="幽霊馬チェックで netkeiba オッズ API も照合 (基準B)。"
                         "高頻度 cron での連打を避けるため既定 OFF。"
                         "OFF でも基準A(horse_id重複)は常時チェック")
    args = ap.parse_args()

    if len(args.date) != 8:
        print(f"❌ 日付フォーマット不正: {args.date}", file=sys.stderr)
        sys.exit(2)

    date_iso = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:8]}"
    init_db()

    all_issues = []
    overall_severity = 0  # 0=OK, 1=warn, 2=critical

    with get_db() as conn:
        # 1. races (#169: 場ごとに 1〜12R の欠番を検査)
        sev, issues, missing_race_ids = check_races(conn, date_iso)
        overall_severity = max(overall_severity, sev)
        all_issues.extend(issues)
        races_critical = sev == 2

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

        # 3.5. ML 出力 sanity (#41, 2026-06-07): flat / degenerate prediction の早期検知
        # 「24R 全 D + ◎勝率 5-13%」のような ML 異常を seal 前に検出して
        # auto-fix の re-predict を発動させる
        sev, issues = check_ml_output_sanity(conn, date_iso)
        overall_severity = max(overall_severity, sev)
        all_issues.extend(issues)
        ml_critical = sev == 2

        # 4. 幽霊馬 (#43/#44) — 基準A は無料・常時、基準B(API) は --ghost-api 時のみ
        sev, issues, ghost_detected = check_ghost_horses(conn, date_iso, use_api=args.ghost_api)
        overall_severity = max(overall_severity, sev)
        all_issues.extend(issues)

        # 5. V2d の前提 (#169): 表示のみ。10分ごとの runner (--quiet) では毎回同じ内容に
        #    なるので省き、予測・投稿の回 (--quiet なし) で出す
        v2d_notes = [] if args.quiet else check_v2d_premise(conn, date_iso)

    # Auto-fix
    races_fixed = False
    if args.auto_fix and overall_severity == 2:
        print(f"\n🛠 Auto-fix mode: 致命的問題を修復します...\n")
        if races_critical and missing_race_ids:
            # #169: 欠番は ID を組み立てて直接取る。取り直しても欠けたままなら
            # 中止の可能性があるので警告に下げる (欠け1レースで終日の投稿を止めない)
            auto_fix_missing_races(args.date, missing_race_ids)
            with get_db() as conn:
                sev_r, _issues_r, still = check_races(conn, date_iso)
            races_fixed = len(still) < len(missing_race_ids)
            if sev_r == 2 and still:
                print(f"⚠️ 取り直しても {len(still)}件が欠けたまま (中止の可能性) → 警告扱い")
                all_issues.append(f"⚠️ 取り直しても欠けたまま (中止の可能性): {', '.join(still)}")
                if not (runners_critical or pred_critical or ml_critical):
                    overall_severity = 1
            elif sev_r == 0:
                all_issues.append("✅ 欠けていたレースを取得")
                if not (runners_critical or pred_critical or ml_critical):
                    overall_severity = 0
        elif races_critical:
            # 場ごと欠けている (カレンダーにはある) → 一覧から全レース取り直す
            runners_critical = True
        if runners_critical:
            if auto_fix_runners(args.date):
                print("✅ 出走馬の自動修復成功")
            else:
                print("❌ 出走馬の自動修復失敗")
        if pred_critical or runners_critical or ml_critical or races_fixed:
            # 出走馬入ったら予測も再実行。ML flat output も再 predict で修復試行
            if ml_critical:
                print("🔄 ML flat output → predict --force で再予測")
            if auto_fix_predictions(args.date):
                print("✅ 予測の自動修復成功")
                overall_severity = 0
            else:
                print("❌ 予測の自動修復失敗")

    # 幽霊馬 auto-fix (警告レベルでも除去する。除去後は該当日を再予測+再export)
    if args.auto_fix and ghost_detected:
        print(f"\n🛠 Auto-fix mode: 幽霊馬を除去します...\n")
        if auto_fix_ghosts(args.date):
            print("✅ 幽霊馬の自動修復成功")
        else:
            print("❌ 幽霊馬の自動修復失敗")

    if not args.quiet or all_issues:
        print(f"\n━━━━━━ Pre-flight check: {args.date} ━━━━━━")
        if all_issues:
            for iss in all_issues:
                print(f"  {iss}")
        else:
            print("✅ 全項目 OK")
        for note in v2d_notes:
            print(f"  {note}")
        print(f"\n総合: {'✅ OK' if overall_severity == 0 else '⚠️ 警告' if overall_severity == 1 else '❌ 致命的'}")

    sys.exit(overall_severity)


if __name__ == "__main__":
    main()
