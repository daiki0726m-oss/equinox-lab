"""
競馬予測メインスクリプト
データ収集 → 分析 → 予測 → 推奨馬券出力 の全フローを実行
"""

import argparse
import json
import sys
import os
from datetime import datetime, timezone, timedelta

# プロジェクトルートをパスに追加
sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, get_db
from scraper import NetkeibaScraper
from ml.model import KeibaModel
from ml.features import FeatureBuilder
from strategy.betting import BettingStrategy
from analyzers.speed_index import SpeedIndexCalculator
from analyzers.odds_value import OddsValueAnalyzer

# APIオッズキャッシュ（レースIDごとに1回だけAPI呼び出しするため）
_api_odds_cache = {}

# ── 能力モデル (オッズ非依存) ローダー (#72) ──
# 2020-2025 backtest で「能力◎」単勝ROI 112% (現◎ 92% / +20pt, 全6年100%超) を実証。
# 各馬の「市場を見ない実力評価」を attach し、◎ の2軸化 + ★妙味 + ⚠️危険な人気 に使う。
_ABILITY_MODEL = None  # (model, calibrator, feature_cols) or False(=ロード失敗)


def _load_ability_model():
    """能力モデルを一度だけロード (失敗時は False を返し以後 skip)。"""
    global _ABILITY_MODEL
    if _ABILITY_MODEL is not None:
        return _ABILITY_MODEL
    try:
        import pickle
        from fast_train import get_feature_columns
        mdir = os.path.join(os.path.dirname(__file__), "models")
        with open(os.path.join(mdir, "model_ability_win.pkl"), "rb") as f:
            model = pickle.load(f)
        with open(os.path.join(mdir, "calibrator_ability_win.pkl"), "rb") as f:
            calib = pickle.load(f)
        cols = [c for c in get_feature_columns() if c not in ("odds_log", "popularity_norm")]
        _ABILITY_MODEL = (model, calib, cols)
    except Exception as e:
        print(f"  ⚠️ 能力モデル未ロード ({e}) → ability_score=0 で継続")
        _ABILITY_MODEL = False
    return _ABILITY_MODEL


def _attach_ability_score(pred_df):
    """pred_df に _ability_score 列 (レース内正規化済み勝率) を付与。
    特徴量列が揃っていれば計算、失敗時は 0。純加算なので既存挙動は不変。"""
    am = _load_ability_model()
    if not am:
        pred_df["_ability_score"] = 0.0
        return pred_df
    model, calib, cols = am
    try:
        missing = [c for c in cols if c not in pred_df.columns]
        if missing:
            pred_df["_ability_score"] = 0.0
            return pred_df
        X = pred_df[cols].fillna(0)
        raw = model.predict(X, num_iteration=getattr(model, "best_iteration", None))
        p = calib.predict(raw)
        s = p.sum()
        pred_df["_ability_score"] = (p / s) if s > 0 else p
    except Exception as e:
        print(f"  ⚠️ 能力スコア計算失敗 ({e}) → 0")
        pred_df["_ability_score"] = 0.0
    return pred_df


def cmd_collect(args):
    """データ収集コマンド"""
    scraper = NetkeibaScraper()

    if args.date:
        # 特定日のレースを収集
        print(f"🏇 {args.date} のレースデータを収集...")
        race_ids = scraper.get_race_list_by_date(args.date)
        for i, rid in enumerate(race_ids):
            print(f"  [{i+1}/{len(race_ids)}] {rid}")
            data = scraper.scrape_race_result(rid)
            if data and data.get("results"):
                scraper.save_race_to_db(data)
            else:
                # 未来のレース: 出馬表から取得
                shutuba = scraper.scrape_shutuba(rid)
                if shutuba and shutuba.get("entries"):
                    with get_db() as conn:
                        conn.execute("""
                            INSERT OR REPLACE INTO races
                            (race_id, race_date, venue, race_number, race_name, grade,
                             distance, surface, direction, weather, track_condition, horse_count, start_time)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            rid, shutuba.get("race_date", ""),
                            shutuba.get("venue", ""), shutuba.get("race_number", 0),
                            shutuba.get("race_name", ""), shutuba.get("grade", ""),
                            shutuba.get("distance", 0), shutuba.get("surface", ""),
                            shutuba.get("direction", ""), shutuba.get("weather", ""),
                            shutuba.get("track_condition", ""),
                            len(shutuba.get("entries", [])),
                            shutuba.get("start_time", "")
                        ))
                        for e in shutuba.get("entries", []):
                            if e.get("horse_id"):
                                conn.execute("""
                                    INSERT OR IGNORE INTO horses (horse_id, horse_name, sex)
                                    VALUES (?, ?, ?)
                                """, (e["horse_id"], e.get("horse_name", ""), e.get("sex", "")))
                            if e.get("jockey_id"):
                                conn.execute("""
                                    INSERT OR IGNORE INTO jockeys (jockey_id, jockey_name)
                                    VALUES (?, ?)
                                """, (e["jockey_id"], e.get("jockey_name", "")))
                            if e.get("trainer_id"):
                                conn.execute("""
                                    INSERT OR IGNORE INTO trainers (trainer_id, trainer_name)
                                    VALUES (?, ?)
                                """, (e["trainer_id"], e.get("trainer_name", "")))
                            conn.execute("""
                                INSERT OR IGNORE INTO results
                                (race_id, horse_id, jockey_id, trainer_id,
                                 post_position, horse_number, odds, popularity,
                                 finish_position, finish_time, finish_time_seconds,
                                 margin, last_3f, passing_order, weight, weight_change, impost)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                rid, e.get("horse_id", ""),
                                e.get("jockey_id", ""), e.get("trainer_id", ""),
                                e.get("post_position", 0), e.get("horse_number", 0),
                                0, 0, 0, "", 0, "", 0, "", 0, 0, e.get("impost", 0)
                            ))
                    print(f"  📋 出馬表保存: {len(shutuba['entries'])}頭")
                else:
                    print(f"  ⚠️ データ取得失敗: {rid}")
    else:
        # 期間指定で収集
        start_y = args.start_year or 2024
        start_m = args.start_month or 1
        end_y = args.end_year or start_y
        end_m = args.end_month or 12
        scraper.collect_range(start_y, start_m, end_y, end_m)

    # 収集結果を表示
    with get_db() as conn:
        race_count = conn.execute("SELECT COUNT(*) as c FROM races").fetchone()["c"]
        result_count = conn.execute("SELECT COUNT(*) as c FROM results").fetchone()["c"]
        print(f"\n📊 DB状況: {race_count}レース / {result_count}出走データ")


def cmd_train(args):
    """モデル学習コマンド（fast_train.pyに委譲）"""
    import subprocess
    script = os.path.join(os.path.dirname(__file__), "fast_train.py")
    print("🧠 高速学習パイプラインを起動...")
    print("   (fast_train.py に委譲)\n")
    result = subprocess.run(
        [sys.executable, script],
        cwd=os.path.dirname(__file__)
    )
    sys.exit(result.returncode)


def cmd_predict(args):
    """レース予測コマンド"""
    model = KeibaModel()
    strategy = BettingStrategy()
    speed_calc = SpeedIndexCalculator()

    if args.race_id:
        race_ids = [args.race_id]
    elif args.date:
        scraper = NetkeibaScraper()
        race_ids = scraper.get_race_list_by_date(args.date)
    else:
        print("❌ --race-id か --date を指定してください")
        return

    # silent skip 検知: 各continueでreason付きで記録
    _predicted_count = 0
    _skipped_races = []  # [(race_id, reason), ...]

    # ─── 10時ロック: 既存キャッシュがある場合は再予測を禁止 ───
    now_hour = datetime.now(timezone(timedelta(hours=9))).hour
    force = getattr(args, 'force', False)
    if not force and now_hour >= 10 and race_ids:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT COUNT(*) as c FROM predictions_cache WHERE race_id = ?",
                (race_ids[0],)
            ).fetchone()
            if existing and existing['c'] > 0:
                print("🔒 10時以降のため予測を再実行しません（既存キャッシュを使用）")
                print("   強制実行する場合: --force オプションを追加")
                return

    # UI-3 fix: 予測前に馬場状態が空のレースがあれば取得
    if race_ids:
        try:
            from refresh_odds import fetch_track_condition
            venues_checked = set()
            with get_db() as conn:
                for rid in race_ids:
                    r = conn.execute(
                        "SELECT venue, track_condition, race_date FROM races WHERE race_id = ?",
                        (rid,)
                    ).fetchone()
                    if r and not r["track_condition"] and r["venue"] not in venues_checked:
                        venues_checked.add(r["venue"])
                        track_info = fetch_track_condition(rid)
                        if track_info and track_info.get("track_condition"):
                            tc = track_info["track_condition"]
                            wt = track_info.get("weather", "")
                            conn.execute("""
                                UPDATE races SET track_condition = ?, weather = ?
                                WHERE venue = ? AND (race_date = ? OR race_date = ?)
                                  AND (track_condition IS NULL OR track_condition = '')
                            """, (tc, wt, r["venue"], r["race_date"],
                                  r["race_date"].replace("-", "")))
                            print(f"  🏇 {r['venue']}: 馬場={tc} 天候={wt}")
            if venues_checked:
                import time as _t; _t.sleep(0.5)
        except Exception as e:
            print(f"  ⚠️ 馬場状態の取得をスキップ: {e}")

    for race_id in race_ids:
        print(f"\n{'='*60}")
        print(f"🏇 レース予測: {race_id}")
        print(f"{'='*60}")

        # ── 投稿済 seal チェック ──
        # 一度 post_predict された予測は、--force でも上書き禁止。
        # (結果配信時に「投稿時の予測」と異なる予測を引かないため)
        from database import is_prediction_sealed
        if is_prediction_sealed(race_id):
            print(f"🔒 {race_id}: 投稿済(seal)のため再予測スキップ")
            _skipped_races.append((race_id, '投稿済 seal'))
            continue

        # レース情報取得
        with get_db() as conn:
            race = conn.execute(
                "SELECT * FROM races WHERE race_id = ?", (race_id,)
            ).fetchone()
            results = conn.execute("""
                SELECT r.*, h.horse_name, j.jockey_name
                FROM results r
                LEFT JOIN horses h ON r.horse_id = h.horse_id
                LEFT JOIN jockeys j ON r.jockey_id = j.jockey_id
                WHERE r.race_id = ?
                ORDER BY r.horse_number
            """, (race_id,)).fetchall()

        if not race:
            # 未来レース: 出馬表をスクレイピングして保存
            print(f"  📡 出馬表を取得中...")
            scraper = NetkeibaScraper()
            shutuba = scraper.scrape_shutuba(race_id)
            if shutuba and shutuba.get("entries"):
                # レース情報をDBに保存
                with get_db() as conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO races
                        (race_id, race_date, venue, race_number, race_name, grade,
                         distance, surface, direction, weather, track_condition, horse_count, start_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        race_id, shutuba.get("race_date", ""),
                        shutuba.get("venue", ""), shutuba.get("race_number", 0),
                        shutuba.get("race_name", ""), shutuba.get("grade", ""),
                        shutuba.get("distance", 0), shutuba.get("surface", ""),
                        shutuba.get("direction", ""), shutuba.get("weather", ""),
                        shutuba.get("track_condition", ""),
                        len(shutuba.get("entries", [])),
                        shutuba.get("start_time", "")
                    ))

                    # 各馬のエントリーをresultsテーブルに保存（finish_position=0で未確定）
                    entries = shutuba.get("entries", [])
                    # 🆕 幽霊馬対策 (#43): 最新スクレイプの馬番に無い結果未確定レコードを削除
                    # (枠順確定前の仮馬番が確定後も残る「幽霊馬」を根絶。save_race_to_db と同じ)
                    _snums = [e.get("horse_number", 0) for e in entries if e.get("horse_number")]
                    if _snums:
                        _ph = ",".join("?" * len(_snums))
                        conn.execute(
                            f"DELETE FROM results WHERE race_id = ? AND finish_position = 0 "
                            f"AND horse_number NOT IN ({_ph})",
                            (race_id, *_snums)
                        )
                    for e in entries:
                        if e.get("horse_id"):
                            conn.execute("""
                                INSERT OR IGNORE INTO horses (horse_id, horse_name, sex)
                                VALUES (?, ?, ?)
                            """, (e["horse_id"], e.get("horse_name", ""), e.get("sex", "")))
                        if e.get("jockey_id"):
                            conn.execute("""
                                INSERT OR IGNORE INTO jockeys (jockey_id, jockey_name)
                                VALUES (?, ?)
                            """, (e["jockey_id"], e.get("jockey_name", "")))
                        if e.get("trainer_id"):
                            conn.execute("""
                                INSERT OR IGNORE INTO trainers (trainer_id, trainer_name)
                                VALUES (?, ?)
                            """, (e["trainer_id"], e.get("trainer_name", "")))

                        conn.execute("""
                            INSERT OR REPLACE INTO results
                            (race_id, horse_id, jockey_id, trainer_id,
                             post_position, horse_number, odds, popularity,
                             finish_position, finish_time, finish_time_seconds,
                             margin, last_3f, passing_order, weight, weight_change, impost)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            race_id, e.get("horse_id", ""),
                            e.get("jockey_id", ""), e.get("trainer_id", ""),
                            e.get("post_position", 0), e.get("horse_number", 0),
                            0, 0, 0, "", 0, "", 0, "", 0, 0,
                            e.get("impost", 0)
                        ))

                # 保存後に再取得
                with get_db() as conn:
                    race = conn.execute(
                        "SELECT * FROM races WHERE race_id = ?", (race_id,)
                    ).fetchone()
                    results = conn.execute("""
                        SELECT r.*, h.horse_name, j.jockey_name
                        FROM results r
                        LEFT JOIN horses h ON r.horse_id = h.horse_id
                        LEFT JOIN jockeys j ON r.jockey_id = j.jockey_id
                        WHERE r.race_id = ?
                        ORDER BY r.horse_number
                    """, (race_id,)).fetchall()
                print(f"  ✅ {len(results)}頭の出馬表を取得")
            else:
                print(f"  ⚠️ 出馬表を取得できませんでした")
                _skipped_races.append((race_id, '出馬表取得失敗'))
                continue

        if not race:
            print(f"  ⚠️ レースデータが見つかりません")
            _skipped_races.append((race_id, 'レースデータ無し'))
            continue

        race_info = dict(race)

        # ── 🆕 #52 (2026-06-08): オッズ充足ゲート (全D化の最終防御) ──
        # model_rank は gain の 92% が odds_log。出走馬の odds が全頭 0 のまま予測すると
        # rank_score がフラット化し、全レース confidence=D の degenerate 予測になる
        # (6/7 の事故)。予測の「前」に odds を確認し、全頭0なら API 取得を試みる。
        # それでも0なら degenerate を書かず、このレースを skip して既存 cache を保持する。
        # (refresh_odds が後で走れば次サイクルで正常予測される)
        if not getattr(args, 'allow_zero_odds', False):
            with get_db() as oconn:
                odds_rows = oconn.execute(
                    "SELECT odds FROM results WHERE race_id = ?", (race_id,)
                ).fetchall()
            n_horses = len(odds_rows)
            n_zero = sum(1 for r in odds_rows if not (r['odds'] and r['odds'] > 0))
            if n_horses > 0 and n_zero == n_horses:
                print(f"  ⚠️ 全{n_horses}頭 odds=0 → 予測前に API でオッズ取得を試行")
                try:
                    from refresh_odds import fetch_odds_from_api
                    api_odds = fetch_odds_from_api(race_id)
                    if api_odds:
                        with get_db() as oconn:
                            for hn, od in api_odds.items():
                                win = od.get('win_odds', 0) if isinstance(od, dict) else od
                                if win and win > 0:
                                    oconn.execute(
                                        "UPDATE results SET odds = ? WHERE race_id = ? AND horse_number = ?",
                                        (win, race_id, int(hn))
                                    )
                        print(f"  📡 API オッズ {len(api_odds)}頭分を反映")
                except Exception as e:
                    print(f"  ⚠️ API オッズ取得失敗: {e}")
                # 再確認: まだ全頭0なら degenerate 予測を避けて skip
                with get_db() as oconn:
                    odds_rows2 = oconn.execute(
                        "SELECT odds FROM results WHERE race_id = ?", (race_id,)
                    ).fetchall()
                n_zero2 = sum(1 for r in odds_rows2 if not (r['odds'] and r['odds'] > 0))
                if n_zero2 == len(odds_rows2) and len(odds_rows2) > 0:
                    print(f"  🚫 オッズ取得不能 (全頭0) → degenerate 予測を避けて skip (既存cache保持)")
                    _skipped_races.append((race_id, 'オッズ全頭0 (degenerate回避)'))
                    continue

        # 予測
        try:
            pred_df = model.predict_race(race_id)
        except ValueError as e:
            print(f"  ⚠️ {e}")
            _skipped_races.append((race_id, f'predict_race: {e}'))
            continue
        except Exception as e:
            print(f"  ❌ 予期しないエラー: {e}")
            _skipped_races.append((race_id, f'予期しないエラー: {e}'))
            continue

        if pred_df.empty:
            print(f"  ⚠️ 予測データを構築できません")
            _skipped_races.append((race_id, '予測データ空'))
            continue

        # 能力スコア (オッズ非依存) を付与 (#72、純加算で既存挙動に影響なし)
        pred_df = _attach_ability_score(pred_df)

        # ── 追い切り (workout) 補正 — ML 統合できない過去データ不足を補う ──
        # workouts は 2026年シーズン分のみ (513件) で、ML 学習データに統合できない。
        # 代替として予測後に grade A/B/C で pred_win/pred_top3 を後処理スケール。
        # A: 「気力充実」「好調持続」級 → +10%、B: 標準 → ±0、C: 仕上がり微妙 → -7%
        try:
            with get_db() as wconn:
                wrows = wconn.execute(
                    "SELECT horse_number, evaluation_grade FROM workouts WHERE race_id = ?",
                    (race_id,)
                ).fetchall()
                workout_map = {r['horse_number']: r['evaluation_grade'] for r in wrows}
        except Exception:
            workout_map = {}

        if workout_map:
            WORKOUT_BOOST = {'A': 1.10, 'B': 1.00, 'C': 0.93}
            for idx in pred_df.index:
                hn = int(pred_df.at[idx, 'horse_number'])
                grade = workout_map.get(hn)
                if grade and grade in WORKOUT_BOOST and WORKOUT_BOOST[grade] != 1.0:
                    pred_df.at[idx, 'pred_win_norm'] *= WORKOUT_BOOST[grade]
                    pred_df.at[idx, 'pred_top3_norm'] *= WORKOUT_BOOST[grade]
            # 再正規化 (合計 1.0 を維持)
            total_win = pred_df['pred_win_norm'].sum()
            if total_win > 0:
                pred_df['pred_win_norm'] = pred_df['pred_win_norm'] / total_win
            print(f"  💪 追い切り補正: {len(workout_map)}頭分 (A:+10% / C:-7%)")

        # ── 馬体重変化補正 (Phase α #31: 2026-05-27) ──
        # 競馬専門家の経験則: 馬体重 ±6kg 以上 は調子変動の signal、ML model に
        # 入っていないので post-prediction 補正。
        #   ±2 以内: 1.00 (理想)
        #   ±3〜5:  0.97 (やや変動)
        #   ±6〜9:  0.90 (大幅変動、警戒)
        #   ±10+:   0.80 (異常変動、危険)
        # この補正は post-ML なので「絶対能力」ではなく「当日コンディション」を反映
        weight_change_map = {}
        for r in results:
            hn = r.get("horse_number") if hasattr(r, 'get') else r["horse_number"]
            wc = r.get("weight_change") if hasattr(r, 'get') else r["weight_change"]
            if hn and wc is not None and wc != 0:
                weight_change_map[int(hn)] = wc

        def wc_factor(wc):
            if wc is None: return 1.0
            absw = abs(wc)
            if absw <= 2: return 1.00
            if absw <= 5: return 0.97
            if absw <= 9: return 0.90
            return 0.80

        if weight_change_map:
            adjusted = 0
            for idx in pred_df.index:
                hn = int(pred_df.at[idx, 'horse_number'])
                wc = weight_change_map.get(hn)
                if wc is not None:
                    factor = wc_factor(wc)
                    if factor != 1.0:
                        pred_df.at[idx, 'pred_win_norm'] *= factor
                        pred_df.at[idx, 'pred_top3_norm'] *= factor
                        adjusted += 1
            total_win = pred_df['pred_win_norm'].sum()
            if total_win > 0:
                pred_df['pred_win_norm'] = pred_df['pred_win_norm'] / total_win
            if adjusted > 0:
                print(f"  ⚖️ 馬体重変化補正: {adjusted}頭 (±6kg+ -10%, ±10kg+ -20%)")

        # ── 馬場バイアス補正 (Phase α #32, Track 1 完成版: 2026-05-27) ──
        # 同日 R9+ で同 venue の R1-R8 結果から馬場バイアス (内伸び/外伸び) を推定。
        # 検出された場合は post-prediction で枠順別に補正。
        race_number_now = race_info.get("race_number") or 0
        venue_now = race_info.get("venue") or ""
        race_date_now = race_info.get("race_date") or ""
        if race_number_now >= 9 and venue_now and race_date_now:
            try:
                from scripts.track_bias_detector import detect_track_bias
                with get_db() as bconn:
                    bias = detect_track_bias(bconn, race_date_now, venue_now)
                if bias and bias.get("confidence") != "low":
                    fb = bias.get("frame_bias")
                    if fb in ("inside", "outside"):
                        # post_position による補正
                        pp_map = {}
                        for r in results:
                            hn = r.get("horse_number") if hasattr(r, 'get') else r["horse_number"]
                            pp = r.get("post_position") if hasattr(r, 'get') else r["post_position"]
                            if hn and pp:
                                pp_map[int(hn)] = int(pp)
                        adj_count = 0
                        for idx in pred_df.index:
                            hn = int(pred_df.at[idx, 'horse_number'])
                            pp = pp_map.get(hn, 0)
                            factor = 1.0
                            if fb == "inside":
                                if pp <= 3: factor = 1.08
                                elif pp >= 6: factor = 0.92
                            elif fb == "outside":
                                if pp >= 6: factor = 1.08
                                elif pp <= 3: factor = 0.92
                            if factor != 1.0:
                                pred_df.at[idx, 'pred_win_norm'] *= factor
                                pred_df.at[idx, 'pred_top3_norm'] *= factor
                                adj_count += 1
                        total_win = pred_df['pred_win_norm'].sum()
                        if total_win > 0:
                            pred_df['pred_win_norm'] = pred_df['pred_win_norm'] / total_win
                        if adj_count > 0:
                            print(f"  🏁 馬場バイアス補正: {fb} bias, {adj_count}頭調整 (内 {bias['details']['inside_rate']*100:.0f}% / 外 {bias['details']['outside_rate']*100:.0f}%)")
            except Exception as e:
                # 静かに skip (バイアス検出は best-effort)
                pass

        # 予測結果表示
        print(f"\n📊 予測結果: {race_info.get('race_name', '')} "
              f"({race_info['venue']} {race_info['race_number']}R "
              f"{race_info['surface']}{race_info['distance']}m)")
        print(f"{'─'*60}")
        print(f"{'馬番':>4} {'馬名':<12} {'勝率':>7} {'複勝率':>7} {'SI':>6}")
        print(f"{'─'*60}")

        predictions = []
        for _, row in pred_df.iterrows():
            # 馬名取得
            horse_name = ""
            for r in results:
                if r["horse_number"] == row["horse_number"]:
                    horse_name = r["horse_name"] or ""
                    break

            print(f"{int(row['horse_number']):>4} {horse_name:<12} "
                  f"{row['pred_win_norm']:>6.1%} {row['pred_top3_norm']/3:>6.1%} "
                  f"{row.get('si_avg', 0):>6.1f}")

            # 推奨馬券生成用データ
            odds_win = 0
            odds_place = 0
            n_horses = len(pred_df)
            for r in results:
                if r["horse_number"] == row["horse_number"]:
                    odds_win = r["odds"] or 0
                    break

            # FIX: resultsテーブルにオッズがない場合、APIから直接取得
            if odds_win <= 0 and not _api_odds_cache.get(race_id):
                try:
                    from refresh_odds import fetch_odds_from_api
                    api_odds = fetch_odds_from_api(race_id)
                    if api_odds:
                        _api_odds_cache[race_id] = api_odds
                        # resultsテーブルも更新
                        with get_db() as conn2:
                            for hn, od in api_odds.items():
                                conn2.execute(
                                    "UPDATE results SET odds = ?, popularity = ? WHERE race_id = ? AND horse_number = ?",
                                    (od["odds_win"], od["popularity"], race_id, hn))
                        print(f"  📡 APIからオッズ取得: {len(api_odds)}頭")
                except Exception as e:
                    print(f"  ⚠️ APIオッズ取得失敗: {e}")

            # APIキャッシュからオッズを取得
            if odds_win <= 0 and race_id in _api_odds_cache:
                api_data = _api_odds_cache[race_id].get(int(row["horse_number"]))
                if api_data:
                    odds_win = api_data["odds_win"]

            # C7 fix: 複勝オッズの推定を改善（頭数・人気帯に応じた係数）
            if odds_win:
                if odds_win <= 3.0:
                    place_ratio = 0.28 + (n_horses - 10) * 0.005
                elif odds_win <= 10.0:
                    place_ratio = 0.33 + (n_horses - 10) * 0.005
                else:
                    place_ratio = 0.40 + (n_horses - 10) * 0.008
                place_ratio = max(0.20, min(0.55, place_ratio))
                odds_place = max(round(odds_win * place_ratio, 1), 1.1)
            else:
                odds_place = 1.5

            # オッズがない場合（未来レース）→ 予測確率から推定
            has_real_odds = odds_win > 0
            if odds_win <= 0 and row["pred_win_norm"] > 0:
                # 推定オッズ = 0.8 / 予測勝率（JRAの控除率20%を考慮）
                odds_win = max(round(0.8 / row["pred_win_norm"], 1), 1.2)
                odds_place = max(round(0.8 / (row["pred_top3_norm"] / 3), 1), 1.1)

            predictions.append({
                "horse_number": int(row["horse_number"]),
                "horse_name": horse_name,
                "pred_win": row["pred_win_norm"],
                "pred_top3": row["pred_top3_norm"] / 3,
                "rank_score": round(float(row.get("rank_score", 0)), 3),
                "odds_win": odds_win,
                "odds_place": odds_place,
                "_has_real_odds": has_real_odds,
                "si_avg": round(row.get("si_avg", 0), 1),
                "jockey_name": next((r["jockey_name"] for r in results if r["horse_number"] == int(row["horse_number"]) and "jockey_name" in r.keys()), ""),
                # カテゴリスコア
                "cat_ability": round(row.get("si_avg", 0), 1),
                "cat_pedigree": round(row.get("pedigree_score", 0), 2),
                "cat_jockey": round(row.get("jt_score", 0), 2),
                "cat_track": round(row.get("bias_score", 0), 2),
                "cat_record": round(row.get("top3_rate_10r", 0) * 100, 1),
                "cat_weather": round(row.get("horse_wet_top3_rate", 0) * 100, 1),
                "win_rate": round(row.get("win_rate_10r", 0) * 100, 1),
                "top3_rate": round(row.get("top3_rate_10r", 0) * 100, 1),
                # 能力モデル (オッズ非依存) 勝率 — 記事の AI独自評価 / ◎2軸化に使用 (#72)
                "ability_score": round(float(row.get("_ability_score", 0) or 0), 4),
            })

        # ── 穴予兆スコア (anasanee_score) と波乱度 (race_volatility) を計算 ──
        # 過去 11265R 統計を元に、前走凡走 + 距離変更 + 脚質 + 血統 等から
        # 各馬の「穴を出しやすさ」をスコア化 (○▲△ 選定で活用)。
        try:
            from volatility import (
                fetch_horse_history, compute_anasanee_score,
                compute_race_volatility, compute_jockey_trust,
            )
            hns = [p["horse_number"] for p in predictions]
            ana_feats = fetch_horse_history(race_id, hns)
            for p in predictions:
                feat = ana_feats.get(p["horse_number"], {}) or {}
                # 2026-05-27: jockey_name=None で NoneType crash → 二重ガード
                jk_name = p.get("jockey_name") or ""
                ana = compute_anasanee_score(feat, jk_name)
                p["anasanee_score"] = ana["score"]
                p["anasanee_reasons"] = ana["reasons"]
                p["jockey_trust"] = compute_jockey_trust(
                    jk_name, p.get("popularity", 0) or 0
                )
            race_volatility = compute_race_volatility(race_info)
        except Exception as e:
            print(f"  ⚠️ 穴予兆計算スキップ: {e}")
            for p in predictions:
                p["anasanee_score"] = 0
                p["anasanee_reasons"] = []
                p["jockey_trust"] = 0
            race_volatility = {"score": 0, "factors": [], "conf_adjust": 0}

        # predictions_cache に保存
        # ソート: 勝率だけだと同率で内枠順になるため、
        # rank_score(モデルの順位スコア)とSI指数を加味した複合スコアで順位付け
        def _sort_key(p):
            win = p["pred_win"]  # 0-1
            rank = p.get("rank_score", 0)  # モデルの順位スコア
            si = max(p.get("si_avg", 0), 0) / 100  # 0-1に正規化 (SI80→0.8)
            # 複合スコア: 勝率50% + rank_score25% + SI25%
            return win * 0.50 + rank * 0.25 + si * 0.25
        sorted_preds = sorted(predictions, key=_sort_key, reverse=True)
        # 人気順: 実オッズがある場合のみ使用（推定オッズは循環参照になるため除外）
        has_real_odds = any(p.get("_has_real_odds") for p in sorted_preds)

        # FIX: APIキャッシュにpopularityがある場合はそれを使用
        if race_id in _api_odds_cache:
            api_pop = _api_odds_cache[race_id]
            popularity_map = {}
            for p in sorted_preds:
                hn = p["horse_number"]
                if hn in api_pop and api_pop[hn].get("popularity", 0) > 0:
                    popularity_map[hn] = api_pop[hn]["popularity"]
                else:
                    popularity_map[hn] = 0
            has_real_odds = True  # APIデータがある = 実オッズあり
        elif has_real_odds:
            by_odds = sorted(sorted_preds, key=lambda x: x.get("odds_win", 999))
            popularity_map = {p["horse_number"]: i+1 for i, p in enumerate(by_odds)}
        else:
            popularity_map = {p["horse_number"]: 0 for p in sorted_preds}

        # NEW: Contrarian 補正 — 「市場が見落としている過小評価馬」を加点
        # 「ほぼ全レース1人気が本命」を避け、AI が見抜いた人気外妙味を◎候補に。
        # 4-18人気で AI 予測勝率 > 市場想定確率(1/pop*0.6) のとき、その差に応じて加点。
        # popularity_map が全頭 0 (未確定) の場合はスキップ (現状ソートのまま)。
        # シミュレーション (5/9-10 各72R) では ◎1-3人気率 74%→65% / 4-9人気率 26%→35% 程度。
        #
        # v10 (2026-05-24): ML 勝率が均一なレース(3歳G1等の学習データ不足)では
        # Contrarian が過剰発火し、SI 最低クラスの最低人気馬が ◎ になる事故が発生
        # (5/24 オークス ◎18人気ミツカネベネラ事件)。
        # ML 勝率のレンジ(max - min)が小さい場合は Contrarian を弱める。
        ml_range = max(p["pred_win"] for p in sorted_preds) - min(p["pred_win"] for p in sorted_preds)
        if ml_range < 0.03:
            # ML が全く識別できていない → Contrarian を完全停止し SI と前哨戦実績を優先
            CONTRARIAN_STRENGTH = 0.0
        elif ml_range < 0.05:
            # 弱めの識別 → Contrarian を30%に弱める
            CONTRARIAN_STRENGTH = 0.3
        else:
            CONTRARIAN_STRENGTH = 1.0
        if any(popularity_map.values()):
            def _final_sort_key(p):
                win = p["pred_win"]
                rank = p.get("rank_score", 0)
                si = max(p.get("si_avg", 0), 0) / 100
                base = win * 0.50 + rank * 0.25 + si * 0.25
                pop = popularity_map.get(p["horse_number"], 0) or 0
                if 4 <= pop <= 18:
                    market_implied = 1.0 / pop * 0.6
                    if win > market_implied:
                        base += (win - market_implied) * (pop - 3) * 0.10 * CONTRARIAN_STRENGTH
                return base
            sorted_preds = sorted(sorted_preds, key=_final_sort_key, reverse=True)

        # 「データ強度」スコア (◎以外の印付けと「注」で共用)
        # AI 勝率 / SI 指数 / 血統 / 騎手 / 直近実績 の合計 (0-13点)
        def _data_strength(p):
            s = 0
            win_pct = (p.get('pred_win', 0) or 0) * 100
            if win_pct >= 7.0: s += 3
            elif win_pct >= 5.0: s += 2
            elif win_pct >= 4.0: s += 1
            si = p.get('si_avg', 0) or 0
            if si >= 90: s += 3
            elif si >= 80: s += 2
            elif si >= 70: s += 1
            ped = p.get('cat_pedigree', 0) or 0
            if ped >= 60: s += 2
            elif ped >= 50: s += 1
            jk = p.get('cat_jockey', 0) or 0
            if jk >= 65: s += 2
            elif jk >= 55: s += 1
            tr = p.get('top3_rate', 0) or 0
            if tr >= 50: s += 2
            elif tr >= 30: s += 1
            rec = p.get('cat_record', 0) or 0
            if rec >= 50: s += 1
            return s

        # 印付け v3 (2026-05-17): 「◎は人気でも OK、相手だけ穴志向」モード
        # 旧版は ML 上位5頭をそのまま ◎○▲△× にしていた → 全部人気順になる弊害。
        # 新版は ○▲△ を「ML 上位2-7位の中で人気外+データ強度を重視」して選ぶ。
        # × は残り、◎ は従来通り ML 1位。
        # ユーザー要望「◎は人気でも構わない、相手だけ穴志向」を反映。

        # 全頭の mark を一旦リセット
        for p in sorted_preds:
            p['mark'] = ''

        # ── ◎ 選定 (#72 2軸化、USE_ABILITY_CORE で切替) ──
        # 既定 (OFF): 従来通り ML 1位を ◎。
        # ON: 能力モデル (オッズ非依存) 1位を ◎。2020-2025 backtest で単勝ROI
        #     112% (ML◎ 92% / +20pt、全6年100%超、単日支配クリア) を実証。
        #     ※full bet (三連複◎軸) ROI の検証 + walk-forward 確認後に既定ONへ。
        # 併せて妙味/危険フラグを「データ」として付与 (印は変えず、記事/UI 用、bet 非影響)。
        ml_top = sorted_preds[0] if sorted_preds else None
        abil_top = max(sorted_preds, key=lambda p: p.get('ability_score', 0) or 0) if sorted_preds else None
        mkt_fav = min(sorted_preds, key=lambda p: (p.get('odds_win') or 1e9)) if sorted_preds else None

        # 妙味/危険フラグ (additive data) — 能力 vs 市場の乖離
        for p in sorted_preds:
            a = p.get('ability_score', 0) or 0        # 能力勝率 (レース内正規化)
            od = p.get('odds_win') or 0
            m = (1.0 / od) if od > 0 else 0           # 市場の暗黙勝率 (控除前)
            p['_ability_vs_market'] = round(a / m, 2) if m > 0 else 0
        if abil_top and mkt_fav:
            # 妙味馬 = 能力1位かつ市場本命でない (能力≠市場、backtest 135% 層)
            abil_top['is_value_pick'] = (abil_top is not mkt_fav)
            # 危険な人気馬 = 市場本命だが能力順位が低い (A/M < 0.6 目安)
            mkt_fav['is_danger_fav'] = ((mkt_fav.get('_ability_vs_market') or 0) < 0.6)

        use_ability_core = os.environ.get('USE_ABILITY_CORE') == '1'
        core = abil_top if (use_ability_core and abil_top) else ml_top
        if core:
            core['mark'] = '◎'

        # 相手候補プール (◎ を除く ML 上位 6頭)
        relay_pool = [p for p in sorted_preds if p is not core][:6]

        def _relay_score(p):
            """相手選定スコア: ML 評価 + データ強度 + 人気外ボーナス + 穴予兆。"""
            pred = (p.get('pred_win', 0) or 0) * 80  # base ML 評価
            si = (p.get('si_avg', 0) or 0) / 4
            data = _data_strength(p) * 3
            pop = p.get('popularity', 0) or 0
            # 4-12人気を妙味とみなしてボーナス (1-3人気は加点なし)
            pop_bonus = max(min(pop - 3, 9), 0) * 1.5
            # 穴予兆スコア (前走凡走 + 距離変更 + 脚質 + 血統 等) を 2倍重み
            ana = (p.get('anasanee_score', 0) or 0) * 2.0
            return pred + si + data + pop_bonus + ana

        # 相手候補をスコア順に並べ替え → ○▲△ に割当
        relay_sorted = sorted(relay_pool, key=_relay_score, reverse=True)
        for i, mk in enumerate(['○', '▲', '△']):
            if i < len(relay_sorted):
                relay_sorted[i]['mark'] = mk

        # ─── × 注 = 「穴馬スコア」 (2026-05-24 改修) ─────────────────────
        # 過去 3-5月 7000R バックテスト結果に基づく改修:
        # 旧: × = 相手候補残り中の AI 勝率最高 / 注 = データ強度最大馬
        #   → × 単独 ROI 50% / 注 単独 ROI 69% で「機能不全」(ノイズ印)
        # 新: × 注 = 穴馬スコア (人気-実力乖離 + コース適性 + 血統 + 穴予兆 + 騎手) 順
        #   → × 単独 ROI 95% / 注 単独 ROI 136% に大幅改善
        #   → ◎軸 三連複ROI 130% → 174% に +44pt 改善
        # 入力データ: si_avg / cat_track / cat_pedigree / cat_jockey / anasanee_score
        # 人気フィルター: 5人気以上 (上位人気は ◎○▲△ で拾うべき)
        def _ana_score(p, pop):
            """穴馬スコア — 人気-実力乖離 + コース適性 + 穴予兆 + 騎手"""
            if pop < 5:  # 上位人気は ◎○▲△ で拾う
                return 0
            si = p.get('si_avg', 0) or 0
            cat_track = p.get('cat_track', 0) or 0
            cat_pedigree = p.get('cat_pedigree', 0) or 0
            cat_jockey = p.get('cat_jockey', 0) or 0
            ana = p.get('anasanee_score', 0) or 0
            diversion = si / max(pop, 1)  # SI ÷ 人気 (実力過小評価度)
            course_fit = (cat_track + cat_pedigree) / 2  # コース適性
            return diversion * 1.0 + course_fit * 0.5 + ana * 3.0 + cat_jockey * 0.3

        # ◎○▲△ 以外を対象に穴馬スコアでランク
        unmarked = [p for p in sorted_preds if not p.get('mark')]
        ana_scored = []
        for p in unmarked:
            pop = popularity_map.get(p['horse_number'], 0) or 0
            score = _ana_score(p, pop)
            if score > 0:  # スコア 0 (人気上位 or データ不足) は除外
                ana_scored.append((score, p))
        ana_scored.sort(key=lambda x: -x[0])
        # 注 = 穴馬スコア1位(注目すべき穴馬)、× = 2位(押さえ)
        # 競馬慣習: 注 = 注目、× = 押さえ
        for i, mk in enumerate(['注', '×']):
            if i < len(ana_scored):
                ana_scored[i][1]['mark'] = mk

        # 推奨理由を生成 (UI-2 fix)
        for i, p in enumerate(sorted_preds):
            pw = p["pred_win"] * 100
            pt = p["pred_top3"] * 100
            si = p.get("si_avg", 0)
            wr = p.get("win_rate", 0)
            tr = p.get("top3_rate", 0)
            reasons = []
            if i == 0:
                if pw >= 40: reasons.append("圧倒的な勝率で本命筆頭")
                elif pw >= 25: reasons.append("勝率トップで信頼度が高い")
                else: reasons.append("僅差ながら勝率1位")
            if wr >= 30: reasons.append(f"直近勝率{wr}%と絶好調")
            elif wr >= 15: reasons.append(f"直近勝率{wr}%で実績あり")
            if tr >= 50: reasons.append(f"直近複勝率{tr}%で安定感抜群")
            elif tr >= 30: reasons.append(f"直近複勝率{tr}%で堅実")
            if si >= 90: reasons.append(f"SI{si}は出走馬中トップクラス")
            elif si >= 80: reasons.append(f"SI{si}で能力上位")
            if pt >= 25: reasons.append("複勝率が非常に高く堅実")
            if pw >= 20 and si >= 80: reasons.append("勝率とSIの両面で好材料")
            if not reasons:
                if pw > 0: reasons.append(f"AI勝率{pw:.1f}%")
                else: reasons.append("出走歴なし or 特徴量不足")
            p["reasons"] = reasons[:3]

        cache_json = json.dumps([{
            "horse_number": p["horse_number"],
            "horse_name": p["horse_name"],
            "mark": p.get("mark", ""),
            "pred_win_pct": round(p["pred_win"] * 100, 1),
            "pred_top3_pct": round(p["pred_top3"] * 100, 1),
            "rank_score": p.get("rank_score", 0),
            "odds_win": round(p.get("odds_win", 0), 1),
            "popularity": popularity_map.get(p["horse_number"], 0),
            "si_avg": p.get("si_avg", 0),
            "jockey_name": p.get("jockey_name", ""),
            "cat_ability": p.get("cat_ability", 0),
            "cat_pedigree": p.get("cat_pedigree", 0),
            "cat_jockey": p.get("cat_jockey", 0),
            "cat_track": p.get("cat_track", 0),
            "cat_record": p.get("cat_record", 0),
            "cat_weather": p.get("cat_weather", 0),
            "win_rate": p.get("win_rate", 0),
            "top3_rate": p.get("top3_rate", 0),
            "anasanee_score": p.get("anasanee_score", 0),
            "anasanee_reasons": p.get("anasanee_reasons", []),
            "reasons": p.get("reasons", []),
        } for p in sorted_preds], ensure_ascii=False)

        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO predictions_cache
                (race_id, predictions_json, all_bets_json, confidence, should_bet, created_at)
                VALUES (?, ?, '{}', 'C', 0, datetime('now'))
            """, (race_id, cache_json))

        # 信頼度: confidence.py(v2 6軸合成、単一のSource of Truth)に委譲
        # v13 (2026-05-26): confidence を bet 判定の前に確定 (旧 path は confidence 知らずに
        # should_bet 判定 → C/D も bet=1 のまま、というバグの根本修正)
        from confidence import evaluate as eval_confidence
        n_horses = len(sorted_preds) if sorted_preds else 1
        top1 = sorted_preds[0] if sorted_preds else {}
        top2 = sorted_preds[1] if len(sorted_preds) >= 2 else top1
        top_win = top1.get("pred_win", 0) * 100
        top2_win = top2.get("pred_win", 0) * 100
        # ◎の複勝率 (pred_top3 がなければ pred_win*2.2 で推定)
        top_top3 = top1.get("pred_top3", 0) * 100 if top1.get("pred_top3") is not None else None
        top3_sum = sum(p.get("pred_win", 0) * 100 for p in sorted_preds[:3])
        top_pop = top1.get("popularity")
        try:
            top_pop = int(top_pop) if top_pop else None
            if top_pop is not None and top_pop <= 0:
                top_pop = None
        except (TypeError, ValueError):
            top_pop = None
        c = eval_confidence(
            top_win_pct=top_win,
            n_horses=n_horses,
            top3_sum_pct=top3_sum,
            grade=race_info.get('grade'),
            second_win_pct=top2_win,
            top_top3_pct=top_top3,
            top_popularity=top_pop,
            top_odds=top1.get('odds_win', 0) or 0,  # v4: ROI 期待値計算用
        )
        confidence = c['confidence']
        conf_reason = c['reason']

        # ── race_volatility 補正 (2026-05-30: confidence からは外し should_bet 専用に) ──
        # 旧設計では未勝利-2 / 1勝×loss-1 等の「投資ROI由来の降格」を confidence に
        # 適用していたが、エンタメ用 confidence = ◎勝率 と矛盾する (◎35%の未勝利本命が
        # ROI 理由で C/D に落ち「自信度A表示なのにグレードC」となる)。
        # confidence は予測自信度 (◎勝率) のまま保持し、未勝利/1勝loss/16頭等の
        # 「投資を避けるべき要因」は should_bet 側で判定する (下の should_bet_race)。
        # race_volatility は should_bet 用に保持。reason には参考として併記のみ。
        if race_volatility and race_volatility.get("factors"):
            factors = ",".join(race_volatility.get("factors", []))
            if factors:
                conf_reason = f"{conf_reason} / 波乱要因({factors})※should_bet判定に反映"

        # ── jockey_trust 補正: ◎が信頼/不信騎手の上位人気のとき微調整 ──
        top_jt = top1.get("jockey_trust", 0) if top1 else 0
        if top_jt >= 2 and confidence in ("A", "B", "C"):
            grades = ["S", "A", "B", "C", "D"]
            confidence = grades[max(0, grades.index(confidence) - 1)]
            conf_reason = f"{conf_reason} / 信頼騎手{top_jt:+d}"
        elif top_jt <= -2 and confidence in ("S", "A"):
            grades = ["S", "A", "B", "C", "D"]
            confidence = grades[min(len(grades) - 1, grades.index(confidence) + 1)]
            conf_reason = f"{conf_reason} / 不信騎手{top_jt:+d}"

        # 🆕 v13 (2026-05-26): confidence-aware should_bet 判定 (旧 v12 の二重評価を一本化)
        # confidence が確定した時点で 1 回だけ should_bet 判定する。
        # C/D は backtest ROI 75-80% の損失層なので明示的に should_bet=0。
        # 🆕 v14 (2026-05-27 #30): race_info を渡して 1勝×2.5-3.9倍 帯遮断
        # 🆕 v15 (Phase α): USE_WHITELIST=1 で specialization mode (walk-forward 確認の 2 segments のみ)
        use_whitelist = os.environ.get("USE_WHITELIST", "") == "1"
        should_bet, reason = strategy.should_bet_race(
            predictions, confidence=confidence, race_info=race_info,
            strict_whitelist=use_whitelist
        )

        # 買い目生成 (常に実行 — EV・妙味計算のため。should_bet=0 でも UI は買い目表示)
        # v3 (2026-05-27): confidence-aware bet weighting (S=1.5x, A=1.2x, B=1.0x)
        # Phase α: whitelist mode で specialization
        bets_result = strategy.generate_bets(
            predictions, confidence=confidence,
            race_info=race_info, strict_whitelist=use_whitelist
        )
        bets_by_type = {}
        for b in bets_result.get('bets', []):
            bt = b.get('type', '単勝')
            if bt not in bets_by_type:
                bets_by_type[bt] = []
            bets_by_type[bt].append(b)

        # v11 (2026-05-24): 見送りレースでも買い目を出す (UI 側で推奨/見送りバッジ判別)
        all_bets_json = json.dumps(bets_by_type, ensure_ascii=False)
        if should_bet:
            print(strategy.format_recommendation(bets_result, race_info))
        else:
            print(f"\n⏭️ 見送り推奨だが買い目は提示: {reason}")

        # キャッシュ更新（買い目・confidence・conf_reason・should_bet・bet_reason）
        with get_db() as conn:
            conn.execute("""
                UPDATE predictions_cache
                SET all_bets_json = ?, confidence = ?, conf_reason = ?,
                    should_bet = ?, bet_reason = ?
                WHERE race_id = ?
            """, (all_bets_json, confidence, conf_reason,
                  1 if should_bet else 0, reason if not should_bet else "OK",
                  race_id))
        print(f"  💾 予測キャッシュを保存 (信頼度: {confidence}, 理由: {conf_reason})")
        _predicted_count += 1

    # ─── 完了サマリー + sanity check ───
    print("\n" + "="*60)
    print(f"📊 予測完了: 成功 {_predicted_count}/{len(race_ids)} レース")
    if _skipped_races:
        print(f"⚠️  スキップ {len(_skipped_races)} レース:")
        for rid, reason in _skipped_races:
            print(f"    {rid}: {reason}")

        # メインレース(11R)が含まれていたら異常終了
        main_skipped = [(rid, r) for rid, r in _skipped_races if rid[10:12] == '11']
        if main_skipped:
            print(f"\n❌ メインレース(11R)を {len(main_skipped)} 件スキップ → 異常状態")
            for rid, r in main_skipped:
                print(f"    {rid}: {r}")
            # CI環境では exit 1 でワークフロー失敗扱いに
            if os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS'):
                print("🚨 CI環境のため exit 1(workflow失敗扱い)")
                sys.exit(1)
    print("="*60)


def cmd_backtest(args):
    """バックテストコマンド（fast_train.pyに委譲）"""
    import subprocess
    script = os.path.join(os.path.dirname(__file__), "fast_train.py")
    print("📊 高速バックテストを起動...")
    print("   (fast_train.py に委譲)\n")
    result = subprocess.run(
        [sys.executable, script],
        cwd=os.path.dirname(__file__)
    )
    sys.exit(result.returncode)


def cmd_status(args):
    """DB状況確認コマンド"""
    with get_db() as conn:
        races = conn.execute("SELECT COUNT(*) as c FROM races").fetchone()["c"]
        results = conn.execute("SELECT COUNT(*) as c FROM results").fetchone()["c"]
        horses = conn.execute("SELECT COUNT(*) as c FROM horses").fetchone()["c"]
        jockeys = conn.execute("SELECT COUNT(*) as c FROM jockeys").fetchone()["c"]

        if races > 0:
            date_range = conn.execute(
                "SELECT MIN(race_date) as min_d, MAX(race_date) as max_d FROM races"
            ).fetchone()
            print(f"📅 期間: {date_range['min_d']} 〜 {date_range['max_d']}")

        bets = conn.execute("SELECT COUNT(*) as c FROM bets").fetchone()["c"]
        if bets > 0:
            bet_stats = conn.execute("""
                SELECT SUM(amount) as total_bet,
                       SUM(payout) as total_payout,
                       SUM(is_hit) as hits
                FROM bets
            """).fetchone()
            print(f"\n💰 馬券実績:")
            print(f"  購入数: {bets}件")
            print(f"  的中数: {bet_stats['hits']}件")
            print(f"  投資額: ¥{bet_stats['total_bet']:,}")
            print(f"  回収額: ¥{bet_stats['total_payout']:,}")

    print(f"\n📊 データベース状況:")
    print(f"  レース数:   {races:,}")
    print(f"  出走データ: {results:,}")
    print(f"  馬:         {horses:,}")
    print(f"  騎手:       {jockeys:,}")

    # モデル状態
    model_path = os.path.join(os.path.dirname(__file__), "models", "model_top3.pkl")
    rank_path = os.path.join(os.path.dirname(__file__), "models", "model_rank.pkl")
    if os.path.exists(model_path):
        has_rank = "✅" if os.path.exists(rank_path) else "⚠️ なし"
        print(f"\n🧠 モデル: ✅ 学習済み (LambdaRank: {has_rank})")
    else:
        print(f"\n🧠 モデル: ⚠️ 未学習 (python predict.py train で学習してください)")


def main():
    parser = argparse.ArgumentParser(
        description="🏇 競馬予想AI — プラス収支を目指す統合予測システム",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # ステップ1: データ収集 (2024年のデータ)
  python predict.py collect --start-year 2024 --end-month 12

  # ステップ2: モデルを学習
  python predict.py train

  # ステップ3: 予測
  python predict.py predict --date 20250315

  # バックテスト
  python predict.py backtest --year 2024

  # DB状況確認
  python predict.py status
        """
    )

    subparsers = parser.add_subparsers(dest="command")

    # collect
    p_collect = subparsers.add_parser("collect", help="レースデータを収集")
    p_collect.add_argument("--date", help="日付 (YYYYMMDD)")
    p_collect.add_argument("--start-year", type=int)
    p_collect.add_argument("--start-month", type=int, default=1)
    p_collect.add_argument("--end-year", type=int)
    p_collect.add_argument("--end-month", type=int, default=12)

    # train
    p_train = subparsers.add_parser("train", help="予測モデルを学習")

    # predict
    p_predict = subparsers.add_parser("predict", help="レースを予測")
    p_predict.add_argument("--race-id", help="レースID")
    p_predict.add_argument("--date", help="日付 (YYYYMMDD)")
    p_predict.add_argument("--force", action="store_true", help="10時以降でも強制再予測")
    p_predict.add_argument("--allow-zero-odds", action="store_true",
                           help="odds=0 でも予測する (#42 odds充足ゲートを無効化、履歴backtest用)")

    # backtest
    p_backtest = subparsers.add_parser("backtest", help="バックテスト実行")
    p_backtest.add_argument("--year", type=int)
    p_backtest.add_argument("--month", type=int)

    # status
    p_status = subparsers.add_parser("status", help="DB状況確認")

    args = parser.parse_args()

    init_db()

    if args.command == "collect":
        cmd_collect(args)
    elif args.command == "train":
        cmd_train(args)
    elif args.command == "predict":
        cmd_predict(args)
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
