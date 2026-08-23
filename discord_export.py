"""Capture a Discord channel and render it as safe MediaWiki wikitext.

Scope (v1, "core" fidelity): author, timestamp, text content (mentions/channels/
roles/custom-emoji resolved to readable text), attachments, and reply references.
Reactions, edit history, embeds and thread expansion are intentionally out of
scope for v1.

Safety: all user-authored text is neutralised so it cannot inject wiki markup
(templates, links, tables, headings, list markers). Bare URLs are left intact so
MediaWiki still auto-links them. Per-message HTML anchors (`<span id=msg-...>`)
let replies and external links target an exact message.
"""

from __future__ import annotations

import re
from datetime import timezone

# Discord entity references embedded in message content.
_RE_USER = re.compile(r"<@!?(\d+)>")
_RE_CHANNEL = re.compile(r"<#(\d+)>")
_RE_ROLE = re.compile(r"<@&(\d+)>")
_RE_EMOJI = re.compile(r"<a?:(\w+):\d+>")

# Characters that carry wiki meaning inline; rendered literally via HTML entities.
_WIKI_ESCAPE = {
    "[": "&#91;", "]": "&#93;",
    "{": "&#123;", "}": "&#125;",
    "|": "&#124;",
}
# Characters that start structural wikitext when they lead a line.
_LINE_LEAD = set("*#:;=! ")


def escape_wikitext(text: str) -> str:
    """Neutralise wiki markup in user text while keeping it readable.

    Encodes bracket/brace/pipe characters, escapes runs of apostrophes (bold/
    italic) and line-leading structural characters. Bare URLs are untouched so
    they still auto-link.
    """
    if not text:
        return ""
    text = "".join(_WIKI_ESCAPE.get(ch, ch) for ch in text)
    text = re.sub(r"'{2,}", lambda m: "&#39;" * len(m.group()), text)
    out = []
    for line in text.split("\n"):
        if line[:1] in _LINE_LEAD:
            line = f"&#{ord(line[0])};{line[1:]}"
        out.append(line)
    return "\n".join(out)


def resolve_mentions(text: str, guild) -> str:
    """Replace Discord <@id>/<#id>/<@&id>/<:emoji:id> tokens with readable text.

    Runs BEFORE escaping; the resolved names are escaped by the caller.
    """
    if not text:
        return ""

    def user_repl(m):
        member = guild.get_member(int(m.group(1))) if guild else None
        return f"@{member.display_name}" if member else "@unknown-user"

    def channel_repl(m):
        chan = guild.get_channel(int(m.group(1))) if guild else None
        return f"#{chan.name}" if chan else "#unknown-channel"

    def role_repl(m):
        role = guild.get_role(int(m.group(1))) if guild else None
        return f"@{role.name}" if role else "@unknown-role"

    text = _RE_USER.sub(user_repl, text)
    text = _RE_CHANNEL.sub(channel_repl, text)
    text = _RE_ROLE.sub(role_repl, text)
    text = _RE_EMOJI.sub(lambda m: f":{m.group(1)}:", text)
    return text


# Characters MediaWiki forbids in titles / that break File names.
_TITLE_BAD = re.compile(r"[#<>\[\]|{}/:]+")


def sanitize_title_part(text: str) -> str:
    """Make a string safe as a MediaWiki title component."""
    text = _TITLE_BAD.sub("-", text).strip().strip("-")
    return re.sub(r"\s+", "_", text) or "unnamed"


def sanitize_filename(name: str) -> str:
    """Make an upload filename safe (keep extension; strip title-hostile chars)."""
    name = name.replace(" ", "_")
    name = re.sub(r"[#<>\[\]|{}:/]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-._") or "file"
    return name[:200]


_CHANNEL_PATTERN = re.compile(r"^([a-z]+)-(\d+)-(spring|summer|fall)-(\d{4})$", re.IGNORECASE)


def make_page_title(channel_name: str, namespace: str = "Archive") -> str:
    """Map a Discord channel name to an archive page title.

    ``cpt-257-summer-2023`` -> ``Archive:2023/Summer/CPT-257``. Channels that do
    not match the DEPT-NUM-TERM-YEAR pattern land under ``Archive:Misc/<name>``.
    """
    m = _CHANNEL_PATTERN.match(channel_name.strip())
    if m:
        dept, num, term, year = m.groups()
        return f"{namespace}:{year}/{term.capitalize()}/{dept.upper()}-{num}"
    return f"{namespace}:Misc/{sanitize_title_part(channel_name)}"


def attachment_upload_name(channel_name: str, message_id: int, filename: str) -> str:
    """Deterministic, collision-resistant upload filename for an attachment."""
    return sanitize_filename(f"{channel_name}-{message_id}-{filename}")


_IMAGE_EXT = {"png", "gif", "jpg", "jpeg", "webp"}


def _render_attachment(att: dict) -> str:
    """Render one attachment as wikitext, using its uploaded wiki filename."""
    wiki_name = att.get("wiki_filename")
    original = escape_wikitext(att.get("filename", "file"))
    if not wiki_name:
        return f"&#128206; {original} ''(attachment not archived: {escape_wikitext(att.get('error', 'upload skipped'))})''"
    ext = wiki_name.rsplit(".", 1)[-1].lower() if "." in wiki_name else ""
    if ext in _IMAGE_EXT:
        return f"[[File:{wiki_name}|thumb|none|400px|{original}]]"
    return f"&#128206; [[Media:{wiki_name}|{original}]]"


def _format_timestamp(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def render_page(channel_name: str, messages: list, meta: dict) -> str:
    """Render the full archive page wikitext for a channel.

    ``messages`` is an ordered list of dicts (see Archive_Bot.capture_channel);
    each attachment dict may carry ``wiki_filename`` (set after upload) or
    ``error``. ``meta`` supplies header fields (guild, channel_id, captured_at,
    message_count, source).
    """
    lines = [
        "{{stub}}" if False else "",
        f"''Automated archive of Discord channel'' '''#{escape_wikitext(channel_name)}''' "
        f"''from the GTC Tech Student server.''",
        "",
        "{| class=\"wikitable\"",
        f"! Channel !! Messages !! Captured (UTC)",
        "|-",
        f"| #{escape_wikitext(channel_name)} || {meta.get('message_count', len(messages))} "
        f"|| {escape_wikitext(meta.get('captured_at', ''))}",
        "|}",
        "",
        "----",
        "",
    ]

    if not messages:
        lines.append("''No messages were found in this channel.''")

    for msg in messages:
        anchor = f'<span id="msg-{msg["id"]}"></span>'
        author = escape_wikitext(msg.get("author", "unknown"))
        ts = _format_timestamp(msg.get("created"))
        edited = " · edited" if msg.get("edited") else ""
        reply = ""
        if msg.get("reply_to"):
            reply = f" · &#8618; in reply to [[#msg-{msg['reply_to']}|earlier message]]"
        header = f"{anchor}\n; {author} <small>({ts}{edited}){reply}</small>"
        lines.append(header)

        content = msg.get("content") or ""
        if content:
            rendered = escape_wikitext(content).replace("\n", "<br>\n: ")
            lines.append(f": {rendered}")

        for att in msg.get("attachments", []):
            lines.append(f": {_render_attachment(att)}")

        if not content and not msg.get("attachments"):
            lines.append(": ''(no text content)''")
        lines.append("")

    lines.append("")
    lines.append(
        f"[[Category:Discord archive]] "
        f"<!-- source_channel_id={meta.get('channel_id')} captured_at={meta.get('captured_at')} -->"
    )
    return "\n".join(lines)
