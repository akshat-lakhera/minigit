"""minigit.mdrender -- owner: M8 web. A small, safe Markdown renderer for README files.

Supports the common subset: ATX headings, paragraphs, bullet and numbered lists (nested by indent),
fenced code blocks, block quotes, horizontal rules, simple pipe tables, inline code, bold, italic and
links. Only ``http://`` and ``https://`` link targets become links; images become their alt text.
Raw HTML is never passed through: every piece of input text is HTML-escaped. Library code: no I/O.
"""
from __future__ import annotations

import html
import re

MAX_DEPTH = 8          # nesting of quotes and lists, and of inline emphasis

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_HR_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_QUOTE_RE = re.compile(r"^ {0,3}> ?(.*)$")
_ITEM_RE = re.compile(r"^( {0,3})([-*+]|\d{1,9}[.)])(?:[ \t]+(.*))?$")
_TABLE_SEP_RE = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
_URL_RE = re.compile(r"^https?://[^\s<>\"'`]+$", re.IGNORECASE)
_INLINE_RE = re.compile(
    r"(?P<code>(?P<ticks>`+)(?P<ctext>.+?)(?P=ticks))"
    r"|(?P<link>(?P<bang>!?)\[(?P<label>[^\[\]\n]+)\]\((?P<url>[^()\s]+)(?:[ \t]+\"[^\"\n]*\")?\))"
    r"|(?P<auto><(?P<aurl>https?://[^\s<>]+)>)"
    r"|(?P<strong>\*\*(?=\S)(?P<s1>.+?)(?<=\S)\*\*|__(?=\S)(?P<s2>.+?)(?<=\S)__)"
    r"|(?P<em>\*(?=[^\s*])(?P<e1>.+?)(?<=[^\s*])\*|(?<![A-Za-z0-9_])_(?=[^\s_])(?P<e2>.+?)(?<=[^\s_])_(?![A-Za-z0-9_]))",
    re.DOTALL,
)


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def render_inline(text: str, depth: int = 0) -> str:
    if depth > MAX_DEPTH:
        return esc(text)
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        out.append(esc(text[pos:m.start()]))
        pos = m.end()
        if m.group("code"):
            out.append("<code>" + esc(m.group("ctext").strip(" ") or m.group("ctext")) + "</code>")
        elif m.group("link"):
            label, url = m.group("label"), m.group("url")
            if m.group("bang"):
                out.append(esc(label))          # images are never loaded: show the alt text
            elif _URL_RE.match(url):
                out.append(f'<a href="{esc(url)}" rel="nofollow noopener noreferrer">'
                           f"{render_inline(label, depth + 1)}</a>")
            else:
                out.append(render_inline(label, depth + 1))
        elif m.group("auto"):
            url = m.group("aurl")
            out.append(f'<a href="{esc(url)}" rel="nofollow noopener noreferrer">{esc(url)}</a>')
        elif m.group("strong"):
            out.append("<strong>" + render_inline(m.group("s1") or m.group("s2"), depth + 1) + "</strong>")
        else:
            out.append("<em>" + render_inline(m.group("e1") or m.group("e2"), depth + 1) + "</em>")
    out.append(esc(text[pos:]))
    return "".join(out)


def _is_block_start(line: str) -> bool:
    return bool(_FENCE_RE.match(line) or _HEADING_RE.match(line) or _HR_RE.match(line)
                or _QUOTE_RE.match(line) or _ITEM_RE.match(line))


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", row)]


def _dedent(line: str, n: int) -> str:
    i = 0
    while i < n and i < len(line) and line[i] == " ":
        i += 1
    return line[i:]


def _render_blocks(lines: list[str], depth: int, tight: bool = False) -> str:
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush() -> None:
        if para:
            text = render_inline("\n".join(s.strip() for s in para), 0)
            out.append(text if tight else f"<p>{text}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i].replace("\t", "    ")
        if not line.strip():
            flush()
            i += 1
            continue
        fence = _FENCE_RE.match(line)
        if fence and not (fence.group(1)[0] == "`" and "`" in fence.group(2)):
            flush()
            mark = fence.group(1)
            body: list[str] = []
            i += 1
            while i < len(lines) and not re.match(r"^ {0,3}" + re.escape(mark[0]) + "{" + str(len(mark)) + r",}[ \t]*$",
                                                  lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # the closing fence (or the end of the input)
            out.append("<pre><code>" + esc("\n".join(body)) + "</code></pre>")
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            out.append(f"<h{level}>{render_inline(heading.group(2) or '')}</h{level}>")
            i += 1
            continue
        if _HR_RE.match(line) and not para:
            out.append("<hr>")
            i += 1
            continue
        if _QUOTE_RE.match(line):
            flush()
            quoted: list[str] = []
            while i < len(lines) and lines[i].strip():
                q = _QUOTE_RE.match(lines[i])
                quoted.append(q.group(1) if q else lines[i])
                i += 1
            inner = _render_blocks(quoted, depth + 1) if depth < MAX_DEPTH else esc("\n".join(quoted))
            out.append(f"<blockquote>{inner}</blockquote>")
            continue
        item = _ITEM_RE.match(line)
        if item and (not para or item.group(3)):
            flush()
            ordered = item.group(2)[0].isdigit()
            start = int(item.group(2)[:-1]) if ordered else 1
            items: list[list[str]] = []
            loose = False
            while i < len(lines):
                cur = lines[i].replace("\t", "    ")
                m = _ITEM_RE.match(cur)
                if m and m.group(2)[0].isdigit() == ordered:
                    indent = len(m.group(1)) + len(m.group(2)) + 1
                    items.append([(m.group(3) or "")])
                    i += 1
                    while i < len(lines):
                        nxt = lines[i].replace("\t", "    ")
                        if not nxt.strip():
                            if i + 1 < len(lines) and lines[i + 1].startswith("  ") and lines[i + 1].strip():
                                items[-1].append("")
                                loose = True
                                i += 1
                                continue
                            break
                        if nxt.startswith("  "):
                            items[-1].append(_dedent(nxt, indent))
                            i += 1
                        elif not _is_block_start(nxt):
                            items[-1].append(nxt)       # a lazy continuation line
                            i += 1
                        else:
                            break
                    after = _ITEM_RE.match(lines[i + 1]) if i + 1 < len(lines) else None
                    if (i < len(lines) and not lines[i].strip() and after
                            and after.group(2)[0].isdigit() == ordered):
                        loose = True
                        i += 1
                    continue
                break
            tag = "ol" if ordered else "ul"
            attr = f' start="{start}"' if ordered and start != 1 else ""
            parts = []
            for body in items:
                inner = (_render_blocks(body, depth + 1, tight=not loose) if depth < MAX_DEPTH
                         else esc("\n".join(body)))
                parts.append(f"<li>{inner}</li>")
            out.append(f"<{tag}{attr}>" + "".join(parts) + f"</{tag}>")
            continue
        if ("|" in line and not para and i + 1 < len(lines) and "|" in lines[i + 1]
                and "-" in lines[i + 1] and _TABLE_SEP_RE.match(lines[i + 1])):
            head = _cells(line)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(_cells(lines[i]))
                i += 1
            thead = "".join(f"<th>{render_inline(c)}</th>" for c in head)
            tbody = "".join("<tr>" + "".join(f"<td>{render_inline(c)}</td>" for c in r[:len(head)])
                            + "</tr>" for r in rows)
            out.append(f'<div class="md-table"><table><thead><tr>{thead}</tr></thead>'
                       f"<tbody>{tbody}</tbody></table></div>")
            continue
        if para and _is_block_start(line) and not _ITEM_RE.match(line):
            flush()
            continue
        para.append(line)
        i += 1
    flush()
    return "".join(out)


def render(text: str) -> str:
    """Markdown source -> an HTML fragment. Safe for any input: nothing is passed through unescaped."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\0", "�")
    return _render_blocks(text.split("\n"), 0)
