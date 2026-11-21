#!/usr/bin/env python3
"""予測 JSON (docs/data/predictions_YYYYMMDD.json) の git 履歴を読む共通部品 (#169)。

なぜ必要か
----------
2026-11-05 の週次反省 (10/31・11/01) で、次の3つがどれも「後から確かめる材料が無い」
ことで潜伏していたと分かった。
  1. 9時台の再予測が race_day_runner の push で毎回巻き戻されていた (#68 以降・毎開催日)
  2. 11/01 未明に仮馬番の予想 JSON が 6時間18分公開されていた
  3. 「予測した時点のオッズ」がどこにも残っておらず、LGB と同時刻の市場を比べられない
DB (keiba.db.gz) の predictions_cache は並行 workflow の push で丸ごと上書きされ
(#70/#101/#141)、オッズも refresh_odds が発走直前まで書き換える (#99 の凍結が効かない)。
一方、GitHub Pages に出した予測 JSON は **公開した版がすべて git に残っている**。
ダッシュボードに何がいつ出ていたかの一次記録はこれしかない。

読み方の注意
------------
- 1日の JSON は 80〜90版ある (runner が10分ごとに書き出す)。版の時刻は commit 時刻
  (UTC → JST)。Pages への反映は commit の1〜2分後なので、公開時間はやや過大に出る。
- 版に入っているオッズは「その版を書き出した時点の DB のオッズ」で、予測に使ったオッズ
  とは限らない。runner は朝の予測 (pred_*) を抱えたまま refresh_odds 後のオッズで
  書き出すので、投稿前の最終版は「予測は 08:00・オッズは 10:2x」になる。
  予測に使ったオッズは、その予測の中身 (pred_top3_abs_pct 等) が **最初に現れた版**
  (= predict と同じ run の export) から取る。
- 予測時点の正本は今後 S 班のスナップショット (docs/data/snapshots/, prediction_snapshot.py)
  になる。あればそちらを優先し、どちらを使ったかを呼び出し側が出力に書く。
- 部分クローン (blob:none) では git show のたびに blob を1つずつ取りに行く。CI では
  prefetch() で一括取得してから読む (keiba.db.gz の過去版は決して取らない — #119 で
  .git が数十GBに膨れディスクが溢れた前科がある。ここが触るのは docs/data の JSON だけ)。
  手元の作業ツリーも blob:none の部分クローン (gc.auto=0) なので、1版ごとの遅延取得だと
  1 blob = 1 pack (約0.5秒) ずつ増える。remote.origin.partialclonefilter が設定されていれば
  既定で prefetch する (PRED_HISTORY_PREFETCH=0 で止める / =1 で常に行う)。
- git の呼び出しには時間制限を付ける (既定120秒)。ネットワーク待ちで止まると feature_parity の
  後続の監視 (起動層の生存確認 #155/#161) まで走らなくなるため。
"""
import datetime as dt
import glob
import json
import math
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JST = dt.timezone(dt.timedelta(hours=9))
RUNNER_SUBJECT = "Race day runner"
GIT_TIMEOUT = int(os.environ.get("PRED_HISTORY_GIT_TIMEOUT") or 120)


def pred_path(d8):
    return f"docs/data/predictions_{d8}.json"


def to_jst(ts):
    """commit 時刻 (epoch 秒) → JST の datetime"""
    return dt.datetime.fromtimestamp(int(ts), JST)


def parse_jst(s):
    """'2026-10-31T10:25:37+09:00' / '2026-10-31 10:25' などを JST datetime に。失敗は None"""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            t = dt.datetime.fromisoformat(s) if fmt is None else dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        return t.replace(tzinfo=JST) if t.tzinfo is None else t.astimezone(JST)
    return None


class History:
    """repo の git 履歴から1日分の予測 JSON の版を読む。

    repo: 履歴を持つ clone (CI では feature_parity.yml が作る /tmp/hist)。
    cache_dir: 読んだ版を {d8}_{sha}.json で置く場所 (任意)。同じ版を2度取りに行かない。
    """

    def __init__(self, repo=None, ref="HEAD", cache_dir=None):
        self.repo = repo or ROOT
        self.ref = ref
        self.cache_dir = cache_dir
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        self._mem = {}
        self._auto_prefetch = None

    def _git(self, *args, check=True, binary=False, timeout=None):
        try:
            r = subprocess.run(["git", "-C", self.repo, *args], capture_output=True,
                               timeout=timeout or GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            # #169: 時間切れは失敗として握る (check=False の呼び出しは「読めなかった」扱いになる)
            if check:
                raise RuntimeError(f"git {' '.join(args[:2])} が {timeout or GIT_TIMEOUT}秒で終わらない")
            return b"" if binary else ""
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])} 失敗: {r.stderr.decode(errors='replace')[:300]}")
        return r.stdout if binary else r.stdout.decode("utf-8", errors="replace")

    def auto_prefetch(self):
        """版を読む前に blob を一括取得するか。PRED_HISTORY_PREFETCH=1/0 が優先、
        無ければ部分クローン (remote.origin.partialclonefilter あり) なら行う。"""
        env = os.environ.get("PRED_HISTORY_PREFETCH")
        if env in ("0", "1"):
            return env == "1"
        if self._auto_prefetch is None:
            self._auto_prefetch = bool(self._git("config", "--get", "remote.origin.partialclonefilter",
                                                 check=False).strip())
        return self._auto_prefetch

    def maybe_prefetch(self, d8, versions):
        if versions and self.auto_prefetch():
            try:
                self.prefetch(d8, versions)
            except (RuntimeError, OSError):
                pass        # 取れなければ1版ずつの遅延取得に任せる

    def shallow_boundary(self):
        """shallow clone なら、残っている最古の commit の時刻 (JST)。shallow でなければ None。
        それより前の版は履歴に無いので、境界の直後の日は「最初に現れた版」を取り違える。"""
        if self._git("rev-parse", "--is-shallow-repository", check=False).strip() != "true":
            return None
        out = self._git("log", "--format=%ct", self.ref, check=False).split()
        return to_jst(out[-1]) if out else None

    def forget(self, d8):
        """その日の版をメモリから捨てる (cache_dir があればディスクには残る)。
        累計の集計で読んだ版を全部抱えると、週を追うごとにメモリが線形に増えるため。"""
        for k in [k for k in self._mem if k[0] == d8]:
            del self._mem[k]

    def available(self):
        """履歴が読めるか (git でない・shallow で1版しか無い等を呼び出し側が判断する材料)"""
        try:
            self._git("rev-parse", "--verify", self.ref)
            return True
        except (RuntimeError, OSError):
            return False

    def versions(self, d8, since=None, until=None):
        """その日の JSON の版を古い順に返す: [{'sha','at'(JST),'subject','runner'}]

        since/until (JST datetime) で commit 時刻を絞る (until は含まない)。
        """
        out = self._git("log", "--format=%H%x09%ct%x09%s", self.ref, "--", pred_path(d8), check=False)
        vs = []
        for line in out.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            sha, ct, subj = parts
            at = to_jst(ct)
            if since and at < since:
                continue
            if until and at >= until:
                continue
            vs.append({"sha": sha, "at": at, "subject": subj, "runner": RUNNER_SUBJECT in subj})
        vs.reverse()
        return vs

    def _cache_file(self, d8, sha):
        if not self.cache_dir:
            return None
        hit = sorted(glob.glob(os.path.join(self.cache_dir, f"{d8}_{sha[:8]}*.json")))
        return hit[0] if hit else os.path.join(self.cache_dir, f"{d8}_{sha[:12]}.json")

    def load(self, d8, sha):
        """その版の JSON (dict)。読めなければ None"""
        key = (d8, sha)
        if key in self._mem:
            return self._mem[key]
        fp = self._cache_file(d8, sha)
        data = None
        if fp and os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = None
        if data is None:
            raw = self._git("show", f"{sha}:{pred_path(d8)}", check=False, binary=True)
            try:
                data = json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                data = None
            if data is not None and fp:
                try:
                    with open(fp, "wb") as f:
                        f.write(raw)
                except OSError:
                    pass
        self._mem[key] = data
        return data

    def prefetch(self, d8, versions):
        """部分クローンで、未取得の blob を1回の fetch でまとめて取る (CI 用、失敗は握る)。

        取るのは docs/data/predictions_*.json の blob だけ。keiba.db.gz には触れない。
        """
        need = [v["sha"] for v in versions
                if not (self._cache_file(d8, v["sha"]) and os.path.exists(self._cache_file(d8, v["sha"])))]
        if not need:
            return 0
        oids = []
        for i in range(0, len(need), 200):
            out = self._git("rev-parse", *[f"{s}:{pred_path(d8)}" for s in need[i:i + 200]], check=False)
            oids += [x for x in out.split() if len(x) == 40]
        if not oids:
            return 0
        for i in range(0, len(oids), 200):
            self._git("-c", "fetch.negotiationAlgorithm=noop", "fetch", "--no-tags",
                      "--no-write-fetch-head", "--recurse-submodules=no", "--filter=blob:none",
                      "origin", *oids[i:i + 200], check=False)
        return len(oids)

    def file_at(self, path, at):
        """任意のファイルの「at (JST) 時点」の版 (dict)。race_baseline.json の閾値など。"""
        out = self._git("log", "-1", "--format=%H", f"--before={at.isoformat()}", self.ref,
                        "--", path, check=False).strip()
        if not out:
            return None
        raw = self._git("show", f"{out}:{path}", check=False, binary=True)
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return None


# ───────────────────────── 版の中身を比べる部品 ─────────────────────────

def races_of(doc):
    """{race_id: race dict}"""
    out = {}
    for _v, rs in ((doc or {}).get("venues") or {}).items():
        for r in rs or []:
            if r.get("race_id"):
                out[str(r["race_id"])] = r
    return out


def pred_fingerprint(race):
    """予測の中身 (モデル出力と印) の指紋。オッズ・着順・払戻のように予測後に正当に
    変わる列は含めない。同じ指紋 = 同じ予測。"""
    hs = race.get("horses") or []
    return tuple(sorted(
        (h.get("horse_number"), h.get("pred_top3_abs_pct"), h.get("pred_win_pct"),
         h.get("rank_score"), h.get("mark") or "")
        for h in hs))


def lgb_vector(race):
    """LGB 出力だけの指紋 (印は点数表なので含めない)。予測時オッズの版を探す時に使う。"""
    hs = race.get("horses") or []
    return tuple(sorted((h.get("horse_number"), h.get("pred_top3_abs_pct"), h.get("pred_win_pct"),
                         h.get("rank_score")) for h in hs))


def marks_of(race):
    """{(印, 馬番)} 集合"""
    return {(h.get("mark"), h.get("horse_number")) for h in race.get("horses") or [] if h.get("mark")}


def load_posted_marks(d8, data_dir=None):
    p = os.path.join(data_dir or os.path.join(ROOT, "docs", "data"), f"posted_marks_{d8}.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def posted_mark_set(entries):
    return {(e.get("mark"), e.get("horse_number")) for e in entries or [] if e.get("mark")}


# ───────────────────────── 予測時点のオッズ ─────────────────────────

def snapshot_module():
    """S 班の prediction_snapshot (予測時点の正本)。無ければ None"""
    try:
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import prediction_snapshot as ps  # noqa: F401
        return ps
    except Exception:
        return None


def predtime_from_snapshot(d8, at=None):
    """スナップショットから race_id → {horse_number: (odds, popularity)} と予測 (LGB 等)。
    無ければ {}。"""
    ps = snapshot_module()
    if ps is None or not hasattr(ps, "latest_before"):
        return {}
    try:
        recs = ps.latest_before(d8, at)
    except Exception:
        return {}
    out = {}
    for rid, rec in (recs or {}).items():
        hs = {}
        for h in rec.get("horses") or []:
            hn = h.get("horse_number")
            if hn is None:
                continue
            hs[int(hn)] = h
        if hs:
            out[str(rid)] = {"horses": hs, "at": parse_jst(rec.get("created_at")),
                             "source": "snapshot"}
    return out


def predtime_from_git(hist, d8, cutoff, versions=None):
    """git 履歴から、各レースについて

      pre  = cutoff (JST) より前の最後の版 (= その時刻にダッシュボードに出ていた版)
      same = pre と同じ LGB 出力が最初に現れた版 (= 予測した run の export。オッズ = 予測に使った値)

    を返す: {race_id: {'pre': race, 'pre_at', 'same': race, 'same_at'}}
    """
    vs = versions if versions is not None else hist.versions(d8, until=cutoff)
    vs = [v for v in vs if v["at"] < cutoff]
    if not vs:
        return {}
    hist.maybe_prefetch(d8, vs)
    last = hist.load(d8, vs[-1]["sha"])
    pre = races_of(last)
    out = {rid: {"pre": r, "pre_at": vs[-1]["at"], "same": None, "same_at": None}
           for rid, r in pre.items()}
    want = {rid: lgb_vector(r) for rid, r in pre.items()}
    for v in vs:            # 古い順 = 最初に現れた版
        if all(o["same"] is not None for o in out.values()):
            break
        doc = hist.load(d8, v["sha"])
        for rid, r in races_of(doc).items():
            o = out.get(rid)
            if o is None or o["same"] is not None:
                continue
            if lgb_vector(r) == want[rid]:
                o["same"] = r
                o["same_at"] = v["at"]
    return out


# ───────────────────────── 投資ゲートの遮断率 ─────────────────────────

def gate_values(race):
    """意思決定チャネル (温度1 softmax = pred_win_norm) の ◎勝率と上位3頭合計。
    build_race_baseline._gate_samples と同じ復元 (rank_score から)。取れなければ None"""
    rs = [h.get("rank_score") for h in race.get("horses") or [] if h.get("rank_score") is not None]
    if len(rs) < 3:
        return None
    mx = max(rs)
    ex = [math.exp(x - mx) for x in rs]
    tot = sum(ex) or 1.0
    pr = sorted((v / tot for v in ex), reverse=True)
    return pr[0], sum(pr[:3])


GATE_TARGET = {"top_prob": 0.30, "top3_sum": 0.25}   # build_race_baseline の TARGET と同じ
# 1週末の平場は 26〜50R しか無く、遮断率の標本誤差は ±7〜9pt ある。固定幅だと誤報が多いので
# 「目標からの差が 10pt 以上 かつ 二項分布で |z|≥2.58 (両側1%)」のときだけ外れとする。
# #156 で実際に起きた 1.7% / 70.1% のような外れは n=26 でも |z|>3 で必ず捕まる。
GATE_MIN_DIFF = 0.10
GATE_Z = 2.58
GATE_MIN_N = 20


def gate_verdict(gates):
    """複数日の遮断率をまとめて目標と比べる。gates: gate_block_rates の結果 + 'date' の list。

    返り値: (行 or None, 外れているか)
    """
    gates = [g for g in gates if g.get("n")]
    n = sum(g["n"] for g in gates)
    if n < GATE_MIN_N:
        return None, False
    rt = sum(g["top_prob"] * g["n"] for g in gates) / n
    rs = sum(g["top3_sum"] * g["n"] for g in gates) / n

    def _stat(rate, tgt):
        se = (tgt * (1 - tgt) / n) ** 0.5
        z = (rate - tgt) / se
        # この標本で「外れ」と判定できる最小の差 (10pt と |z|≥2.58 の大きい方)
        mde = max(GATE_MIN_DIFF, GATE_Z * se)
        return z, mde, abs(rate - tgt) >= GATE_MIN_DIFF and abs(z) >= GATE_Z
    zt, mt, ot = _stat(rt, GATE_TARGET["top_prob"])
    zs, ms, os_ = _stat(rs, GATE_TARGET["top3_sum"])
    # #169 (レビュー指摘): 旧文言は判定に届かない時「目標の範囲内 (標本誤差の内)」と言い切っていた。
    # 1週末の平場 n≈26 では約23pt ずれないと検出できず、10/31・11/01 の top_prob 46% (目標30%、
    # +16pt) も「範囲内」と出た。差・z・検出できる差の大きさをそのまま出す。
    msg = (f"投資ゲート遮断率 ({'・'.join(g['date'] for g in gates)} 平場{n}R): "
           f"top_prob {rt:.0%} (目標{GATE_TARGET['top_prob']:.0%}、差 {100*(rt-GATE_TARGET['top_prob']):+.0f}pt"
           f"・z={zt:+.2f}) / top3_sum {rs:.0%} (目標{GATE_TARGET['top3_sum']:.0%}、"
           f"差 {100*(rs-GATE_TARGET['top3_sum']):+.0f}pt・z={zs:+.2f})")
    if ot or os_:
        return msg + " — 目標から統計的に外れている (#156 の再発疑い。閾値と分布がペアになっていない)", True
    return (msg + f" — この標本では目標と区別できない (検出できる差は top_prob 約±{100*mt:.0f}pt・"
            f"top3_sum 約±{100*ms:.0f}pt。目標どおりという意味ではない)"), False


def gate_block_rates(races, thresholds, is_ood):
    """平場 (障害・未勝利・新馬を除く) のレースで、混戦判定が弾いた割合。

    races: race dict の iterable / thresholds: {'top_prob','top3_sum'}
    返り値: {'n', 'top_prob': 率, 'top3_sum': 率, 'either': 率}
    """
    n = bt = bs = be = 0
    for r in races:
        if is_ood(r.get("race_name") or ""):
            continue
        gv = gate_values(r)
        if gv is None:
            continue
        top, s3 = gv
        n += 1
        a = top < thresholds["top_prob"]
        b = s3 < thresholds["top3_sum"]
        bt += a
        bs += b
        be += (a or b)
    if not n:
        return {"n": 0}
    return {"n": n, "top_prob": bt / n, "top3_sum": bs / n, "either": be / n}
