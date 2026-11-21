// 買い目の表記をまとめる (#176)。ダッシュボード (index.html / predict.html) 共通。
//
// ユーザー要望「2-4 / 2-5 なら 2-4,5 のように、まとめられるものはまとめて」。
// 1点ずつ並べていた買い目を、競馬の一般的なフォーメーション表記にまとめる。
//   馬連・ワイド  : 2-4,5          (= 2-4, 2-5)        / 2,4,5 BOX
//   三連複        : 2-9-1,3,4,5    (= 2頭軸ながし)      / 2-4,5,9（軸1頭ながし） / 2,4,5,9 BOX
//   馬単・三連単  : 2→4,5          / 2,4→2,4,5→2,4,5,9 (着順の列。同じ馬は1点の中で重ならない)
//   単勝・複勝    : 2,5
// まとめた表記は「買っている組み合わせとぴったり同じ集合」になる時だけ使う (余分な組み合わせを
// 含む表記にはしない)。ぴったりにならない部分は、さらに小さいまとまりか1点ずつで出す。
// 金額が違う点は同じまとまりに入れない (表示は「各¥100 × 6点」)。
(function (global) {
  'use strict';
  const ORDERED = new Set(['馬単', '三連単']);
  const SINGLE = new Set(['単勝', '複勝']);

  const asc = (a, b) => a - b;
  const uniq = a => [...new Set(a)].sort(asc);
  const join = a => a.join(',');

  function normCombo(bt, hns) {
    const a = (hns || []).map(Number);
    return ORDERED.has(bt) ? a : [...a].sort(asc);
  }
  const keyOf = c => c.join('-');

  // フォーメーション (列ごとの馬の集合) が表す組み合わせの集合 (キー文字列)
  function expand(cols, ordered) {
    const out = new Set();
    const rec = (i, acc) => {
      if (i === cols.length) {
        out.add(keyOf(ordered ? acc : [...acc].sort(asc)));
        return;
      }
      for (const h of cols[i]) {
        if (acc.includes(h)) continue;
        rec(i + 1, acc.concat([h]));
      }
    };
    rec(0, []);
    return out;
  }
  const sameSet = (a, b) => a.size === b.size && [...a].every(x => b.has(x));

  function allPairsKeys(S) {
    const s = new Set();
    for (let i = 0; i < S.length; i++) for (let j = i + 1; j < S.length; j++) s.add(keyOf([S[i], S[j]]));
    return s;
  }
  function allTriplesKeys(S) {
    const s = new Set();
    for (let i = 0; i < S.length; i++) for (let j = i + 1; j < S.length; j++)
      for (let k = j + 1; k < S.length; k++) s.add(keyOf([S[i], S[j], S[k]]));
    return s;
  }
  function mostFrequent(combos) {
    const cnt = new Map();
    combos.forEach(c => new Set(c).forEach(h => cnt.set(h, (cnt.get(h) || 0) + 1)));
    let best = null, bc = -1;
    [...cnt.keys()].sort(asc).forEach(h => { if (cnt.get(h) > bc) { best = h; bc = cnt.get(h); } });
    return best;
  }

  // 馬連・ワイド・枠連 (順不同の2頭)
  function blocksPairs(combos) {
    let rest = combos.slice();
    const blocks = [];
    while (rest.length) {
      const keys = new Set(rest.map(keyOf));
      const S = uniq(rest.flat());
      if (S.length >= 3 && sameSet(keys, allPairsKeys(S))) {
        blocks.push({ text: `${join(S)} BOX`, keys });
        break;
      }
      const h = mostFrequent(rest);
      const mine = rest.filter(c => c.includes(h));
      const others = uniq(mine.map(c => (c[0] === h ? c[1] : c[0])));
      blocks.push({ text: `${h}-${join(others)}`, keys: new Set(mine.map(keyOf)) });
      rest = rest.filter(c => !c.includes(h));
    }
    return blocks;
  }

  // 三連複 (順不同の3頭)
  function blocksTrio(combos) {
    let rest = combos.slice();
    const blocks = [];
    while (rest.length) {
      const keys = new Set(rest.map(keyOf));
      const S = uniq(rest.flat());
      if (S.length >= 4 && sameSet(keys, allTriplesKeys(S))) {
        blocks.push({ text: `${join(S)} BOX`, keys });
        break;
      }
      const h = mostFrequent(rest);
      const mine = rest.filter(c => c.includes(h));
      const pairs = mine.map(c => c.filter(x => x !== h));
      const S2 = uniq(pairs.flat());
      const pk = new Set(pairs.map(keyOf));
      if (S2.length >= 3 && sameSet(pk, allPairsKeys(S2))) {
        // 軸1頭ながし
        blocks.push({ text: `${h}-${join(S2)}（軸1頭ながし）`, keys: new Set(mine.map(keyOf)) });
        rest = rest.filter(c => !c.includes(h));
        continue;
      }
      // 2頭軸ながし (h と g を含むものを1つのまとまりに)
      const g = mostFrequent(pairs);
      const sub = mine.filter(c => c.includes(g));
      const T = uniq(sub.map(c => c.find(x => x !== h && x !== g)));
      const [a, b] = [h, g].sort(asc);
      blocks.push({ text: `${a}-${b}-${join(T)}`, keys: new Set(sub.map(keyOf)) });
      const done = new Set(sub.map(keyOf));
      rest = rest.filter(c => !done.has(keyOf(c)));
    }
    return blocks;
  }

  // 馬単・三連単 (着順あり)。まず全体を1つのフォーメーションで、だめなら1着ごと、さらに1-2着ごと
  function blocksOrdered(combos, n) {
    const tryForm = (cs) => {
      const cols = [];
      for (let i = 0; i < n; i++) cols.push(uniq(cs.map(c => c[i])));
      const want = new Set(cs.map(keyOf));
      return sameSet(expand(cols, true), want)
        ? { text: cols.map(join).join('→'), keys: want } : null;
    };
    const whole = tryForm(combos);
    if (whole) return [whole];
    const blocks = [];
    uniq(combos.map(c => c[0])).forEach(a => {
      const sub = combos.filter(c => c[0] === a);
      const f = tryForm(sub);
      if (f) { blocks.push(f); return; }
      if (n === 3) {
        uniq(sub.map(c => c[1])).forEach(b => {
          const sub2 = sub.filter(c => c[1] === b);
          blocks.push({ text: `${a}→${b}→${join(uniq(sub2.map(c => c[2])))}`, keys: new Set(sub2.map(keyOf)) });
        });
      } else {
        sub.forEach(c => blocks.push({ text: keyOf(c).replace(/-/g, '→'), keys: new Set([keyOf(c)]) }));
      }
    });
    return blocks;
  }

  // 組み合わせのリスト → まとめた表記のリスト [{text, keys}]
  function compress(bt, combos) {
    const cs = combos.map(c => normCombo(bt, c));
    if (!cs.length) return [];
    if (SINGLE.has(bt) || cs[0].length === 1) {
      return [{ text: join(uniq(cs.flat())), keys: new Set(cs.map(keyOf)) }];
    }
    if (ORDERED.has(bt)) return blocksOrdered(cs, cs[0].length);
    if (cs[0].length === 2) return blocksPairs(cs);
    if (cs[0].length === 3) return blocksTrio(cs);
    return cs.map(c => ({ text: keyOf(c), keys: new Set([keyOf(c)]) }));
  }

  // 買い目の行 → 金額・主力の別にまとめたブロック [{text, points, amount, honor, lines}]
  // 行の順 (最初に出てきた順) を保つ。horse_numbers が無い行は detail をそのまま1点で出す。
  function groupBetLines(bets, bt) {
    const groups = [];
    const byKey = new Map();
    (bets || []).forEach(b => {
      const k = `${b.honor ? 1 : 0}|${b.amount}`;
      if (!byKey.has(k)) { byKey.set(k, { honor: !!b.honor, amount: b.amount, lines: [] }); groups.push(byKey.get(k)); }
      byKey.get(k).lines.push(b);
    });
    const out = [];
    groups.forEach(g => {
      const withNums = g.lines.filter(b => Array.isArray(b.horse_numbers) && b.horse_numbers.length);
      const noNums = g.lines.filter(b => !(Array.isArray(b.horse_numbers) && b.horse_numbers.length));
      const lineOf = new Map(withNums.map(b => [keyOf(normCombo(bt, b.horse_numbers)), b]));
      compress(bt, withNums.map(b => b.horse_numbers)).forEach(bl => {
        const lines = [...bl.keys].map(k => lineOf.get(k)).filter(Boolean);
        out.push({ text: bl.text, points: lines.length, amount: g.amount, honor: g.honor, lines });
      });
      noNums.forEach(b => out.push({ text: b.detail || b.bet_detail || '', points: 1, amount: g.amount, honor: g.honor, lines: [b] }));
    });
    return out;
  }

  const api = { compress, groupBetLines, expand };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  global.BetNotation = api;
})(typeof window !== 'undefined' ? window : globalThis);
