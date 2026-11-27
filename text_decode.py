"""HTTP レスポンスを正しい文字コードで文字列にする (#181)。

netkeiba は 2026 年に EUC-JP から UTF-8 へ順次移行しており、ページごとに移行時期が違う
(出馬表は 2026-06 頃 #85、血統ページ /horse/ped/ は 2026-11 中旬)。呼び出し側で
``resp.encoding = "euc-jp"`` と決め打ちしていると、移行した瞬間から全部の値が文字化けし、
しかも run は success のまま値だけが壊れる (#85 / #169 / #181)。

規則:
  1. サーバが Content-Type で宣言した charset を最優先で、厳密に (errors="strict") 読む。
  2. 宣言が無い・宣言どおりに読めない時は UTF-8 → EUC-JP の順に厳密に試す。
  3. どれでも読めない時だけ、宣言 (無ければ UTF-8) で置換文字入りの文字列を返す。
     その場合は呼び出し側が ``has_mojibake`` で検出して捨てる。
"""

import re

MOJIBAKE = "�"  # 読めなかったバイトの置換文字 (U+FFFD)

_CHARSET_RE = re.compile(r"charset=([\w-]+)", re.I)


def declared_charset(resp):
    """Content-Type ヘッダの charset (無ければ None)。"""
    m = _CHARSET_RE.search(resp.headers.get("Content-Type", "") or "")
    return m.group(1) if m else None


def decode_html(resp, fallbacks=("utf-8", "euc-jp")):
    """``requests.Response`` を文字列にする。上の規則を参照。"""
    raw = resp.content or b""
    declared = declared_charset(resp)
    tried = []
    for enc in ([declared] if declared else []) + list(fallbacks):
        key = enc.lower().replace("_", "-")
        if key in tried:
            continue
        tried.append(key)
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode(declared or "utf-8", errors="replace")


def has_mojibake(*values):
    """どれか1つでも置換文字を含めば True (文字化けした値を保存しないための判定)。"""
    return any(MOJIBAKE in str(v) for v in values if v)
