"""Shared registry and live verification for empty Discord text channels.

Empty channels have no message content worth publishing to an individual wiki
page.  The bot records them in one sealed MediaWiki list instead, keyed by
immutable Discord IDs, and rechecks Discord immediately before deletion.
"""

from __future__ import annotations

import datetime
import re
import urllib.parse

import discord_export
from wiki_client import WikiError


_FORMAT_RE = re.compile(r"<!-- empty_channel_list_format=(\d+) -->")
_COUNT_RE = re.compile(r"<!-- empty_channel_count=(\d+) -->")
_ENTRY_RE = re.compile(
    r"<!-- empty_channel "
    r"guild_id=(\d+) channel_id=(\d+) "
    r"verified_at=([^ ]+) channel_name=([^ ]*) category_name=([^ ]*) -->"
)
_UTC_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z"
)
_EDIT_ATTEMPTS = 4


def list_page_title(namespace: str = "Archive") -> str:
    return f"{namespace}:Empty Channel List"


def entry_anchor(channel_id: int) -> str:
    return f"empty-channel-{int(channel_id)}"


def make_record(
    *,
    guild_id: int,
    channel_id: int,
    channel_name: str,
    category_name: str,
    verified_at: str,
) -> dict:
    """Build and validate one registry record."""
    record = {
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "channel_name": str(channel_name),
        "category_name": str(category_name or ""),
        "verified_at": str(verified_at),
    }
    if record["guild_id"] <= 0 or record["channel_id"] <= 0:
        raise ValueError("Discord guild and channel IDs must be positive")
    if not record["channel_name"]:
        raise ValueError("empty-channel record requires a channel name")
    if not _valid_utc_timestamp(record["verified_at"]):
        raise ValueError("empty-channel record requires a valid UTC timestamp")
    return record


def _valid_utc_timestamp(value: str) -> bool:
    if not _UTC_TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() == datetime.timedelta(0)


def _encode(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _decode_canonical(value: str) -> str | None:
    decoded = urllib.parse.unquote(value)
    return decoded if _encode(decoded) == value else None


def render_empty_channel_list(records: dict[int, dict]) -> str:
    """Render a deterministic human table plus machine-readable sealed records."""
    normalized = {}
    for key, value in records.items():
        record = make_record(**value)
        if int(key) != record["channel_id"] or record["channel_id"] in normalized:
            raise ValueError("empty-channel records must be uniquely keyed by channel ID")
        normalized[record["channel_id"]] = record

    ordered = sorted(
        normalized.values(),
        key=lambda item: (item["verified_at"], item["channel_id"]),
    )
    lines = [
        "''Bot-maintained record of Discord channels verified empty during `/publish`.''",
        "",
        "{| class=\"wikitable sortable\"",
        "! Channel !! Category !! Guild ID !! Channel ID !! Verified empty (UTC)",
    ]
    for record in ordered:
        category = (
            discord_export.escape_wikitext(record["category_name"])
            if record["category_name"] else "''(none)''"
        )
        lines.extend([
            "|-",
            f'| <span id="{entry_anchor(record["channel_id"])}"></span>'
            f'#{discord_export.escape_wikitext(record["channel_name"])} '
            f'|| {category} || {record["guild_id"]} || {record["channel_id"]} '
            f'|| {record["verified_at"]}',
        ])
    lines.extend([
        "|}",
        "",
        "[[Category:Discord archive]]",
        "",
        "<!-- empty_channel_list_format=1 -->",
        f"<!-- empty_channel_count={len(ordered)} -->",
    ])
    lines.extend(
        "<!-- empty_channel "
        f"guild_id={record['guild_id']} channel_id={record['channel_id']} "
        f"verified_at={record['verified_at']} "
        f"channel_name={_encode(record['channel_name'])} "
        f"category_name={_encode(record['category_name'])} -->"
        for record in ordered
    )
    return discord_export.seal_canonical_content("\n".join(lines))


def parse_empty_channel_list(content: str) -> dict[int, dict] | None:
    """Parse a complete, uniquely keyed v1 registry or fail closed."""
    if not discord_export.verify_canonical_content(content):
        return None
    formats = _FORMAT_RE.findall(content)
    counts = _COUNT_RE.findall(content)
    if formats != ["1"] or len(counts) != 1:
        return None
    expected_count = int(counts[0])
    records = {}
    for guild_id, channel_id, verified_at, encoded_name, encoded_category in _ENTRY_RE.findall(content):
        channel_name = _decode_canonical(encoded_name)
        category_name = _decode_canonical(encoded_category)
        if channel_name is None or category_name is None:
            return None
        try:
            record = make_record(
                guild_id=int(guild_id),
                channel_id=int(channel_id),
                channel_name=channel_name,
                category_name=category_name,
                verified_at=verified_at,
            )
        except (TypeError, ValueError):
            return None
        if record["channel_id"] in records:
            return None
        records[record["channel_id"]] = record
    if len(records) != expected_count:
        return None
    # The seal protects the bytes, while deterministic re-rendering also proves
    # the human table and machine markers describe the same exact records with
    # no extra bot-looking material inserted elsewhere on the page.
    if render_empty_channel_list(records) != content:
        return None
    return records


async def load_records(client, namespace: str) -> tuple[dict[int, dict], dict | None]:
    """Read and validate the shared registry."""
    title = list_page_title(namespace)
    page = await client.get_page(title)
    if page is None:
        return {}, None
    records = parse_empty_channel_list(page.get("content", ""))
    if records is None:
        raise WikiError(
            f"empty-channel registry {title!r} is malformed or failed its integrity seal"
        )
    return records, page


async def upsert_record(client, namespace: str, record: dict) -> dict:
    """Atomically insert/update one record and verify it from MediaWiki."""
    record = make_record(**record)
    title = list_page_title(namespace)
    for attempt in range(_EDIT_ATTEMPTS):
        records, page = await load_records(client, namespace)
        records[record["channel_id"]] = record
        content = render_empty_channel_list(records)
        try:
            edit = await client.edit_page(
                title,
                content,
                f"Record empty Discord channel #{record['channel_name']}",
                createonly=page is None,
                baserevid=page["revid"] if page else None,
            )
        except WikiError as exc:
            if exc.code in {"articleexists", "editconflict"} and attempt < _EDIT_ATTEMPTS - 1:
                continue
            raise

        saved = await client.get_page(title)
        saved_records = parse_empty_channel_list(saved.get("content", "")) if saved else None
        nochange = edit.get("result") == "Nochange" or (
            edit.get("result") == "Success" and "nochange" in edit
        )
        edit_visible = bool(
            saved and (
                nochange
                or saved.get("revid") == edit.get("newrevid")
                # A later valid concurrent edit may already be visible; the
                # requested record must still survive that revision.
                or (saved_records and saved_records.get(record["channel_id"]) == record)
            )
        )
        if edit_visible and saved_records and saved_records.get(record["channel_id"]) == record:
            return saved
        raise WikiError("empty-channel registry failed read-back verification")
    raise WikiError("empty-channel registry update exhausted conflict retries")


async def is_completely_empty(channel, iter_threads) -> bool:
    """Check the parent and every active/archived thread for any message.

    Enumeration/read exceptions deliberately propagate so callers can fail
    closed instead of mistaking an inaccessible stream for an empty one.
    """
    async for _message in channel.history(limit=1, oldest_first=False):
        return False
    threads = [thread async for thread in iter_threads(channel)]
    for thread in threads:
        async for _message in thread.history(limit=1, oldest_first=False):
            return False
    return True
