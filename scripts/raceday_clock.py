#!/usr/bin/env python3
"""土日以外の開催日 (月曜祝日・台風順延) の定刻の起動 (#179)。

背景:
  土日の朝の処理 (予測の作り直し 04:00〜07:40・race_day_runner の起動・前夜予想) は、どれも
  曜日で書いた cron (UTC の '5,6' など) に乗っている。土日以外の開催日には自前の時計が無く、
  posting_watchdog の */30 の中のセーフティネット1本 (#160/#161) だけが頼りだった。
  2026-11-23(月・祝) は GitHub がその watchdog を5時間以上飛ばし、07:07 に手で dispatch する
  まで予測がゼロだった。1本の cron に全部を賭ける構造が原因。

仕組み:
  raceday_clock.yml が土日と同じ時刻 (分だけ数分ずらす) を中心に35本の cron を持ち、各 tick がこの
  スクリプトを数秒走らせる。対象日 T が「土日以外の開催日」と race_calendar.json が言う時だけ、
  本体 (auto_post_x / race_day_runner / weekend_prefetch / collect_results) を mode と日付を
  明示して dispatch する。非開催日は何も出さずに終わる。本体の cron 表は1行も変えない
  (= 土日の挙動は構造的に不変)。セーフティネットは見張り役として残る。

  - 対象日は「起動した cron の直近の予定時刻」から決める (実行時刻ではない)。cron は数時間
    遅れる (#22) ので、23:41 の前夜 tick が翌 00:30 に来ても対象日は翌日 (= その夜の翌日) のまま。
  - 窓・lock・待機の判定はセーフティネットと同じ道具 (raceday_safety_net の関数) を使う。
    二重投稿の最終防止は post_x の lock (#141)。ここは無駄打ちを減らすだけ。

使い方 (テスト・ローカル):
  python3 scripts/raceday_clock.py --schedule '3 19 * * 0-4' --now 2026-11-23T04:05 \\
      --calendar <cal.json> --slots-dir <dir> --fake-gh '{}' --dry-run
  workflow は env (CLOCK_SCHEDULE / CLOCK_INTENT / CLOCK_DATE / CLOCK_DRY_RUN) を渡す。
  --fake-gh を渡した時は netkeiba に出ない (確かめる HTML は --fake-gh の "race_list" で渡す)。
終了コード: 0 = 正常 (何もしない・縮退を含む) / 1 = 例外・dispatch 失敗
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

JST = dt.timezone(dt.timedelta(hours=9))
UTC = dt.timezone.utc

# cron (UTC) → (intents, 対象日の offset [予定時刻の JST 日 + offset])。唯一の対応表。
# raceday_clock.yml の schedule と scripts/test_raceday_clock.py で突き合わせる。
# 曜日欄: JST の 00:00-08:59 は前日の UTC なので JST 月〜金 = UTC '0-4'、09:00 以降は '1-5'。
# 前夜 (JST 日〜木の 15:41/19:41/23:41) は同日の UTC で '0-4'。
# → この時計が JST の土・日に動くのは「日曜の前夜3本」だけ (翌月曜が開催日の時しか何もしない)。
#
# #179 レビュー (M1): 時刻表は「定刻に当てる」でなく「遅れて着いても窓に入る」前提で組む。
#   このリポジトリでは 11/22-23 に schedule が一律 4〜8時間遅れた (auto_post_x の 07:00 cron が 11:03、
#   runner の 08:30 cron が 12:21、collect_results の 20:00 cron が翌 03:10)。時計の tick も同じ
#   スケジューラに乗るので同じだけ遅れる。土日の写しだけだと 23:41 〜 03:37 に tick が無く、
#   一律 6:00〜8:45 の遅れでは 9:30 前に DB の予測も runner の起動も無かった (遅れ掃引で確認)。
#   そこで当日分の predict を深夜 00:07/01:07/02:07 にも置く。decide() は予定時刻で対象日を決め、
#   窓は実行時刻で判定するので、6時間遅れて 06:07 に着いても当日の predict として働く。定刻に着いた
#   場合も predict の段階 (refresh_entries / オッズ充足ゲート #52) が扱い、非開催日はカレンダーで何もしない。
#   修正前は 4h45m 以上の一律遅延で時計の predict が0本 (runner の起動も 8h 遅れで 11:37)。追加後は一律
#   〜8h40m の遅れまで 8:48 の締切前に predict を出せ、runner も早く起きる (8h 遅れで 09:07 → T-clock-3 の tick で 08:37。test_raceday_clock.py §9)。
#   odds_flash の tick は 09:01 の1本 (窓 9:00〜9:15)。一律の遅れでこれを外した日は race_day_runner の代打
#   (trigger_post odds_flash) が頼りだが、runner の判定は起動から ~8〜14分後 (起動29秒 + 1周目 436秒、
#   11/07 は 763秒。11/23 本番ログ実測) で、代打の締切は 9:15 (race_utils.ODDS_FLASH_LATEST_HM)。
#   → runner を **09:01 までに** 起こす tick が要る。decide() は当日 8:30〜19:00 に着いた tick なら intent に
#   関係なく runner を確認するので、#179 T-clock-3 で「runner だけ」の tick を足し、23:41 〜 08:53 の
#   予定時刻の間隔をすべて30分以下にした (8:30〜9:01 は31分の窓 = 一律 0〜9h19m のどの遅れでも1本入る)。
#   旧表は間隔が 50〜90分あり、一律 0〜9h の遅れの約4割 (34〜46分・124〜176分・330〜382分 等) で
#   runner の起動が 9:07〜9:41 になり、odds_flash (9:00 の最後の作り直しを兼ねる) がどこからも出なかった。
#   足した tick は定刻に着けば 8:30 前なので何もしない (DB も触らない。数秒で終わる)。
#   それより大きい一律遅延には、同じスケジューラに乗るこの時計は勝てない (独立した時計は GAS だけ #155)。
SCHEDULE = {
    "41 6 * * 0-4": (("prefetch_eve",), 1),            # 日〜木 15:41 前夜予想 (翌日分)
    "41 10 * * 0-4": (("prefetch_eve",), 1),           # 日〜木 19:41
    "41 14 * * 0-4": (("prefetch_eve",), 1),           # 日〜木 23:41
    "7 15 * * 0-4": (("predict",), 0),                 # 月〜金 00:07 当日分の predict (遅延対策 M1)
    "7 16 * * 0-4": (("predict",), 0),                 # 月〜金 01:07
    "7 17 * * 0-4": (("predict",), 0),                 # 月〜金 02:07
    "37 18 * * 0-4": (("prefetch_early",), 0),         # 月〜金 03:37 当日分の前夜予想
    "3 19 * * 0-4": (("predict",), 0),                 # 月〜金 04:03
    "33 20 * * 0-4": (("predict",), 0),                # 05:33
    "3 22 * * 0-4": (("predict",), 0),                 # 07:03
    "43 22 * * 0-4": (("predict",), 0),                # 07:43
    # #179 T-clock-3: 遅れて 8:30〜9:01 に着いて runner を起こすための tick (定刻なら何もしない)。
    #   上と合わせて 23:41 〜 08:53 の予定の間隔を30分以下にする (理由は上の odds_flash の注)。
    "37 15 * * 0-4": (("runner",), 0),                 # 月〜金 00:37
    "37 16 * * 0-4": (("runner",), 0),                 # 01:37
    "37 17 * * 0-4": (("runner",), 0),                 # 02:37
    "7 18 * * 0-4": (("runner",), 0),                  # 03:07
    "33 19 * * 0-4": (("runner",), 0),                 # 04:33
    "3 20 * * 0-4": (("runner",), 0),                  # 05:03
    "3 21 * * 0-4": (("runner",), 0),                  # 06:03
    "33 21 * * 0-4": (("runner",), 0),                 # 06:33
    "23 22 * * 0-4": (("runner",), 0),                 # 07:23
    "3 23 * * 0-4": (("runner",), 0),                  # 08:03
    "33 23 * * 0-4": (("runner",), 0),                 # 08:33
    "53 23 * * 0-4": (("runner",), 0),                 # 08:53
    "1 0 * * 1-5": (("runner", "odds_flash"), 0),      # 09:01
    "31 0 * * 1-5": (("runner", "post_predict"), 0),   # 09:31
    "51 0 * * 1-5": (("runner", "post_predict"), 0),   # 09:51
    "7 1 * * 1-5": (("runner",), 0),                   # 10:07
    "7 2 * * 1-5": (("runner",), 0),                   # 11:07
    "7 4 * * 1-5": (("runner",), 0),                   # 13:07
    "7 6 * * 1-5": (("runner",), 0),                   # 15:07
    "33 8 * * 1-5": (("runner", "results"), 0),        # 17:33
    # #179 レビュー (m3): results は 17:33 の1本と runner の1回 (dispatch 成功で済み扱い) だけだと、
    #   待機中に別の run (開催日の月曜にも動く平日の weekday/evening cron) に取り消された時に出し直す
    #   経路が無い。lock と完走ガードがあるので何本出しても冪等 → 取り消しは次の tick が拾う。
    "33 10 * * 1-5": (("results",), 0),                # 19:33 results (再試行)
    "33 12 * * 1-5": (("results",), 0),                # 21:33 results (再試行)
    "7 11 * * 1-5": (("sweep",), 0),                   # 20:07 夜間スイープ
    # #179 レビュー (m2): 2本目は 20:07 から 160分空ける (月祝は collect_results 自身の月曜 cron 20:00 が
    #   1本目になるので、22:07 だと 127分で必ず見送っていた)。
    "47 13 * * 1-5": (("sweep",), 0),                  # 22:47
}
INTENTS = ("prefetch_eve", "prefetch_early", "predict", "runner", "odds_flash",
           "post_predict", "results", "sweep")
# #179 T-clock-2: カレンダーが「開催日でない」と言っても、作り直し (feature_parity 毎朝 05:00 / fetch_weekend_races)
#   より後に決まった平日の開催 (台風の順延など) は載っていない。作り直しは GitHub の遅れで 09:36・10:29 着の実測が
#   あり (11/23)、それを待つと predict の締切 (8:48)・9:30 の確定を過ぎる。そこで平日の対象日にカレンダーが
#   新しい否定 (False) を返した時だけ、その1日の netkeiba のレース一覧を引いて確かめる (1 tick 1 リクエスト)。
#   取れない・JRA のレースが無い → カレンダーの判定どおり何もしない。カレンダーは書き換えない (次の作り直しで載る)。
#   この workflow は sparse checkout で pip も入れないので標準ライブラリ (urllib) だけで引く。
PROBE_INTENTS = ("prefetch_eve", "prefetch_early", "predict", "runner", "odds_flash", "post_predict")
RACE_LIST_URL = "https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={d8}"
NETKEIBA_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")   # scraper.NetkeibaScraper.HEADERS と同じ
MANUAL_OFFSET = {"prefetch_eve": 1}
PREDICT_RUN_MIN = 12          # predict の run の所要 (fetch の chain #173 と同じ) — 9:00 の12分前で締める
EARLY_TOLERANCE_MIN = 10      # cron が予定より早く起きた時の許容
MAX_DELAY_H = 20              # これより遅れた tick は対象日を決められないので何もしない
SWEEP_MIN_GAP = 100           # 夜間スイープは前回から100分以上空ける (1晩2回まで)
# #179 レビュー (m2): セーフティネットの150分は */30 の watchdog 向けの値。この時計の固定 tick は
#   20:07 と 22:47 (160分間隔) なので、150分だと 1本目が30分遅れただけで2本目が必ず見送りになる。


def log(msg=""):
    print(msg, flush=True)


# ── 予定時刻 ─────────────────────────────────────────────────────
def _dow_set(field):
    """cron の曜日欄 ('0-4' / '1-5' / '*' / '6,0') → 曜日の集合 (cron の数え方 0=日 … 6=土)。"""
    if field.strip() == "*":
        return set(range(7))
    out = set()
    for part in field.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            out.update(range(a, b + 1))
        elif part:
            out.add(int(part))
    return {x % 7 for x in out}


def planned_fire(cron, now_utc):
    """起動した cron の直近の予定時刻 (UTC)。cron は早くは起きないが、念のため10分の前倒しを許す。
    曜日欄に合うまで1日ずつ戻す。読めなければ None。"""
    try:
        mi, hh, _dom, _mon, dow = cron.split()
        allowed = _dow_set(dow)
        cand = now_utc.astimezone(UTC).replace(hour=int(hh), minute=int(mi), second=0, microsecond=0)
    except Exception:
        return None
    if cand > now_utc + dt.timedelta(minutes=EARLY_TOLERANCE_MIN):
        cand -= dt.timedelta(days=1)
    for _ in range(8):
        if (cand.weekday() + 1) % 7 in allowed:      # Python 月=0 → cron 月=1
            return cand
        cand -= dt.timedelta(days=1)
    return None


def final_rebuild_hm():
    """predict.FINAL_REBUILD_JST (当日の最後の作り直し #173) を HHMM の整数で。predict は import しない
    (lightgbm 等が要る) — fetch_weekend_races の chain と同じく正規表現で読む。読めなければ 900。"""
    try:
        with open(os.path.join(ROOT, "predict.py"), encoding="utf-8") as f:
            m = re.search(r"^FINAL_REBUILD_JST\s*=\s*\(\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)", f.read(), re.M)
        if m:
            return int(m.group(1)) * 100 + int(m.group(2))
    except OSError:
        pass
    log("::warning::#179: predict.FINAL_REBUILD_JST を読めない → 9:00 とみなす")
    return 900


def _minus_min(hm, minutes):
    t = (hm // 100) * 60 + hm % 100 - minutes
    return (t // 60) * 100 + t % 60


# ── netkeiba で1日だけ確かめる (#179 T-clock-2) ─────────────────────
def race_ids_from_list_html(text):
    """race_list_sub.html の HTML から race_id (12桁) を拾う。scraper.get_race_list_by_date と同じ規則
    (result.html / shutuba.html / shutuba_debut.html のリンクだけ。movie.html 等は数えない)。"""
    out = set()
    for href in re.findall(r'href="([^"]+)"', text or ""):
        if re.search(r"/(?:result|shutuba|shutuba_debut)\.html", href):
            m = re.search(r"race_id=(\d{12})", href)
            if m:
                out.add(m.group(1))
    return sorted(out)


def judge_race_list(text):
    """レース一覧の HTML → (True/False, 理由)。JRA の場コード 01-10 のレースがあれば True (build_race_calendar と同じ判定)。"""
    from build_race_calendar import jra_summary
    n, venues = jra_summary(race_ids_from_list_html(text))
    if n:
        return True, f"netkeiba のレース一覧に JRA の {n}R ({'・'.join(venues)})"
    return False, "netkeiba のレース一覧に JRA のレースが無い"


def probe_race_day(gh, t8):
    """対象日 t8 を netkeiba のレース一覧で確かめる。(True/False/None, 理由)。None = 取れない。"""
    try:
        text, why = gh.race_list_html(t8)
    except Exception as e:
        return None, f"netkeiba に問い合わせられない ({type(e).__name__})"
    if text is None:
        return None, why
    return judge_race_list(text)


def probe_worth(intents, target, now, final_hm):
    """この tick がこの時刻に何かを出しうるか (= netkeiba に問い合わせる価値があるか)。定刻に着いた
    runner だけの tick (8:30 前) などは問い合わせない。判定の本体は decide (ここは窓の粗い写し)。"""
    from race_utils import PREDICTION_LOCK_HM, ODDS_FLASH_LATEST_HM
    hm = now.hour * 100 + now.minute
    same_day = now.date() == target
    cutoff = dt.datetime.combine(target, dt.time(final_hm // 100, final_hm % 100), JST)
    for i in intents:
        if i == "runner" and same_day and 830 <= hm < 1900:
            return True
        if i in ("prefetch_eve", "prefetch_early") and now < cutoff:
            return True
        if i == "predict" and same_day and hm < _minus_min(final_hm, PREDICT_RUN_MIN):
            return True
        if i == "odds_flash" and same_day and 900 <= hm < ODDS_FLASH_LATEST_HM:
            return True
        if i == "post_predict" and same_day and PREDICTION_LOCK_HM <= hm < 1155:
            return True
    return False


# ── 判定 (純粋関数) ───────────────────────────────────────────────
def decide(intents, target, now, locks, gh, final_hm):
    """1 tick で出す dispatch を決める。戻り値 [(workflow, mode, fields, 理由)]。

    target: 対象日 (date)、now: JST の aware datetime、locks: posted_slots_T の slots (dict)、
    gh: active(wf, title=None) / pending(wf) / since(wf) を持つオブジェクト (-1 / None = 取得失敗)。
    カレンダーの判定・土日の除外は呼び出し側で済ませてから呼ぶ。
    1 tick で auto_post_x へ出すのは最大1本 (concurrency の待機は1本しか持てない #161)。"""
    from race_utils import (PREDICTION_LOCK_HM, ODDS_FLASH_LATEST_HM,
                            prediction_locked_for)
    acts = []
    hm = now.hour * 100 + now.minute
    same_day = now.date() == target
    t8 = target.strftime("%Y%m%d")
    # 翌朝 05:00 までは T の続き (results・夜間スイープ)
    t_start = dt.datetime.combine(target, dt.time(0, 0), JST)
    after = lambda h, m: now >= t_start + dt.timedelta(hours=h, minutes=m)   # noqa: E731
    before_next5 = now < t_start + dt.timedelta(days=1, hours=5)

    # runner: 当日 8:30-19:00 の tick はどれも確認する (安い。昼に runner が死んでも次の tick が起こす)
    if same_day and 830 <= hm < 1900:
        n = gh.active("race_day_runner.yml")
        if n == 0:
            acts.append(("race_day_runner.yml", None, {}, "race_day_runner が不在"))
        elif n < 0:
            log("   ⚠️ race_day_runner の稼働状況を確認できない → 起動を見送り")
        else:
            log(f"   ✅ race_day_runner 稼働中/待機中 ({n}件)")

    ap_sent = False
    for intent in intents:
        if intent == "runner":
            continue
        if intent in ("prefetch_eve", "prefetch_early"):
            cutoff = t_start + dt.timedelta(hours=final_hm // 100, minutes=final_hm % 100)
            if now >= cutoff:
                log(f"   ⏭️ {intent}: 対象日 {t8} の最後の作り直し ({final_hm // 100}:{final_hm % 100:02d}) を過ぎた")
                continue
            n = gh.active("weekend_prefetch.yml")
            if n != 0:
                log(f"   ⏳ weekend_prefetch が実行中/待機中/確認不能 ({n}) → 重ねない")
                continue
            acts.append(("weekend_prefetch.yml", None, {"dates": t8}, f"{t8} の前夜予想"))
            continue
        if intent == "sweep":
            if not (after(20, 0) and before_next5):
                log(f"   ⏭️ sweep: 窓 ({t8} 20:00〜翌05:00) の外")
                continue
            since = gh.since("collect_results.yml")
            running = gh.active("collect_results.yml")
            if since is None or running != 0:
                log(f"   ⏳ collect_results: 直近 {since} 分前 / 実行中 {running} → 見送り")
                continue
            if since < SWEEP_MIN_GAP:
                log(f"   ✅ collect_results は {since}分前に実行済み")
                continue
            acts.append(("collect_results.yml", None, {"date": t8}, f"{t8} の夜間スイープ"))
            continue
        # ここから auto_post_x (predict / odds_flash / post_predict / results)
        if ap_sent:
            log(f"   ⏭️ {intent}: この tick は auto_post_x を既に1本出した")
            continue
        if intent == "predict":
            limit = _minus_min(final_hm, PREDICT_RUN_MIN)
            if not same_day:
                log(f"   ⏭️ predict: 日付をまたいだ tick (対象日 {t8}) → 前日分なので出さない")
                continue
            if hm >= limit or prediction_locked_for(t8, now):
                log(f"   ⏭️ predict: 作り直しの締切 ({limit // 100}:{limit % 100:02d}) を過ぎた/確定済み")
                continue
            n = gh.active("auto_post_x.yml")
            if n != 0:
                log(f"   ⏳ auto_post_x が実行中/待機中/確認不能 ({n}) → 後ろに積まない")
                continue
            acts.append(("auto_post_x.yml", "predict", {}, f"{t8} の予測の作り直し"))
            ap_sent = True
            continue
        if intent in ("odds_flash", "post_predict", "results"):
            if intent == "results":
                ok = after(17, 30) and before_next5
                win = f"{t8} 17:30〜翌05:00"
            else:
                start, end = (900, ODDS_FLASH_LATEST_HM) if intent == "odds_flash" else (PREDICTION_LOCK_HM, 1155)
                ok = same_day and start <= hm < end
                win = f"当日 {start // 100}:{start % 100:02d}〜{end // 100}:{end % 100:02d}"
            if not ok:
                log(f"   ⏭️ {intent}: 窓 ({win}) の外")
                continue
            if intent in locks:
                log(f"   ⏭️ {intent} は lock 済み ({(locks[intent] or {}).get('posted_at')})")
                continue
            if intent == "results" and "post_predict" not in locks:
                # #179 T-landing-4: 予想を1本も配信できていない日 (post_predict の lock 無し) は results に報告対象が無い
                #   (post_x は「予想投稿の記録が無いため結果対象なし」で lock を解放して終わる #150)。lock が立たないので
                #   17:33/19:33/21:33 の tick が毎回 results を出し、その run は投稿しないのに collect・配当収集・lock の
                #   取得/解放で keiba.db.gz を必ず変えて commit する (bootstrap #179(1) までは1本 ~27MB。11/23 実測)。
                #   着順の収集は race_day_runner と夜間スイープが担う。セーフティネット・runner の results は従来どおり。
                log(f"   ⏭️ results: {t8} は予想投稿 (post_predict) の lock が無い = 報告対象なし → 出さない")
                continue
            p = gh.pending("auto_post_x.yml")
            a = gh.active("auto_post_x.yml", title=intent)
            if p != 0 or a != 0:
                log(f"   ⏳ {intent}: auto_post_x の待機 {p} / 同じ mode の実行中 {a} → 重ねない")
                continue
            fields = {"date_override": t8} if intent == "results" else {}
            acts.append(("auto_post_x.yml", intent, fields, f"{t8} の {intent}"))
            ap_sent = True
    return acts


# ── GitHub (本番) と偽物 (テスト) ─────────────────────────────────
class LiveGh:
    """raceday_safety_net の gh 呼び出しを包む (同じ判定をする)。"""

    def __init__(self):
        import raceday_safety_net as sn
        self.sn = sn

    def active(self, wf, title=None):
        return self.sn.gh_runs_active(wf, title=title)

    def pending(self, wf):
        return self.sn._auto_post_pending() if wf == "auto_post_x.yml" else self.active(wf)

    def since(self, wf):
        """直近 run からの経過分。run が1本も無ければ大きな値、取得失敗は None
        (セーフティネットの gh_minutes_since_last_run は両者を区別しないので自前で引く)。"""
        try:
            out = subprocess.run(
                ["gh", "api", f"repos/{os.environ.get('GITHUB_REPOSITORY', '')}"
                 f"/actions/workflows/{wf}/runs?per_page=1", "--jq",
                 ".workflow_runs[0].created_at // \"none\""],
                capture_output=True, text=True, timeout=60)
        except Exception:
            return None
        ts = out.stdout.strip()
        if out.returncode != 0 or not ts:
            return None
        if ts == "none":
            return 10 ** 6
        try:
            t = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            return None
        return int((dt.datetime.now(UTC) - t).total_seconds() // 60)

    def dispatch(self, wf, mode, fields, dry):
        return self.sn.dispatch(wf, mode, dry, fields or None)

    def race_list_html(self, t8):
        """netkeiba のレース一覧 (race_list_sub) の HTML。(text, 理由) / 取れなければ (None, 理由)。"""
        import urllib.request
        req = urllib.request.Request(RACE_LIST_URL.format(d8=t8), headers={"User-Agent": NETKEIBA_UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                if getattr(r, "status", 200) != 200:
                    return None, f"netkeiba の HTTP {r.status}"
                return r.read().decode("utf-8", "replace"), ""
        except Exception as e:
            return None, f"netkeiba に届かない ({type(e).__name__}: {e})"[:160]


class FakeGh:
    """--fake-gh '{"active": {"auto_post_x.yml": 0, "auto_post_x.yml:odds_flash": 0},
    "pending": {...}, "since": {"collect_results.yml": 300}, "dispatch_fail": [...],
    "race_list": {"20260922": "<html>…" または null (取れない)}}'。無いキーは 0。
    race_list に無い日は netkeiba に問い合わせない (テストはネットワークに出ない)。"""

    def __init__(self, spec):
        self.s = spec or {}
        self.sent = []
        self.probed = []

    def active(self, wf, title=None):
        a = self.s.get("active") or {}
        if title is not None and f"{wf}:{title}" in a:
            return a[f"{wf}:{title}"]
        return a.get(wf, 0) if title is None else 0

    def pending(self, wf):
        return (self.s.get("pending") or {}).get(wf, 0)

    def since(self, wf):
        return (self.s.get("since") or {}).get(wf, 10 ** 6)

    def dispatch(self, wf, mode, fields, dry):
        label = wf + (f" mode={mode}" if mode else "") + "".join(f" {k}={v}" for k, v in (fields or {}).items())
        self.sent.append(label)
        if wf in (self.s.get("dispatch_fail") or []):
            log(f"   ❌ dispatch 失敗: {label}")
            return False
        log(f"   [dry-run] would dispatch: {label}" if dry else f"   ✅ (fake) dispatched: {label}")
        return True

    def race_list_html(self, t8):
        rl = self.s.get("race_list") or {}
        self.probed.append(t8)
        log(f"   (fake) netkeiba のレース一覧を問い合わせ: {t8}")
        if t8 not in rl:
            return None, "テスト: 問い合わせない"
        return (rl[t8], "") if rl[t8] is not None else (None, "テスト: 取れない")


def read_locks(t8, slots_dir=None):
    """posted_slots_T の lock。contents API (CDN を通らない #173) → checkout の版 → 無し。"""
    if not slots_dir:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if repo:
            try:
                r = subprocess.run(
                    ["gh", "api", "-H", "Accept: application/vnd.github.raw",
                     f"repos/{repo}/contents/docs/data/posted_slots_{t8}.json?ref=main"],
                    capture_output=True, text=True, timeout=30)
                if r.returncode == 0 and r.stdout.strip():
                    return (json.loads(r.stdout).get("slots") or {})
                if "Not Found" in (r.stdout + r.stderr):
                    return {}
            except Exception:
                pass
        slots_dir = os.path.join(ROOT, "docs", "data")
    try:
        with open(os.path.join(slots_dir, f"posted_slots_{t8}.json"), encoding="utf-8") as f:
            return json.load(f).get("slots") or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"   ⚠️ posted_slots_{t8}.json を読めない ({type(e).__name__}) → lock 無しとみなす")
        return {}


def _parse_now(s):
    t = dt.datetime.fromisoformat(s)
    return t.replace(tzinfo=JST) if t.tzinfo is None else t.astimezone(JST)


def main(argv=None):
    ap = argparse.ArgumentParser(description="土日以外の開催日の定刻の起動 (#179)")
    ap.add_argument("--schedule", default=os.environ.get("CLOCK_SCHEDULE", ""))
    ap.add_argument("--intent", default=os.environ.get("CLOCK_INTENT", ""))
    ap.add_argument("--date", default=os.environ.get("CLOCK_DATE", ""), help="対象日 YYYYMMDD (手動)")
    ap.add_argument("--now", help="現在時刻 (JST, ISO8601。tz 無しは JST) — テスト用")
    ap.add_argument("--calendar", help="race_calendar.json の場所 — テスト用")
    ap.add_argument("--slots-dir", help="posted_slots_*.json の場所 — テスト用 (指定時は API を見ない)")
    ap.add_argument("--fake-gh", help="偽の gh (JSON) — テスト用")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    now = _parse_now(a.now) if a.now else dt.datetime.now(JST)
    sched = (a.schedule or "").strip()
    manual = not sched
    dry = a.dry_run or (manual and os.environ.get("CLOCK_DRY_RUN", "1") != "0")

    if sched:
        if sched not in SCHEDULE:
            log(f"::warning::#179: 対応表に無い cron '{sched}' → 何もしない")
            return 0
        intents, offset = SCHEDULE[sched]
        planned = planned_fire(sched, now.astimezone(UTC))
        if planned is None:
            log(f"::warning::#179: cron '{sched}' の予定時刻を決められない → 何もしない")
            return 0
        planned_jst = planned.astimezone(JST)
        delay_min = (now - planned_jst).total_seconds() / 60
        if delay_min > MAX_DELAY_H * 60:
            log(f"::warning::#179: 予定 {planned_jst:%m/%d %H:%M} から {delay_min / 60:.1f} 時間遅れた tick → 何もしない")
            return 0
        target = planned_jst.date() + dt.timedelta(days=offset)
        log(f"🗓 #179 時計: cron '{sched}' 予定 {planned_jst:%Y-%m-%d %H:%M} JST / 実行 {now:%Y-%m-%d %H:%M} JST "
            f"(遅れ {max(0, delay_min):.0f}分) → 対象日 {target} / intents {list(intents)}")
    else:
        if a.intent not in INTENTS:
            log(f"使い方: --schedule か --intent ({'/'.join(INTENTS)}) を指定 (#179)")
            return 0
        intents = ("runner", a.intent) if a.intent != "runner" else ("runner",)
        if a.date:
            d = a.date.replace("-", "")
            target = dt.date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        else:
            target = now.date() + dt.timedelta(days=MANUAL_OFFSET.get(a.intent, 0))
        log(f"🗓 #179 時計 (手動): intent {a.intent} / 対象日 {target} / 実行 {now:%Y-%m-%d %H:%M} JST"
            f"{' / dry-run (判定だけ)' if dry else ''}")

    if target.weekday() >= 5:
        log(f"⏭️ {target} は土日 → ネイティブの cron が担当 (この時計は何もしない)")
        return 0

    from race_utils import calendar_race_day
    listed, why = calendar_race_day(target.isoformat(), now=now, path=a.calendar)
    if listed is None:
        log(f"::warning::#179: race_calendar.json を判定に使えない ({why}) → 土日だけを開催日とみなす (縮退)。"
            f"この tick は何もしない。土日以外の開催日なら posting_watchdog のセーフティネットが Issue で知らせる")
        return 0
    gh = FakeGh(json.loads(a.fake_gh)) if a.fake_gh is not None else LiveGh()
    final_hm = final_rebuild_hm()
    t8 = target.strftime("%Y%m%d")
    if not listed:
        # #179 T-clock-2: 新しいカレンダーの否定は「作り直しより後に決まった順延」を知らない → 平日の対象日だけ
        #   netkeiba で確かめる (この tick が何かを出しうる時だけ。上で土日は返している)
        if set(intents) & set(PROBE_INTENTS) and probe_worth(intents, target, now, final_hm):
            hit, pwhy = probe_race_day(gh, t8)
            if hit:
                log(f"::warning::#179: カレンダー ({why}) と食い違い、{target} は {pwhy} → 開催日として扱う "
                    f"(作り直しより後に決まった平日の開催 = 急な順延など。次のカレンダーの作り直しで載る)")
                listed, why = True, f"{target} は {pwhy}"
            else:
                log(f"   🔎 netkeiba でも {target} の開催を確認できない ({pwhy})")
    if not listed:
        log(f"⏭️ {why} → 何もしない")
        return 0
    log(f"🏇 {why}")

    locks = read_locks(t8, a.slots_dir)
    log(f"   lock 済み slot: {sorted(locks) or 'なし'}")
    acts = decide(intents, target, now, locks, gh, final_hm)
    failed = 0
    for wf, mode, fields, why in acts:
        log(f"🚨 {why} → dispatch")
        if not gh.dispatch(wf, mode, fields, dry):
            failed += 1
    log(f"\n📋 dispatch: {[f'{w}' + (f' {m}' if m else '') for w, m, _, _ in acts] or 'なし'}"
        + (" (dry-run)" if dry else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
