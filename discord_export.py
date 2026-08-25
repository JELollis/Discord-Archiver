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
import hashlib
import urllib.parse
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
_BEHAVIOR_SWITCH = re.compile(r"__[A-Z][A-Z0-9_]*__", re.IGNORECASE)
_LEGACY_INCOMPLETE_MARKER = (
    "<!-- INCOMPLETE: attachment or capture errors; deletion is blocked. -->"
)


def escape_wikitext(text: str) -> str:
    """Neutralise wiki markup in user text while keeping it readable.

    Encodes bracket/brace/pipe characters, escapes runs of apostrophes (bold/
    italic), runs of tildes (MediaWiki signature substitution) and line-leading
    structural characters. Bare URLs are untouched so they still auto-link.
    """
    if not text:
        return ""
    text = "".join(_WIKI_ESCAPE.get(ch, ch) for ch in text)
    text = re.sub(r"'{2,}", lambda m: "&#39;" * len(m.group()), text)
    # MediaWiki behavior switches such as __NOTOC__ and __NOINDEX__ affect the
    # entire page even inside indented text. Encode their underscores while
    # preserving the visible message text.
    text = _BEHAVIOR_SWITCH.sub(
        lambda m: m.group().replace("_", "&#95;"), text
    )
    # MediaWiki substitutes runs of 3-5 tildes (~~~/~~~~/~~~~~) for the editing
    # user's signature and/or timestamp during a save; encode them so archived
    # message text is preserved verbatim instead of being rewritten.
    text = re.sub(r"~{3,}", lambda m: "&#126;" * len(m.group()), text)
    out = []
    for line in text.split("\n"):
        if line[:1] in _LINE_LEAD:
            line = f"&#{ord(line[0])};{line[1:]}"
        out.append(line)
    return "\n".join(out)


def is_adoptable_legacy_incomplete_page(
    page: dict,
    channel_name: str,
    bot_username: str,
    *,
    part_number: int | None = None,
) -> bool:
    """Return whether an old marker-less bot page is safe to migrate.

    Early archiver versions wrote an ``INCOMPLETE`` page without a stable
    channel ownership marker. Adoption is deliberately much stricter than the
    normal ownership check: only a single-revision page with the exact legacy
    marker, generated header, bot author, and edit summary is eligible.
    """
    if page.get("revision_count") != 1 or page.get("user") != bot_username:
        return False
    content = page.get("content", "")
    if not isinstance(content, str):
        return False
    if "<!-- archive_owner_channel_id=" in content or "<!-- source_channel_id=" in content:
        return False
    if content.count(_LEGACY_INCOMPLETE_MARKER) != 1:
        return False
    expected_header = (
        "''Automated archive of Discord channel'' "
        f"'''#{escape_wikitext(channel_name)}''' "
        "''from the GTC Tech Student server.''"
    )
    if content.count(expected_header) != 1:
        return False

    comment = page.get("comment", "")
    if part_number is not None:
        return comment == f"Archive part {part_number} for #{channel_name}"
    return bool(re.fullmatch(
        rf"Archive #{re.escape(channel_name)} \(\d+ messages\)",
        str(comment),
    ))


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
_PART_COUNT_RE = re.compile(r"<!-- archive_part_count=(\d+) -->")
_PART_MARKER_RE = re.compile(r"<!-- archive_part=(\d+) sha256=([0-9a-f]{64}) -->")
_ATTACHMENT_COUNT_RE = re.compile(r"<!-- archive_attachment_count=(\d+) -->")
_ATTACHMENT_MARKER_RE = re.compile(
    r"<!-- archive_attachment filename=([^ \n]+) sha1=([0-9a-f]{40}) size=(\d+) -->"
)
_THREAD_COUNT_RE = re.compile(r"<!-- source_thread_count=(\d+) -->")
_THREAD_MARKER_RE = re.compile(
    r"<!-- source_thread_id=(\d+) name_sha256=([0-9a-f]{64}) -->"
)
# NAS-hosted ("external") attachments too large for the wiki: linked, not uploaded.
_EXTERNAL_COUNT_RE = re.compile(r"<!-- archive_external_count=(\d+) -->")
_EXTERNAL_MARKER_RE = re.compile(
    r"<!-- archive_external filename=([^ \n]+) sha1=([0-9a-f]{40}) size=(\d+) -->"
)
_CANONICAL_MARKER_RE = re.compile(
    r"<!-- archive_canonical_sha256=([0-9a-f]{64}) -->"
)


def _truncate_bytes(text: str, max_bytes: int) -> str:
    """Truncate to at most max_bytes of UTF-8 without splitting a codepoint."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


def content_sha256(text: str) -> str:
    """Return the stable UTF-8 SHA-256 used by split archive manifests."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_part_manifest(manifest: list[tuple[int, str]]) -> str:
    """Render the canonical page's expected split-part content hashes."""
    lines = [f"<!-- archive_part_count={len(manifest)} -->"]
    lines.extend(
        f"<!-- archive_part={part_number} sha256={digest} -->"
        for part_number, digest in manifest
    )
    return "\n".join(lines)


def parse_part_manifest(content: str) -> list[tuple[int, str]] | None:
    """Parse and validate one complete, contiguous generated part manifest."""
    count_matches = _PART_COUNT_RE.findall(content)
    if len(count_matches) != 1:
        return None
    expected_count = int(count_matches[0])
    entries = [(int(number), digest) for number, digest in _PART_MARKER_RE.findall(content)]
    if len(entries) != expected_count:
        return None
    entries.sort()
    if [number for number, _digest in entries] != list(range(1, expected_count + 1)):
        return None
    return entries


def render_attachment_manifest(
    attachments: list[tuple[str, str, int]],
) -> str:
    """Render current wiki filenames, SHA-1 digests, and byte sizes."""
    ordered = sorted(attachments)
    lines = [f"<!-- archive_attachment_count={len(ordered)} -->"]
    lines.extend(
        "<!-- archive_attachment "
        f"filename={urllib.parse.quote(filename, safe='')} sha1={digest} size={size} -->"
        for filename, digest, size in ordered
    )
    return "\n".join(lines)


def parse_attachment_manifest(
    content: str,
) -> list[tuple[str, str, int]] | None:
    """Parse one complete, unique attachment integrity manifest."""
    count_matches = _ATTACHMENT_COUNT_RE.findall(content)
    if len(count_matches) != 1:
        return None
    expected_count = int(count_matches[0])
    entries = []
    for encoded, digest, size in _ATTACHMENT_MARKER_RE.findall(content):
        filename = urllib.parse.unquote(encoded)
        if urllib.parse.quote(filename, safe="") != encoded:
            return None
        entries.append((filename, digest, int(size)))
    if len(entries) != expected_count or len({name for name, _digest, _size in entries}) != len(entries):
        return None
    return sorted(entries)


def render_external_manifest(
    externals: list[tuple[str, str, int]],
) -> str:
    """Render NAS-hosted (external) attachment names, SHA-1 digests, and sizes."""
    ordered = sorted(externals)
    lines = [f"<!-- archive_external_count={len(ordered)} -->"]
    lines.extend(
        "<!-- archive_external "
        f"filename={urllib.parse.quote(filename, safe='')} sha1={digest} size={size} -->"
        for filename, digest, size in ordered
    )
    return "\n".join(lines)


def parse_external_manifest(
    content: str,
) -> list[tuple[str, str, int]] | None:
    """Parse one complete, unique external (NAS) attachment manifest."""
    count_matches = _EXTERNAL_COUNT_RE.findall(content)
    if len(count_matches) != 1:
        return None
    expected_count = int(count_matches[0])
    entries = []
    for encoded, digest, size in _EXTERNAL_MARKER_RE.findall(content):
        filename = urllib.parse.unquote(encoded)
        if urllib.parse.quote(filename, safe="") != encoded:
            return None
        entries.append((filename, digest, int(size)))
    if len(entries) != expected_count or len({name for name, _d, _s in entries}) != len(entries):
        return None
    return sorted(entries)


def render_thread_manifest(thread_names: dict[int, str]) -> str:
    """Render immutable thread IDs and hashes of their captured names."""
    lines = [f"<!-- source_thread_count={len(thread_names)} -->"]
    lines.extend(
        f"<!-- source_thread_id={thread_id} name_sha256={content_sha256(name)} -->"
        for thread_id, name in sorted(thread_names.items())
    )
    return "\n".join(lines)


def parse_thread_manifest(content: str) -> dict[int, str] | None:
    """Parse one complete thread ID/name-hash manifest."""
    count_matches = _THREAD_COUNT_RE.findall(content)
    if len(count_matches) != 1:
        return None
    expected_count = int(count_matches[0])
    entries = [(int(thread_id), digest) for thread_id, digest in _THREAD_MARKER_RE.findall(content)]
    if len(entries) != expected_count or len({thread_id for thread_id, _digest in entries}) != len(entries):
        return None
    return dict(entries)


def seal_canonical_content(content: str) -> str:
    """Append a SHA-256 seal covering all canonical wikitext before the seal."""
    if _CANONICAL_MARKER_RE.search(content):
        raise ValueError("canonical content already contains an integrity seal")
    base = content.rstrip("\n")
    return f"{base}\n<!-- archive_canonical_sha256={content_sha256(base)} -->\n"


def verify_canonical_content(content: str) -> bool:
    """Verify the unique final seal while excluding only the seal itself."""
    matches = list(_CANONICAL_MARKER_RE.finditer(content))
    if len(matches) != 1:
        return False
    marker = matches[0]
    if content[marker.end():] not in ("", "\n"):
        return False
    prefix = content[:marker.start()]
    if not prefix.endswith("\n"):
        return False
    return content_sha256(prefix[:-1]) == marker.group(1)


def sanitize_title_part(text: str) -> str:
    """Make a string safe as a MediaWiki title component.

    MediaWiki caps a full page title at 255 UTF-8 bytes, so bound the component
    by encoded byte length (a name of many emoji/CJK characters can be well under
    255 characters yet exceed the byte limit). 200 bytes leaves room for the
    ``Archive:Misc/`` prefix and any ``/Part N`` suffix.
    """
    text = _TITLE_BAD.sub("-", text).strip().strip("-")
    text = re.sub(r"\s+", "_", text) or "unnamed"
    return _truncate_bytes(text, 200).strip("-_") or "unnamed"


def sanitize_filename(name: str) -> str:
    """Make an upload filename safe and keep it below 200 UTF-8 bytes.

    MediaWiki applies its title limit to bytes, not Python characters. Keeping
    the extension while truncating by bytes avoids rejecting filenames that
    contain many emoji or CJK characters.
    """
    name = name.replace(" ", "_")
    name = re.sub(r"[#<>\[\]|{}:/]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-._") or "file"
    if len(name.encode("utf-8")) <= 200:
        return name
    stem, dot, extension = name.rpartition(".")
    if not dot or not extension:
        return _truncate_bytes(name, 200).rstrip("-._") or "file"
    suffix = f".{extension}"
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix_bytes >= 200:
        return _truncate_bytes(name, 200).rstrip("-._") or "file"
    stem = _truncate_bytes(stem, 200 - suffix_bytes).rstrip("-._") or "file"
    return f"{stem}{suffix}"


def parse_name_list(raw: str | None) -> list[str]:
    """Split a comma-separated option into ordered, unique, casefolded names.

    Trims surrounding whitespace and a leading ``#`` from each entry, drops empty
    entries, and de-duplicates case-insensitively while preserving the order the
    user typed. Used for the ``/publish`` category and channel name lists so a
    single validated parser governs both.
    """
    seen: set[str] = set()
    names: list[str] = []
    for token in (raw or "").split(","):
        name = token.strip().lstrip("#").strip().casefold()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


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
    # Keep immutable ids at the beginning so byte truncation can never remove
    # them when a long multibyte channel/original filename consumes the budget.
    return sanitize_filename(f"{message_id}-{attachment_id}-{channel_name}-{filename}")


def zip_fallback_upload_name(upload_name: str) -> str:
    """Return a ZIP name that cannot retain a blacklisted compound extension.

    MediaWiki checks every extension segment in names such as ``script.py.zip``.
    Replace dots in the original upload name before adding the permitted ZIP
    extension; the archived file itself keeps its original name inside the ZIP.
    """
    extension_safe_stem = sanitize_filename(upload_name).replace(".", "_")
    return sanitize_filename(f"{extension_safe_stem}.zip")


_IMAGE_EXT = {"png", "gif", "jpg", "jpeg", "webp"}


def _human_size(num_bytes) -> str:
    """Format a byte count as a short human-readable size (e.g. 1.4 GB)."""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _render_attachment(att: dict) -> str:
    """Render one attachment as wikitext, using its uploaded wiki filename."""
    original = escape_wikitext(att.get("filename", "file"))
    # NAS-hosted (external) attachment: too large for the wiki, stored on the file
    # share and linked directly. Rendered as an external link, not a File: embed.
    nas_url = att.get("nas_url")
    if nas_url:
        size = att.get("nas_size")
        suffix = f" ''({_human_size(size)}, external)''" if size else " ''(external)''"
        return f"&#128206; [{nas_url} {original}]{suffix}"
    wiki_name = att.get("wiki_filename")
    if not wiki_name:
        return f"&#128206; {original} ''(attachment not archived: {escape_wikitext(att.get('error', 'upload skipped'))})''"
    ext = wiki_name.rsplit(".", 1)[-1].lower() if "." in wiki_name else ""
    if ext in _IMAGE_EXT:
        return f"[[File:{wiki_name}|thumb|none|400px|{original}]]"
    suffix = " ''(zipped)''" if att.get("zipped") else ""
    return f"&#128206; [[Media:{wiki_name}|{original}]]{suffix}"


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
    part: bool = False,
    anchor_index: dict | None = None,
    self_title: str | None = None,
) -> str:
    """Render the full archive page wikitext for a channel.

    ``messages`` is an ordered list of dicts (see Archive_Bot.capture_channel);
    each attachment dict may carry ``wiki_filename`` (set after upload) or
    ``error``. ``meta`` supplies header fields (guild, channel_id, captured_at,
    message_count, source).

    ``index`` marks a split archive's index page (suppresses the empty-channel
    notice). ``part`` marks an auxiliary content page: it intentionally carries
    no deletion-clearance marker, without falsely labeling successful content as
    incomplete. ``anchor_index`` maps a message id to the page title that holds
    it, and ``self_title`` is this page's title, so cross-part reply links point
    at the correct part page instead of a dead same-page anchor.
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
    # Ownership survives incomplete publication. The completion marker below is
    # deliberately absent until every required publication step succeeds, but a
    # failed/in-progress page must still be protected from another same-named
    # Discord channel overwriting it.
    lines.append(f"<!-- archive_owner_channel_id={meta.get('channel_id')} -->")
    if part:
        lines.append("<!-- Archive part; deletion clearance is recorded on the canonical index page. -->")
    elif meta.get("complete", True):
        lines.append(render_thread_manifest(meta.get("thread_names", {})))
        # A boundary per independently-read message stream prevents a later
        # thread snowflake from hiding a parent message that arrived mid-capture.
        for stream_id, boundary in sorted(meta.get("stream_boundaries", {}).items()):
            lines.append(f"<!-- source_stream_id={stream_id} captured_until={boundary} -->")
        lines.append(
            f"<!-- source_channel_id={meta.get('channel_id')} captured_at={meta.get('captured_at')} "
            f"captured_until={meta.get('captured_until')} -->"
        )
    else:
        lines.append("<!-- INCOMPLETE: publication is not finalised; deletion is blocked. -->")
    return "\n".join(lines)


def split_messages(
    messages: list,
    channel_name: str,
    meta: dict,
    max_bytes: int = 1_800_000,
    *,
    page_title: str | None = None,
) -> list[list]:
    """Partition messages so each rendered page stays below MediaWiki's byte limit.

    MediaWiki's article-size limit ($wgMaxArticleSize, default 2 MiB) counts UTF-8
    bytes, so measure the encoded byte length (multibyte emoji/names count for more
    than one character).
    """
    # Measure each message's rendered byte cost once (O(n) total) instead of
    # re-rendering the whole growing chunk every iteration (which was O(n^2) and
    # could stall /publish on very active channels). The per-message cost is the
    # rendered page size minus the fixed header/footer overhead.
    part_meta = {**meta, "complete": False}
    # index=True suppresses the empty-channel notice. Non-empty part pages do not
    # contain that notice either, so this is their real fixed overhead; including
    # it here would subtract the notice from every message cost and undercount
    # channels containing many short messages.
    overhead = len(render_page(
        channel_name, [], part_meta, index=True, part=True
    ).encode("utf-8"))

    base_title = page_title or make_page_title(channel_name)
    def part_title(number: int) -> str:
        return f"{base_title}/Part {number}"

    def msg_cost(m, part_number: int) -> int:
        # Always reserve a maximum-length cross-part target for a reply. Some
        # replies will ultimately use a shorter same-page fragment, but never the
        # reverse; the final rendered part can therefore only be smaller than this
        # O(n) estimate. Six-digit part counts are far beyond Discord's capacity.
        priced_index = None
        reply_to = m.get("reply_to")
        if reply_to:
            priced_index = {reply_to: f"{base_title}/Part 999999"}
        rendered = render_page(
            channel_name, [m], part_meta, part=True,
            anchor_index=priced_index, self_title=part_title(part_number),
        )
        return max(1, len(rendered.encode("utf-8")) - overhead)

    chunks = []
    current = []
    current_size = 0
    part_number = 1
    active_thread = None  # last thread_header seen, so continuations keep context
    for message in messages:
        if message.get("thread_header"):
            active_thread = message
        cost = msg_cost(message, part_number)
        if current and overhead + current_size + cost > max_bytes:
            chunks.append(current)
            current = []
            current_size = 0
            part_number += 1
            # If the split fell inside a thread, re-emit its heading so the
            # continuation part does not render thread replies as if they were
            # parent-channel messages (losing their source-thread attribution).
            if active_thread is not None and not message.get("thread_header"):
                cont = dict(active_thread)
                cont["thread_header"] = f"{active_thread['thread_header']} (continued)"
                current.append(cont)
                current_size += msg_cost(cont, part_number)
            # Moving this reply to a new part can turn a short local fragment into
            # a much longer cross-part target, so recompute after the split.
            cost = msg_cost(message, part_number)
        if overhead + current_size + cost > max_bytes:
            raise ValueError(
                f"one message plus its required thread/reply context exceeds the "
                f"{max_bytes}-byte archive page limit"
            )
        current.append(message)
        current_size += cost
    if current or not chunks:
        chunks.append(current)
    return chunks
