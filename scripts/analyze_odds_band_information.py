#!/usr/bin/env python3
"""オッズ帯別に「モデルは市場に情報を足しているか」を測る。

ユーザーの問い:「的中率狙って人気馬買っても配当少ないから儲からない」= 正しい。
検証したいのは「モデルの学習が人気馬に偏っていて、配当の大きい層では market に
何も足していないのでは?」という仮説。もし中穴〜穴の帯で model が market に情報を
足しているなら、損失関数をオッズで重み付けして「儲かる側」に学習容量を寄せる余地がある。
評価は #109 規律で 2025-03 以降 (真のOOS) のみ。
"""
import os, sys, time
sys.path.insert(0,'/Users/daikimorimoto/Desktop/keiba'); os.chdir('/Users/daikimorimoto/Desktop/keiba')
import numpy as np, pandas as pd, lightgbm as lgb
from sklearn.metrics import roc_auc_score
from fast_train import (load_all_data, build_horse_history, build_jockey_trainer_stats,
                        build_speed_index_cache, build_pedigree_cross_stats,
                        compute_features_fast, get_feature_columns)
from database import init_db
init_db()
races_df, results_df, _ = load_all_data()
info = races_df.set_index("race_id")[["race_date","venue","distance","surface","track_condition","horse_count","race_name","grade"]].to_dict("index")
for col in info[list(info)[0]].keys():
    results_df[col] = results_df["race_id"].map(lambda rid, c=col: info.get(rid, {}).get(c, ""))
hh = build_horse_history(results_df, races_df)
js, ts, cs = build_jockey_trainer_stats(results_df, races_df)
si = build_speed_index_cache(results_df, races_df)
ped = build_pedigree_cross_stats(results_df, races_df)
sc, ss, dc, ds = ped[0], ped[1], ped[2], ped[3]
grouped = {rid: g for rid, g in results_df.groupby("race_id")}
rows=[]; t0=time.time()
for n, race in enumerate(races_df.itertuples(index=False)):
    rr = grouped.get(race.race_id)
    if rr is None or rr.empty: continue
    rows.extend(compute_features_fast(race._asdict(), rr.to_dict("records"), hh, js, ts, cs, si,
                                      sire_course=sc, sire_surface=ss, damsire_course=dc, damsire_surface=ds))
    if (n+1)%6000==0: print(f"  ...{n+1} ({time.time()-t0:.0f}s)", flush=True)
df = pd.DataFrame(rows)
df["race_date"] = df["race_id"].map(lambda r: info.get(r,{}).get("race_date",""))
df = df[(df.finish_position>0) & (df.odds>0)].sort_values("race_date")
CUT="2025-03-01"
tr, te = df[df.race_date<CUT], df[df.race_date>=CUT]
feat = get_feature_columns()
y_tr=(tr.finish_position<=3).astype(int); y_te=(te.finish_position<=3).astype(int)
m = lgb.LGBMClassifier(n_estimators=600, learning_rate=0.05, num_leaves=63,
                       feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=5,
                       random_state=42, verbose=-1)
m.fit(tr[feat].fillna(0), y_tr)
te = te.copy(); te["p_model"] = m.predict_proba(te[feat].fillna(0))[:,1]
# 市場の暗黙複勝率: 単勝オッズから複勝の近似 (1/odds を レース内で正規化 × 3)
inv = 1.0/te.odds
te["p_mkt"] = te.groupby("race_id")["odds"].transform(lambda s: (1/s)/(1/s).sum()) * 3
te["p_mkt"] = te.p_mkt.clip(0.01, 0.99)
te["y"] = y_te.values
print(f"\n評価 {len(te):,}頭 ({CUT}以降)")
print(f"{'オッズ帯':<12}{'n':>8}{'実複勝率':>9}{'市場AUC':>9}{'モデルAUC':>10}{'モデル上乗せ':>12}{'単勝回収':>9}")
bands=[(1,3),(3,6),(6,10),(10,20),(20,50),(50,10000)]
for lo,hi in bands:
    sub=te[(te.odds>=lo)&(te.odds<hi)]
    if len(sub)<500: continue
    try:
        a_m=roc_auc_score(sub.y, sub.p_mkt); a_p=roc_auc_score(sub.y, sub.p_model)
    except Exception: continue
    # 帯内で「モデルが市場より高く評価した馬」の単勝回収
    sub=sub.copy(); sub["edge"]=sub.p_model/sub.p_mkt
    top=sub[sub.edge>=sub.edge.quantile(0.8)]
    ret=np.where(top.finish_position==1, top.odds*100, 0).mean()
    print(f"{lo}-{hi if hi<10000 else '∞'}倍{'':<6}{len(sub):>8,}{100*sub.y.mean():>8.1f}%{a_m:>9.3f}{a_p:>10.3f}{a_p-a_m:>+12.3f}{ret:>8.0f}円")
print("\n※ モデル上乗せ = モデルAUC - 市場AUC。正なら『その帯で市場に情報を足している』")
print("※ 単勝回収 = その帯でモデルが市場より高評価した上位20%を単勝100円で買った場合")
print("BAND_DONE")
