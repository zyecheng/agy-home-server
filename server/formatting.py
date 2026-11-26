# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Lightweight Markdown -> Telegram HTML converter (stdlib-only, safe fallback)."""

from __future__ import annotations

import html
import re

_FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\r\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\r\n]+)\]\((https?://[^\s)\"<>]+|tg://[^\s)\"<>]+)\)")
_BOLD_STAR_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_BOLD_UNDER_RE = re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?=\S)([^*\r\n]+?)(?<=\S)\*(?!\*)")
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*$")
_BULLET_RE = re.compile(r"^([ \t]*)[-*][ \t]+(.*)$")


def markdown_to_telegram_html(text: str) -> str:
    """Convert common LLM Markdown into Telegram-safe HTML.

    Protects fenced code blocks and inline code spans before escaping HTML
    entities, and restricts links to http/https/tg schemes.
    """
    if not text:
        return ""

    placeholders: list[str] = []

    def _store(rendered: str) -> str:
        idx = len(placeholders)
        placeholders.append(rendered)
        return f"\x00TGPH{idx}\x00"

    # 1. Extract fenced code blocks first so inner syntax is untouched
    def _repl_fence(m: re.Match[str]) -> str:
        lang = m.group(1).strip()
        code = html.escape(m.group(2).rstrip("\r\n"), quote=False)
        if lang:
            safe_lang = html.escape(lang, quote=True)
            return _store(f'<pre><code class="language-{safe_lang}">{code}</code></pre>')
        return _store(f"<pre>{code}</pre>")

    work = _FENCE_RE.sub(_repl_fence, text)

    # 2. Extract inline code spans
    def _repl_inline(m: re.Match[str]) -> str:
        code = html.escape(m.group(1), quote=False)
        return _store(f"<code>{code}</code>")

    work = _INLINE_CODE_RE.sub(_repl_inline, work)

    # 3. Extract safe links before HTML-escaping URLs/labels
    def _repl_link(m: re.Match[str]) -> str:
        label = html.escape(m.group(1), quote=False)
        url = html.escape(m.group(2), quote=True)
        return _store(f'<a href="{url}">{label}</a>')

    work = _LINK_RE.sub(_repl_link, work)

    # 4. Escape HTML entities on remaining prose
    work = html.escape(work, quote=False)

    # 5. Block-level line transforms (headings, blockquotes, bullets)
    out_lines: list[str] = []
    quote_buf: list[str] = []

    def _flush_quote() -> None:
        if quote_buf:
            out_lines.append("<blockquote>" + "\n".join(quote_buf) + "</blockquote>")
            quote_buf.clear()

    for line in work.splitlines():
        if line.startswith("&gt; ") or line == "&gt;":
            quote_buf.append(line[5:] if line.startswith("&gt; ") else "")
            continue
        _flush_quote()

        m_head = _HEADING_RE.match(line)
        if m_head:
            out_lines.append(f"<b>{m_head.group(1)}</b>")
            continue

        m_bul = _BULLET_RE.match(line)
        if m_bul:
            out_lines.append(f"{m_bul.group(1)}• {m_bul.group(2)}")
            continue

        out_lines.append(line)

    _flush_quote()
    work = "\n".join(out_lines)

    # 6. Inline emphasis transforms
    work = _BOLD_STAR_RE.sub(r"<b>\1</b>", work)
    work = _BOLD_UNDER_RE.sub(r"<b>\1</b>", work)
    work = _STRIKE_RE.sub(r"<s>\1</s>", work)
    work = _ITALIC_STAR_RE.sub(r"<i>\1</i>", work)

    # 7. Restore code & link placeholders
    for idx, rendered in enumerate(placeholders):
        work = work.replace(f"\x00TGPH{idx}\x00", rendered)

    return work
