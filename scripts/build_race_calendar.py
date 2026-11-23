#!/usr/bin/env python3
"""開催日カレンダーを текст サイドカーに書き出す (#158)

なぜ要るか
----------
このシステムの「レース日」判定は全て曜日ベース (cron の `6,0` と `DOW -ge 6`) で、
**祝日の3日間開催 (月曜) が構造的に見えない**。2026-09-21 (敬老の日) は
阪神10R・神戸新聞杯(G2) が組まれていたが、レース枠は DB に登録された一方で
出走馬の取得も予測も走らず、ダッシュボードは発走1時間半前まで404だった。
3日間開催は年に数回ある (成人の日/GW/敬老の日/スポーツの日/文化の日 等)。

曜日を足して回る対症療法はやめる。#2 の馬番ガードと同じ考え方で
**「DB にレースが入っているか」をデータ駆動の判定にする**。
ただし watchdog は keiba.db を持たない (正本は .gz、展開もしない #150) ので、
DB を読めるワークフロー側でこのカレンダーを書き出し、
watchdog は git 上のテキストを読むだけにする。

出力: docs/data/race_calendar.json
  {"generated_at": ..., "dates": {"2026-09-21": {"races": 10, "venues": ["阪神"]}, ...}}
  対象は「今日を含む前後14日」。

--probe (#179 Part D): カレンダーの「発見」
-------------------------------------------
時計 (raceday_clock) もセーフティネットも、土日以外の開催日が **DB (= このカレンダー) に入っていなければ
何も起きない**。fetch_weekend_races は次の土日しか取らないので、月曜祝日・順延の火曜が DB に入るのは
偶然だった (2026-11-23 は 24R 中 6R だけ載っていた)。
--probe の時だけ、netkeiba の月カレンダー (窓 [今日, 今日+14] にかかる1〜2か月) の日付セルのリンク
(race_list.html?kaisai_date=) のうち **平日 (月〜金)** について既存のレース一覧の取得
(scraper.get_race_list_by_date) を呼び、JRA の場コード 01-10 のレースがあれば
{"races": n, "venues": [...], "source": "netkeiba"} を足す。
  - 無ければ足さない (海外馬券・中止・未発表を「開催日」にしない)。土日は触らない。
  - DB にある平日も一覧と突き合わせ、races は大きい方・venues は和集合で「補う」だけ (source=db+netkeiba)。
    DB に特別登録の1レースだけ、という日に場数×12 の期待レース数が半日分に縮むのを防ぐ (#179 T-partd-4)。
  - 日付はセルのリンクからだけ拾う。天候パネル (weather_summary.html?kaisai_date=<今日>) は開催日の
    開催時間帯にだけ全ページに出る別物で、拾うと形式の確認が時間帯次第で偽の合格になる (#179 T-partd-3)。
  - 翌月のページは、その月の最初のレース一覧が出るまで (初週の約1週間前まで) リンクが1本も無いのが正常
    (毎月17日頃から窓が翌月にかかる)。空・取得失敗は「その月は候補なし」で、当月の発見を巻き添えにしない。
    「形式が変わった」と止めるのは今日を含む月だけ (#179 T-weekend-1 / T-partd-2)。
  - レース一覧を取れない (HTTP エラー・タイムアウト) 日は「開催なし」と確定しない (status=partial で警告)。

前回の版の引き継ぎ (#179 T-clock-1 / T-partd-1 / T-landing-3)
---------------------------------------------------------------
作り直しは毎回 DB 由来から始まるので、前回 netkeiba で見つけた平日 (DB にまだ1レースも無い月曜祝日など) は
引き継がないと、問い合わせの失敗・外側の timeout・`|| --probe なし` の呼び直しの1回で黙って消え、新しい版なので
calendar_race_day / セーフティネットが確信を持って「開催日でない」と言う (時計もセーフティネットも黙る)。
そこで前回の版の source=netkeiba / db+netkeiba の平日 (窓内) を、DB 由来に重ねて **最初の書き込みから** 引き継ぐ。
外すのは --probe が「その月のカレンダーを読めた (リンクがある) のに、その日がもう載っていない」と確かめた
未来の日だけ (中止・移動。::warning:: を出す)。一覧を取れなかった日は引き継ぐ (判定できない)。
一覧を取れても、空・途中まで (どこかの場が12Rに満たない) なら前回の値を減らさない — 一時的な空ページ・
取りこぼし (9/21 は一覧が 10R だけ返った #161) と中止を区別できないので、警告して引き継ぎ、一覧で増えた分だけ足す
(#179 round 5)。全場12Rそろった一覧を取れた DB にある日は、引き継がず DB 由来 + 今回の一覧で補った値に戻す —
補う分が無くなった (場が中止・移動して一覧が DB と一致した) のに前回の値を残すと、窓を出るまで期待レース数・
開催場を過大に言い続ける (#179 round 4 T-partd-4 / R3-T-stale-merge)。
今日より前の日は確かめようがないので窓を出るまで残す (翌朝の完了検査が使う)。

  - 失敗・タイムアウトは DB 由来 + 引き継ぎで書く (先に書いてから問い合わせるので、外側の
    `timeout 120` に殺されても残る。呼び出し側は `|| 従来の呼び出し` で必ず書き直す — それも引き継ぐ)。
  - DB には書かない (scraper の __init__ は init_db で keiba.db を触るので呼ばない)。通信は 2〜5 回/回
    (REQUEST_INTERVAL つき)。netkeiba の race ページは UTF-8 で、文字コードは scraper._get がサーバの
    Content-Type に従う (#85)。ここで読むのは数字 (kaisai_date / race_id) だけ。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "data", "race_calendar.json")
WINDOW_DAYS = 14
PROBE_DEADLINE_S = 100           # 呼び出し側の timeout 120 より前に自分で打ち切る
CALENDAR_URL = "https://race.netkeiba.com/top/calendar.html?year={y}&month={m}"
VENUE_NAMES = {1: "札幌", 2: "函館", 3: "福島", 4: "新潟", 5: "東京",
               6: "中山", 7: "中京", 8: "京都", 9: "阪神", 10: "小倉"}


class ProbeError(Exception):
    pass


def db_dates(db, lo, hi):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    dates = {}
    try:
        for r in conn.execute(
            "SELECT race_date, COUNT(*) n, GROUP_CONCAT(DISTINCT venue) v FROM races "
            "WHERE race_date BETWEEN ? AND ? AND race_date != '' "
            # #161: JRA の場コード (01-10) だけ。地方競馬の残骸行 (venue 空) が1行でもあると、
            # その日が「開催日」になり非開催日に予測・投稿を dispatch しうる。
            "AND CAST(substr(race_id, 5, 2) AS INT) BETWEEN 1 AND 10 AND venue != '' "
            "GROUP BY race_date",
                (lo.isoformat(), hi.isoformat())):
            dates[r["race_date"]] = {
                "races": r["n"],
                "venues": sorted((r["v"] or "").split(",")) if r["v"] else [],
            }
    finally:
        conn.close()
    return dates


# #179 T-weekend-1 / T-partd-2 / T-partd-3: 日付は月カレンダーの日付セルのリンク (race_list.html?kaisai_date=) からだけ拾う。
#   素の `kaisai_date=` だと、開催日の開催時間帯にだけサイト共通で出る「リアルタイム天候情報」
#   (/race/weather_summary.html?kaisai_date=<今日>) まで拾う。2026-11-23 の12月・1月のページはセルのリンクが
#   0本で、拾えたのはこの天候パネルの 20261123 だけだった = 「形式の確認」が開催日の日中だけ偽の合格になり、
#   05:00 の定時実行 (パネルが出ない) とで結果が変わる。
CAL_LINK_RE = re.compile(r"race_list\.html\?[^\"'<>\s]*?kaisai_date=(\d{8})")


def kaisai_dates_from_html(text):
    """netkeiba の月カレンダーの HTML から日付セルのリンクの kaisai_date=YYYYMMDD を拾う。"""
    out = []
    for d8 in sorted(set(CAL_LINK_RE.findall(text or ""))):
        try:
            out.append(datetime.strptime(d8, "%Y%m%d").date())
        except ValueError:
            continue
    return out


def jra_summary(race_ids):
    """レース一覧の race_id から JRA (場コード 01-10) のレース数と場名。それ以外 (海外など) は数えない。"""
    by = {}
    for rid in race_ids or []:
        rid = str(rid)
        if not re.fullmatch(r"\d{12}", rid):
            continue
        code = int(rid[4:6])
        if 1 <= code <= 10:
            by.setdefault(code, set()).add(rid)
    n = sum(len(v) for v in by.values())
    return n, sorted(VENUE_NAMES[c] for c in by)


def months_covering(lo, hi):
    out, y, m = [], lo.year, lo.month
    while (y, m) <= (hi.year, hi.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def make_scraper():
    """既存のスクレイパー (間隔制御・文字コード #85 込み) を、keiba.db に触れずに使う。"""
    import requests
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from scraper import NetkeibaScraper
    s = NetkeibaScraper.__new__(NetkeibaScraper)    # __init__ は init_db() で DB を触るので呼ばない
    s.session = requests.Session()
    s.session.headers.update(NetkeibaScraper.HEADERS)
    return s


class ProbeResult:
    """probe() の結果 (#179)。
    add:      新しく見つけた平日 (source=netkeiba) と、DB 由来を補った平日 (source=db+netkeiba)
    observed: レース一覧を取れた平日 → (JRA のレース数, 場名)。空 (0) も含む・DB にある日も含む
              (#179 round 4: 前回補った値が今回の一覧でもう支えられていないかを main が判定する)
    seen:     リンクのある月のカレンダーに載っていた日 (窓内・全曜日)
    verified: 日付セルのリンクが1本以上あった月 (= 形式を確かめられた月。「載っていない」と言える月)
    unknown:  レース一覧を取れなかった日 → 理由 (「開催なし」と確定しない)
    skipped:  読めなかった・確かめられなかった月 → 理由 (当月以外。その月は候補なし)"""

    def __init__(self):
        self.add, self.seen, self.verified, self.unknown, self.skipped = {}, set(), set(), {}, {}
        self.observed = {}

    @property
    def partial(self):
        return bool(self.unknown or self.skipped)


def merge_up(base, obs):
    """DB 由来の base を netkeiba の観測 obs で「補う」(races は大きい方・venues は和集合)。
    変わらなければ base をそのまま、変われば source=db+netkeiba の新しい dict (#179 T-partd-4)。"""
    b_n, b_v = int(base.get("races") or 0), sorted(base.get("venues") or [])
    n = max(b_n, int(obs.get("races") or 0))
    v = sorted(set(b_v) | set(obs.get("venues") or []))
    if (n, v) == (b_n, b_v):
        return base
    return {"races": n, "venues": v, "source": "db+netkeiba"}


# JRA は1場1日12レース (raceday_safety_net.expected_races と同じ前提)
RACES_PER_VENUE = 12


def list_complete(n, venues):
    """#179 round 5: レース一覧が「その日の全部」に見えるか (JRA のレースが1本以上あり、どの場も12レースそろう)。
    空・途中まで (一時的な空ページ・9/21 の 10R のような取りこぼし) は False — 前回の値を減らす根拠にしない。"""
    return n > 0 and n >= RACES_PER_VENUE * len(venues or [])


def probe(dates, today, scraper, deadline=None, log=print):
    """netkeiba の月カレンダーとレース一覧で、窓 [today, today+14] の平日 (月〜金) の開催を確かめる。
    dates は DB 由来の dates (引き継いだ日は含めない = 毎回問い合わせ直す)。戻り値 ProbeResult。

    ProbeError (呼び出し側は DB 由来 + 引き継ぎで書く) になるのは:
      - 今日を含む月のカレンダーを取れない・形式が変わった疑い (この月は過去の開催日のリンクが残るので、
        DB に今日以前の当月の開催日があるのにリンクが0本 / HaveData のセルがあるのにリンクが0本)
      - カレンダーを読み終える前に締切を過ぎた
    翌月以降のページが空・取れないのは「その月は候補なし」(skipped / 空は正常)。レース一覧を取れない日は
    unknown (開催なしと確定しない)。一覧の途中で締切を過ぎたら残りを unknown にして見つけた分は返す。"""
    lo, hi = today, today + timedelta(days=WINDOW_DAYS)
    cur = (today.year, today.month)
    res = ProbeResult()
    new_cands, known_cands = set(), set()
    for y, m in months_covering(lo, hi):
        if deadline is not None and time.monotonic() > deadline:
            raise ProbeError("締切を過ぎた (カレンダーの取得中)")
        resp = scraper._get(CALENDAR_URL.format(y=y, m=m))
        text = getattr(resp, "text", "") if resp else None
        if text is None:
            if (y, m) == cur:
                raise ProbeError(f"netkeiba の {y}年{m}月 (今日を含む月) のカレンダーを取れない")
            # #179 T-partd-2: 翌月が取れない時はその月を飛ばすだけ (当月の発見は捨てない)
            res.skipped[(y, m)] = "取れない"
            log(f"   ⚠️ {y}年{m}月のカレンダーを取れない → この月は候補なし (当月の発見は残す)")
            continue
        got = kaisai_dates_from_html(text)
        if not got:
            prefix = f"{y:04d}-{m:02d}-"
            past = sorted(k for k in dates if k.startswith(prefix) and k <= today.isoformat())
            if "HaveData" in text:
                why = "レース一覧のある日 (HaveData) のセルがあるのに日付のリンクが0本"
            elif (y, m) == cur and past:
                why = f"DB の開催日 {past[0]} 等があるのに日付のリンクが0本"
            else:
                why = None
            if why:
                if (y, m) == cur:
                    raise ProbeError(f"{y}年{m}月 (今日を含む月) のカレンダーに {why} (ページの形式が変わった疑い)")
                res.skipped[(y, m)] = why
                log(f"   ⚠️ {y}年{m}月のカレンダーに {why} (形式が変わった疑い) → この月は候補なし")
                continue
            # #179 T-weekend-1: 翌月 (や月初の当月) はレース一覧が出るまでリンクが0本なのが正常 → 候補なし。
            #   この月は「確かめた」に数えない (引き継いだ日を「載っていない」として外さない)
            log(f"   🔎 {y}年{m}月のカレンダーはまだ開催日のリンクが無い (レース一覧の公開前は普通) → 候補なし")
            continue
        res.verified.add((y, m))
        for d in got:
            if not lo <= d <= hi:
                continue
            res.seen.add(d.isoformat())
            if d.weekday() < 5:
                (known_cands if d.isoformat() in dates else new_cands).add(d)

    def race_list(d):
        """(n, venues) / None = 取れない (unknown に理由を残す)。"""
        k = d.isoformat()
        try:
            rids = scraper.get_race_list_by_date(d.strftime("%Y%m%d"), strict=True)
        except Exception as e:
            res.unknown[k] = f"例外 {type(e).__name__}: {e}"[:120]
            return None
        if rids is None:
            res.unknown[k] = "レース一覧を取れない (HTTP エラー・タイムアウト)"
            return None
        return jra_summary(rids)

    # 発見 (DB に無い平日) を先に、DB にある平日の「補う」を後に回す (補う側の失敗・締切で発見を捨てない)
    queue = [(d, False) for d in sorted(new_cands)] + [(d, True) for d in sorted(known_cands)]
    for i, (d, known) in enumerate(queue):
        k = d.isoformat()
        if deadline is not None and time.monotonic() > deadline:
            for d2, _ in queue[i:]:
                res.unknown[d2.isoformat()] = "締切を過ぎた (問い合わせ前)"
            log(f"   ⚠️ 締切を過ぎた → 残り {len(queue) - i}日 ({k}〜) は判定できない")
            break
        got = race_list(d)
        if got is None:
            log(f"   ⚠️ {d} (平日{'・DB にあり' if known else ''}) のレース一覧を取れない ({res.unknown[k]}) "
                f"→ この日は判定できない (前回の版で見つけていれば引き継ぐ)")
            continue
        n, venues = got
        res.observed[k] = (n, venues)
        if known:
            merged = merge_up(dates[k], {"races": n, "venues": venues})
            if merged is not dates[k]:
                res.add[k] = merged
                log(f"   🔎 {d} (平日・DB にあり {dates[k].get('races')}R) を netkeiba の一覧で補う → "
                    f"{merged['races']}R / {'・'.join(merged['venues'])}")
            continue
        if n:
            res.add[k] = {"races": n, "venues": venues, "source": "netkeiba"}
            log(f"   🔎 {d} (平日) は netkeiba に JRA の開催あり ({n}R / {'・'.join(venues)}) → カレンダーに足す")
        else:
            log(f"   🔎 {d} (平日) は netkeiba のカレンダーにあるが JRA のレースが無い "
                f"(海外・中止・未発表) → 足さない")
    return res


def load_previous(path):
    """前回の版の dates ({iso: info})。無い・壊れている → {} (初回と同じ)。"""
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        return {}
    d = prev.get("dates") if isinstance(prev, dict) else None
    return d if isinstance(d, dict) else {}


def carry_forward(prev, db, lo, hi):
    """#179 T-clock-1 / T-partd-1 / T-landing-3: 前回の版で netkeiba から確かめた平日を引き継ぐ。
    窓 [lo, hi]・平日・source が netkeiba / db+netkeiba の日だけ。DB に無い日はそのまま (source=netkeiba)、
    DB にある日は DB 由来を補った形 (補う分が無ければ引き継がない = DB 由来が正)。"""
    out = {}
    for k, v in (prev or {}).items():
        if not isinstance(v, dict) or v.get("source") not in ("netkeiba", "db+netkeiba"):
            continue
        try:
            d = date.fromisoformat(k)
        except (TypeError, ValueError):
            continue
        if not lo <= d <= hi or d.weekday() >= 5:
            continue
        try:
            obs = {"races": int(v.get("races") or 0), "venues": sorted(v.get("venues") or [])}
        except (TypeError, ValueError):
            continue
        if k in db:
            merged = merge_up(db[k], obs)
            if merged is not db[k]:
                out[k] = merged
        elif obs["races"] > 0:
            out[k] = {**obs, "source": "netkeiba"}
    return out


def write_payload(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="開催日カレンダー (docs/data/race_calendar.json) を書き出す")
    ap.add_argument("--probe", action="store_true",
                    help="netkeiba のカレンダーで平日の開催日を発見して足す (#179 Part D)")
    ap.add_argument("--db", default=os.path.join(ROOT, "keiba.db"), help=argparse.SUPPRESS)
    ap.add_argument("--out", default=OUT, help=argparse.SUPPRESS)
    ap.add_argument("--today", help=argparse.SUPPRESS)           # テスト用 YYYY-MM-DD
    a = ap.parse_args(argv)

    if not os.path.exists(a.db):
        print("⚠️ keiba.db が無いのでカレンダーを更新しません (既存値を維持)")
        return 1
    now = datetime.now(JST)
    today = date.fromisoformat(a.today) if a.today else now.date()
    lo, hi = today - timedelta(days=WINDOW_DAYS), today + timedelta(days=WINDOW_DAYS)
    db = db_dates(a.db, lo, hi)
    # #179 T-clock-1 / T-partd-1 / T-landing-3: 書き換える前に前回の版を読み、netkeiba で見つけた平日を引き継ぐ。
    #   最初の書き込みから重ねるので、問い合わせの失敗・外側の timeout・`|| --probe なし` のどれでも消えない
    carried = carry_forward(load_previous(a.out), db, lo, hi)
    dates = {**db, **carried}
    payload = {"generated_at": now.isoformat(),
               "window": [lo.isoformat(), hi.isoformat()], "dates": dict(sorted(dates.items()))}
    if carried:
        payload["carried"] = sorted(carried)
        print(f"   📌 #179: 前回 netkeiba で確かめた平日を引き継ぐ: {', '.join(sorted(carried))}")
    # 先に DB 由来 + 引き継ぎを書く (この後の問い合わせが外側の timeout に殺されても、この版が残る)
    write_payload(a.out, payload)

    if a.probe:
        info = {"at": datetime.now(JST).isoformat(), "added": []}
        try:
            res = probe(dict(db), today, make_scraper(),
                        deadline=time.monotonic() + PROBE_DEADLINE_S)
        except Exception as e:      # 失敗・締切 → DB 由来 + 引き継ぎ (上で書き済み)
            info["status"] = f"failed: {type(e).__name__}: {e}"[:300]
            kept = (f" + 前回 netkeiba で確かめた {len(carried)}日 ({', '.join(sorted(carried))}) を引き継いで書く"
                    if carried else "だけで書く")
            print(f"::warning::#179: netkeiba での開催日の発見に失敗 ({e}) → DB 由来の日付{kept}")
        else:
            final = dict(db)
            kept, dropped, reverted, partial_list = [], [], [], []
            for k, v in sorted(carried.items()):
                d = date.fromisoformat(k)
                obs = res.observed.get(k)
                if obs is not None and not list_complete(*obs):
                    # #179 round 5 (R4-revert-on-empty-or-partial-list): 一覧は取れたが空・途中まで (どこかの場が12Rに
                    #   満たない)。一時的な空ページ・取りこぼし (9/21 は一覧が 10R だけ返った #161) と中止を区別できない
                    #   → 前回の値を減らさない (一覧で増えた分だけ足す)。減らすと期待レース数・開催場を過小に言い、
                    #   セーフティネットの予測の再投入と「予測不足」の警告、preflight の欠けた場の取得が黙って止まる。
                    #   過大に言う側 (警告が余分に出る) に倒す。前回の値を外すのは「全場12Rそろった一覧」が支えない時だけ。
                    n_obs, v_obs = obs
                    prev_v = v
                    base = res.add.get(k)
                    if base is not None:
                        # 一覧で増えた分は足す (res.add も合わせた値にして、added / merged の記録にも残す)
                        v = {"races": max(int(v.get("races") or 0), int(base.get("races") or 0)),
                             "venues": sorted(set(v.get("venues") or []) | set(base.get("venues") or [])),
                             "source": base.get("source") or v.get("source")}
                        res.add[k] = v
                    final[k] = v
                    kept.append(k)
                    partial_list.append(k)
                    print(f"::warning::#179: {k} の netkeiba の一覧が空・途中まで ({n_obs}R / "
                          f"{'・'.join(v_obs) or 'JRA なし'}) — 中止か一時的な取りこぼしか区別できないので、"
                          f"前回見つけた {prev_v.get('races')}R / {'・'.join(prev_v.get('venues') or [])} を減らさずに引き継ぐ"
                          + (f" (一覧で増えた分を足して {v.get('races')}R / {'・'.join(v.get('venues') or [])})"
                             if v is not prev_v else ""))
                    continue
                if k in res.add:
                    continue                    # 確かめ直した値で置き換える (下)
                if obs is not None and k in db:
                    # #179 round 4 (T-partd-4 / R3-T-stale-merge): 全場12Rそろった一覧を取れた DB にある日。res.add に無い =
                    #   merge_up(DB 由来, 今回の一覧) が DB 由来のまま = 前回補った分 (中止・移動した場など) を今回の一覧は
                    #   もう支えていない → DB 由来に戻す (final[k] は db[k] のまま)。前回の値を残すと、DB と一覧が一致しても
                    #   窓を出るまで期待レース数・開催場を過大に言い続け、セーフティネットの「予測不足」と preflight の
                    #   「開催場なのに DB に無い」を毎回誤って出す。
                    reverted.append(k)
                    n_obs, v_obs = res.observed[k]
                    print(f"   🔎 {k}: netkeiba の一覧 ({n_obs}R / {'・'.join(v_obs) or 'JRA なし'}) が DB 由来 "
                          f"({db[k].get('races')}R / {'・'.join(db[k].get('venues') or [])}) を超えない → 前回補った "
                          f"{v.get('races')}R / {'・'.join(v.get('venues') or [])} を外す (DB 由来に戻す)")
                    continue
                if (d < today or (d.year, d.month) not in res.verified
                        or k in res.unknown or k in res.seen):
                    # 今日より前 (確かめようがない) / その月を確かめられない / 一覧を取れない (締切を含む) → 引き継ぐ
                    final[k] = v
                    kept.append(k)
                else:
                    dropped.append(k)
                    print(f"::warning::#179: 前回 netkeiba で見つけた {k} が、読めた月カレンダーから消えた "
                          f"(中止・移動) → " + ("netkeiba で補った分を外す (DB 由来に戻す)" if k in db
                                               else "開催日から外す"))
            for k, v in res.add.items():
                if date.fromisoformat(k).weekday() < 5:     # 念のため二重に守る
                    final[k] = v
            # #179 round 5 (PT-lookalike-1): 前回見つけた開催場が「全場12Rそろった一覧」に無いので外した日は警告する。
            #   場の中止・移動なら正しいが、一覧が場を丸ごと落とした一時的な取りこぼしとは一覧だけでは区別できない
            #   (取りこぼしなら次の作り直しで戻る)。黙って減らさない
            shrunk = []
            for k, v in sorted(carried.items()):
                if k not in final:
                    continue                        # dropped (上で警告済み)
                lost = sorted(set(v.get("venues") or []) - set(final[k].get("venues") or []))
                if lost:
                    shrunk.append(k)
                    print(f"::warning::#179: {k} は前回 netkeiba で見つけた {'・'.join(lost)} が今回の一覧 (全場12R) に無い "
                          f"→ 外す ({v.get('races')}R → {final[k].get('races')}R)。中止・移動なら正しい。"
                          f"一覧の一時的な取りこぼしなら次の作り直しで戻る")
            dates = final
            new = sorted(k for k in res.add if k not in db and k not in carried)
            refreshed = sorted(k for k in res.add if k not in db and k in carried)   # 前回見つけた日を確かめ直した
            merged = sorted(k for k in res.add if k in db)
            info["added"] = new
            if refreshed:
                info["refreshed"] = refreshed
            if merged:
                info["merged"] = merged
            if kept:
                info["kept"] = kept
            if dropped:
                info["dropped"] = dropped
            if reverted:
                info["reverted"] = reverted
            if partial_list:
                info["partial_list"] = partial_list
            if shrunk:
                info["shrunk"] = shrunk
            if res.partial:
                why = [f"{y}年{m}月: {r}" for (y, m), r in sorted(res.skipped.items())]
                why += [f"{k}: {r}" for k, r in sorted(res.unknown.items())]
                info["status"] = ("partial: " + " / ".join(why))[:300]
                print(f"::warning::#179: netkeiba で確かめられなかった月・日がある ({' / '.join(why)}) "
                      f"→ その分は判定しない (前回の版で見つけた日は引き継ぐ)")
            else:
                info["status"] = "ok"
            payload["dates"] = dict(sorted(dates.items()))
            if kept:
                payload["carried"] = kept
            else:
                payload.pop("carried", None)
            print(f"🔎 #179: netkeiba で平日の開催日を確認 — 追加 {len(new)}日"
                  + (f" ({', '.join(new)})" if new else "")
                  + (f" / 確かめ直す {len(refreshed)}日 ({', '.join(refreshed)})" if refreshed else "")
                  + (f" / 補う {len(merged)}日 ({', '.join(merged)})" if merged else "")
                  + (f" / 外す {len(dropped)}日 ({', '.join(dropped)})" if dropped else "")
                  + (f" / 補いを外す {len(reverted)}日 ({', '.join(reverted)})" if reverted else "")
                  + (f" / 開催場が減った {len(shrunk)}日 ({', '.join(shrunk)})" if shrunk else "")
                  + (f" / 一覧が途中まで {len(partial_list)}日 ({', '.join(partial_list)})" if partial_list else ""))
        payload["probe"] = info
        write_payload(a.out, payload)

    t = dates.get(today.isoformat())
    print(f"📅 開催日カレンダー更新: {len(dates)}日分 "
          f"({lo} 〜 {hi})")
    print(f"   本日 {today} は " +
          (f"開催あり ({t['races']}R / {'・'.join(t['venues'])})" if t else "開催なし"))
    # 曜日ベースの想定と食い違う日を目立たせる (3日間開催の検知)
    extra = [d for d in sorted(dates)
             if datetime.fromisoformat(d).weekday() < 5]
    if extra:
        print(f"   ⚠️ 平日開催 (曜日ベースの判定から漏れる日): {', '.join(extra)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
