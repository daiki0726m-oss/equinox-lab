#!/usr/bin/env python3
"""記事 markdown → note 貼り付け用 HTML に変換 (ブラウザで読める/note にコピペ用)。

既存の articles/*_note.html と同じスタイル (max-width 680px / Hiragino / 緑アクセント)。
markdown lib/pandoc に依存しない最小パーサ (#/##/###/- list/---/**bold**/段落)。

使い方: python3 scripts/md_to_note_html.py articles/<name>.md [出力.html]
省略時は同名 .html を同じディレクトリに出力。出力パスを stdout に印字。
"""
import sys
import os
import re
import html

STYLE = (
    'body{max-width:680px;margin:2rem auto;padding:0 1rem;'
    'font-family:"Hiragino Sans",sans-serif;line-height:1.9;color:#222}'
    'h1{font-size:1.5rem;border-bottom:2px solid #222;padding-bottom:.4rem}'
    'h2{font-size:1.2rem;margin-top:2rem;border-left:4px solid #2a6;padding-left:.5rem}'
    'h3{font-size:1.05rem}li{margin:.15rem 0}hr{border:none;border-top:1px dashed #aaa;margin:1.6rem 0}'
    'strong{color:#1a5}'
    'blockquote{background:#eef7f0;border-left:4px solid #2a6;margin:1rem 0;'
    'padding:.7rem 1rem;border-radius:0 6px 6px 0;color:#1c4a30}'
    '.notice{background:#fff6d8;border:1px solid #e6c200;padding:.6rem .8rem;'
    'border-radius:6px;font-size:.85rem}'
)


def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return s


def md_to_body(md: str) -> str:
    out, in_ul, in_bq = [], False, False

    def close_blocks():
        nonlocal in_ul, in_bq
        if in_ul:
            out.append("</ul>"); in_ul = False
        if in_bq:
            out.append("</blockquote>"); in_bq = False

    for raw in md.split("\n"):
        st = raw.rstrip()
        if st.startswith("### "):
            close_blocks(); out.append(f"<h3>{_inline(st[4:])}</h3>")
        elif st.startswith("## "):
            close_blocks(); out.append(f"<h2>{_inline(st[3:])}</h2>")
        elif st.startswith("# "):
            close_blocks(); out.append(f"<h1>{_inline(st[2:])}</h1>")
        elif st.strip() == "---":
            close_blocks(); out.append("<hr>")
        elif st.startswith("> "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            if not in_bq:
                out.append("<blockquote>"); in_bq = True
            out.append(f"<p>{_inline(st[2:])}</p>")
        elif st.startswith("- "):
            if in_bq:
                out.append("</blockquote>"); in_bq = False
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{_inline(st[2:])}</li>")
        elif st.strip() == "":
            close_blocks()
        else:
            close_blocks(); out.append(f"<p>{_inline(st)}</p>")
    close_blocks()
    return "\n".join(out)


def main():
    if len(sys.argv) < 2:
        print("usage: md_to_note_html.py <article.md> [out.html]")
        sys.exit(1)
    md_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(md_path)[0] + ".html"
    md = open(md_path, encoding="utf-8").read()
    # タイトル = 最初の # 見出し
    m = re.search(r"^#\s+(.+)$", md, re.M)
    title = (m.group(1) if m else os.path.basename(md_path)).strip()
    body = md_to_body(md)
    doc = (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{STYLE}</style></head><body>\n"
        '<div class="notice">📋 <strong>⌘A → ⌘C</strong> で全選択コピー → note に貼り付け '
        "(この行は貼り付け後に削除)。</div>\n"
        f"{body}\n</body></html>\n"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    print(out_path)


if __name__ == "__main__":
    main()
