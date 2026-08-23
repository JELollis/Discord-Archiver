"""Capture a Discord channel and render it as safe MediaWiki wikitext.

Scope (v1, "core" fidelity): author, timestamp, text content (mentions/channels/
roles/custom-emoji resolved to readable text), attachments, reply references, and
thread messages (active + archived, rendered as per-thread sections). Reactions,
edit history and embeds are intentionally out of scope for v1.

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
    "<": "&lt;",
    ">": "&gt;",
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


def resolve_mentions(text: str, guild, *, members=None, channels=None, roles=None) -> str:
    """Replace Discord <@id>/<#id>/<@&id>/<:emoji:id> tokens with readable text.

    Runs BEFORE escaping; the resolved names are escaped by the caller.
    """
    if not text:
        return ""

    def user_repl(m):
        member = (members or {}).get(int(m.group(1)))
        if member is None and guild:
            member = guild.get_member(int(m.group(1)))
        return f"@{member.display_name}" if member else "@unknown-user"

    def channel_repl(m):
        chan = (channels or {}).get(int(m.group(1)))
        if chan is None and guild:
            chan = guild.get_channel(int(m.group(1)))
        return f"#{chan.name}" if chan else "#unknown-channel"

    def role_repl(m):
        role = (roles or {}).get(int(m.group(1)))
        if role is None and guild:
            role = guild.get_role(int(m.group(1)))
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
    if len(name) <= 200:
        return name
    stem, dot, extension = name.rpartition(".")
    if not dot or not extension:
        return name[:200]
    suffix = f".{extension}"
    return f"{stem[:200 - len(suffix)]}{suffix}"


_CHANNEL_PATTERN = re.compile(r"^([a-z]+)-(\d+)-(spring|summer|fall)-(\d{4})$", re.IGNORECASE)


def make_page_title(channel_name: str, namespace: str = "Archive") -> str:
    """Map a Discord channel name to an archive page title.

    ``cpt-257-summer-2023`` -> ``Archive:CPT-257/Summer 2023``. Channels that do
    not match the DEPT-NUM-TERM-YEAR pattern land under ``Archive:Misc/<name>``.
    """
    m = _CHANNEL_PATTERN.match(channel_name.strip())
    if m:
        dept, num, term, year = m.groups()
        return f"{namespace}:{dept.upper()}-{num}/{term.capitalize()} {year}"
    return f"{namespace}:Misc/{sanitize_title_part(channel_name)}"


def attachment_upload_name(channel_name: str, message_id: int, attachment_id: int, filename: str) -> str:
    """Deterministic, collision-resistant upload filename for an attachment."""
    return sanitize_filename(f"{channel_name}-{message_id}-{attachment_id}-{filename}")


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


def render_page(
    channel_name: str,
    messages: list,
    meta: dict,
    *,
    index: bool = False,
    anchor_index: dict | None = None,
    self_title: str | None = None,
) -> str:
    """Render the full archive page wikitext for a channel.

    ``messages`` is an ordered list of dicts (see Archive_Bot.capture_channel);
    each attachment dict may carry ``wiki_filename`` (set after upload) or
    ``error``. ``meta`` supplies header fields (guild, channel_id, captured_at,
    message_count, source).

    ``index`` marks a split archive's index page (suppresses the empty-channel
    notice). ``anchor_index`` maps a message id to the page title that holds it,
    and ``self_title`` is this page's title, so cross-part reply links point at
    the correct part page instead of a dead same-page anchor.
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

    if not messages and not index:
        lines.append("''No messages were found in this channel.''")

    for msg in messages:
        if msg.get("thread_header"):
            lines.append("")
            lines.append(f"=== Thread: {escape_wikitext(msg['thread_header'])} ===")
            lines.append("")
            continue
        anchor = f'<span id="msg-{msg["id"]}"></span>'
        author = escape_wikitext(msg.get("author", "unknown"))
        username = msg.get("author_username")
        author_id = msg.get("author_id")
        identity = f" (@{escape_wikitext(username)})" if username and username != msg.get("author") else ""
        if author_id:
            identity += f" [Discord ID: {author_id}]"
        ts = _format_timestamp(msg.get("created"))
        edited = " · edited" if msg.get("edited") else ""
        reply = ""
        if msg.get("reply_to"):
            rid = msg["reply_to"]
            target_title = (anchor_index or {}).get(rid)
            if target_title and self_title and target_title != self_title:
                link = f"[[{target_title}#msg-{rid}|earlier message]]"
            else:
                link = f"[[#msg-{rid}|earlier message]]"
            reply = f" · &#8618; in reply to {link}"
        header = f"{anchor}\n; {author}{identity} <small>({ts}{edited}){reply}</small>"
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
    lines.append("[[Category:Discord archive]]")
    if meta.get("category"):
        lines.append(f"[[Category:{escape_wikitext(meta['category'])}]]")
    if meta.get("complete", True):
        lines.append(
            f"<!-- source_channel_id={meta.get('channel_id')} captured_at={meta.get('captured_at')} "
            f"captured_until={meta.get('captured_until')} -->"
        )
    else:
        lines.append("<!-- INCOMPLETE: attachment or capture errors; deletion is blocked. -->")
    return "\n".join(lines)


def split_messages(messages: list, channel_name: str, meta: dict, max_bytes: int = 1_800_000) -> list[list]:
    """Partition messages so each rendered page stays below MediaWiki's byte limit.

    MediaWiki's article-size limit ($wgMaxArticleSize, default 2 MiB) counts UTF-8
    bytes, so measure the encoded byte length (multibyte emoji/names count for more
    than one character).
    """
    chunks = []
    current = []
    for message in messages:
        candidate = current + [message]
        rendered = render_page(channel_name, candidate, {**meta, "complete": False})
        if current and len(rendered.encode("utf-8")) > max_bytes:
            chunks.append(current)
            current = [message]
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current)
    return chunks
