"""レースの天候・馬場 (races.weather / races.track_condition) の読み取り・正規化・保存規則 (#169)。

2026-10-25 16:53〜10-26 17:10 UTC の間に netkeiba の RaceData01 の書式が変わった:
  旧: 「… / 天候:晴 / 馬場:稍」 (見出し語つきの文字列)
  新: 天候は <span class="Icon_Weather02 Icon_W02" title="曇"> のアイコンだけ、
      馬場は <span class="Item03"> 良</span> (芝) / <span class="Item04"> 稍</span> (ダート) の1文字。
      障害 (芝→ダートを走る) は Item03 と Item04 の両方が出る。
scraper / refresh_odds の正規表現「天候:(\\S+)」「馬場:(\\S+)」が当たらなくなり、
代わりの部分一致 ["不良","稍重","重","良"] は1文字の「稍」「不」を拾えないので、
10/24 以降は天候が全レース空、稍重・不良も空で保存された。
さらに save_race_to_db の INSERT OR REPLACE が、当日正しく取れていた 10/24・10/25 の値
(稍36・不16) を 10/26 の取り直しで空に上書きした。

ここに書くもの (scraper / refresh_odds / predict が同じ規則を使う):
  - parse_weather_track(): 新旧どちらの書式からも (天候, 馬場) を取る
  - normalize_track_condition(): DB の正規表記「良/稍/重/不」に揃える
    (2020-2026 の DB は全 23,600 レースがこの1文字表記。2文字の「稍重/不良」は0件)
  - keep_existing_race_meta(): 新しく取れた値が空なら既存の値を残す (空で上書きしない)
  - meta_fill_rates(): 開催日ごとの充足率 (collect_results の自己修復と日次の完了検査が使う)
"""
import re
import unicodedata

# DB の正規表記 (1文字)。features.py / fast_train.py の tc_map もこの表記を前提にしている。
TRACK_CONDITIONS = ("良", "稍", "重", "不")
_TRACK_CANON = {"良": "良", "稍": "稍", "稍重": "稍", "重": "重", "不": "不", "不良": "不"}
# 天候は DB の既存値 (晴/曇/雨/小雨/小雪/雪) をそのまま使う。未知の値は捨てずにそのまま入れる
# (旧実装も見出し語の後ろをそのまま保存していた)。
KNOWN_WEATHERS = ("晴", "曇", "雨", "小雨", "小雪", "雪")
# 障害の馬場は芝の値を入れる (DB の過去分: 芝ダで馬場が違った日の障害 108R のうち
# 芝と一致 85 / ダートと一致 15 = 旧書式の「馬場:」の最初の値が芝だった)。
_LABEL_RE = re.compile(r"^(?:馬場|芝|ダート|ダ|障害|障)\s*[:：]\s*")
_warned = set()


def _nfkc(s):
    return unicodedata.normalize("NFKC", s or "").strip()


def _warn_once(kind, raw):
    key = (kind, raw)
    if key not in _warned:
        _warned.add(key)
        print(f"⚠️ #169 {kind} が想定外の表記: {raw!r} → 保存しない (書式変更の疑い)")


def normalize_track_condition(raw):
    """馬場の表記を DB の正規表記 (良/稍/重/不) に。読めなければ ""。

    「稍重」「不良」(2文字)・「稍」「不」(1文字)・「馬場:稍」「ダ:重」(見出し語つき)を受ける。"""
    s = _nfkc(raw).lstrip("/").strip()
    s = _LABEL_RE.sub("", s).strip()
    if not s:
        return ""
    if s in _TRACK_CANON:
        return _TRACK_CANON[s]
    for k in sorted(_TRACK_CANON, key=len, reverse=True):   # 「稍重 ダ:重」のような連結
        if s.startswith(k):
            return _TRACK_CANON[k]
    _warn_once("馬場", raw)
    return ""


def normalize_weather(raw):
    """天候の表記を正規化 (NFKC・見出し語と区切りの除去)。空なら ""。"""
    s = _nfkc(raw).lstrip("/").strip()
    s = re.sub(r"^天候\s*[:：]\s*", "", s).strip()
    if s and s not in KNOWN_WEATHERS:
        _warn_once("天候", raw)
    return s


def parse_weather_track(race_data01):
    """RaceData01 (BeautifulSoup の Tag) から (天候, 馬場) を返す。取れなければ ""。

    1) 旧書式の見出し語 (天候: / 馬場:) — 書式が戻っても読める
    2) 新書式: 天候 = class が Icon_Weather で始まる span の title / aria-label、
       馬場 = span.Item03 (芝) / span.Item04 (ダート)。レースの芝ダで優先する方を選ぶ
       (障害は芝。片方しか無ければそれ)
    3) 最後の手段として本文の部分一致 (旧実装と同じ ["不良","稍重","重","良"]。1文字の
       「不」「稍」は本文の別の語に紛れうるので部分一致には足さない)
    """
    if race_data01 is None:
        return "", ""
    text = race_data01.get_text(" ")
    weather, track = "", ""

    m = re.search(r"天候\s*[:：]\s*([^\s/]+)", text)
    if m:
        weather = normalize_weather(m.group(1))
    if not weather:
        for sp in race_data01.find_all("span"):
            cls = " ".join(sp.get("class") or [])
            if "Icon_Weather" in cls:
                weather = normalize_weather(sp.get("title") or sp.get("aria-label") or "")
                if weather:
                    break

    m = re.search(r"馬場\s*[:：]\s*([^\s/]+)", text)
    if m:
        track = normalize_track_condition(m.group(1))
    if not track:
        turf = race_data01.find("span", class_="Item03")
        dirt = race_data01.find("span", class_="Item04")
        tv = normalize_track_condition(turf.get_text()) if turf else ""
        dv = normalize_track_condition(dirt.get_text()) if dirt else ""
        # scraper._parse_race_info と同じ芝ダ判定 (「芝」を含めば芝 = 障害も芝)
        is_dirt = ("芝" not in text) and ("ダ" in text)
        track = (dv or tv) if is_dirt else (tv or dv)
    if not track:
        for cond in ("不良", "稍重", "重", "良"):
            if cond in text:
                track = _TRACK_CANON[cond]
                break
    return weather, track


def keep_existing_race_meta(race_id, data, conn=None):
    """data (race dict) の weather / track_condition が空なら、DB の既存の非空値で埋めた
    **コピー**を返す (data 自体は変えない)。新しい値が非空ならそのまま (上書きしてよい)。

    races の保存は INSERT OR REPLACE なので、空の値を渡すと既存の正しい値を消す
    (10/26 の取り直しで 10/24・10/25 の稍36・不16 が消えた)。天候・馬場以外の列には触らない。"""
    if not data or not race_id:
        return data
    w = (data.get("weather") or "").strip()
    t = (data.get("track_condition") or "").strip()
    if w and t:
        return data

    def _q(c):
        return c.execute("SELECT weather, track_condition FROM races WHERE race_id = ?",
                         (race_id,)).fetchone()
    try:
        if conn is not None:
            row = _q(conn)
        else:
            from database import get_db
            with get_db() as c:
                row = _q(c)
    except Exception as e:
        print(f"  ⚠️ #169 既存の天候・馬場の読み取りに失敗 ({race_id}): {e} → 取れた値のまま保存")
        return data
    if not row:
        return data
    out = dict(data)
    if not w and (row[0] or ""):
        out["weather"] = row[0]
    if not t and (row[1] or ""):
        out["track_condition"] = row[1]
    return out


# 確定済み (1着がある) JRA のレースだけを母数にする。未走・中止のレースは天候・馬場が
# 空でも異常ではないので数えない。
_FILL_SQL = """
    SELECT g.race_date,
           COUNT(*) AS n,
           SUM(CASE WHEN COALESCE(g.weather, '') <> '' THEN 1 ELSE 0 END) AS n_weather,
           SUM(CASE WHEN COALESCE(g.track_condition, '') <> '' THEN 1 ELSE 0 END) AS n_track
    FROM races g
    WHERE g.race_date >= ? AND g.race_date < ?
      AND CAST(substr(g.race_id, 5, 2) AS INT) BETWEEN 1 AND 10
      AND EXISTS (SELECT 1 FROM results r WHERE r.race_id = g.race_id AND r.finish_position = 1)
    GROUP BY g.race_date
    ORDER BY g.race_date
"""


def meta_fill_rates(conn, date_from, date_to):
    """[date_from, date_to) の開催日ごとの充足率 [{race_date, n, weather, track}] (0-1)。"""
    out = []
    for d, n, nw, nt in conn.execute(_FILL_SQL, (date_from, date_to)).fetchall():
        if not n:
            continue
        out.append({"race_date": d, "n": int(n), "weather": (nw or 0) / n, "track": (nt or 0) / n})
    return out


def low_meta_fill_dates(conn, date_from, date_to, threshold=0.9):
    """天候か馬場の充足率が threshold 未満の開催日 (meta_fill_rates の部分集合)。"""
    return [r for r in meta_fill_rates(conn, date_from, date_to)
            if r["weather"] < threshold or r["track"] < threshold]


def pick_meta_refetch_dates(low, exclude=(), now=None, k=2):
    """collect_results の自己修復で、天候・馬場を取り直す開催日を最大 k 日選ぶ (#169)。

    low: low_meta_fill_dates() の結果。exclude: 着順の欠けなど別の理由で既に取り直す日。

    新しい順に並べ、取り出す位置を実行ごとにずらす (状態ファイルを持たない輪番)。
    「新しい順に先頭 k 日」だけだと、取り直しても埋まらない開催日 (ある場の結果ページに
    天候が出ない等) が先頭にあると、その日が窓 (21日) を抜けるまで毎回選ばれ続け、
    それより古い開催日がずっと後回しになる (#169 レビュー)。取り直して埋まった日は
    low から消えるので、普段 (全部埋まる時) は輪番でも数回の実行で全日が埋まる。
    位置は JST の (日付, 2時間刻み) から決める: 土日 20:00 と 22:00 の回で別の k 日になる。
    """
    import datetime as _dt
    cand = sorted({r["race_date"] for r in low} - set(exclude), reverse=True)
    if len(cand) <= k:
        return cand
    now = now or _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9)))
    turn = now.date().toordinal() * 12 + now.hour // 2
    start = (turn * k) % len(cand)
    return [cand[(start + i) % len(cand)] for i in range(k)]
