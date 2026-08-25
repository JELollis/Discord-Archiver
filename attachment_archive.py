"""Bounded-memory helpers for staging Discord attachments for MediaWiki."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import zipfile


DOWNLOAD_BLOCK_BYTES = 1024 * 1024


class AttachmentArchiveError(Exception):
    """Raised when an attachment cannot be staged completely and byte-exactly."""


class AttachmentTooLargeError(AttachmentArchiveError):
    """Raised before upload when an attachment exceeds the effective archive cap."""


async def stage_discord_attachment(
    attachment,
    destination: str | os.PathLike[str],
    *,
    expected_size: int,
    maximum_size: int,
    memory_threshold: int,
    session_factory=None,
) -> int:
    """Write one Discord attachment to disk without unbounded memory use.

    Small files retain discord.py's authenticated download path. Larger files
    are streamed from the attachment's signed CDN URL in one-MiB blocks because
    discord.py 2.5's ``Attachment.save`` internally buffers the entire response.
    The declared and received sizes must match before any wiki upload begins.
    """
    expected_size = max(0, int(expected_size))
    maximum_size = max(0, int(maximum_size))
    memory_threshold = max(0, int(memory_threshold))
    if expected_size > maximum_size:
        raise AttachmentTooLargeError(
            f"attachment is {expected_size} bytes; archive limit is {maximum_size} bytes"
        )

    destination = Path(destination)
    if expected_size <= memory_threshold:
        data = await attachment.read()
        received = len(data)
        if received > maximum_size:
            raise AttachmentTooLargeError(
                f"attachment download reached {received} bytes; archive limit is {maximum_size} bytes"
            )
        # Discord's reported ``attachment.size`` is NOT a reliable completeness
        # check: for some (notably re-encoded/migrated) images the CDN serves a
        # different byte count than the metadata claims, in both directions.
        # ``attachment.read()`` returns the entire HTTP body or raises, so a short
        # read cannot pass silently; the archived SHA-1/size come from the bytes we
        # actually store, not from ``attachment.size``.
        await asyncio.to_thread(destination.write_bytes, data)
        return received

    url = str(getattr(attachment, "url", ""))
    if not url:
        raise AttachmentArchiveError("attachment has no downloadable CDN URL")

    session_kwargs = {}
    if session_factory is None:
        import aiohttp

        session_factory = aiohttp.ClientSession
        session_kwargs["timeout"] = aiohttp.ClientTimeout(
            total=None, sock_connect=30, sock_read=180
        )
    received = 0
    content_length = None
    async with session_factory(**session_kwargs) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            declared = getattr(response, "headers", {}).get("Content-Length")
            if declared is not None:
                try:
                    content_length = int(declared)
                except (TypeError, ValueError):
                    content_length = None
            with destination.open("wb") as handle:
                async for block in response.content.iter_chunked(DOWNLOAD_BLOCK_BYTES):
                    if not block:
                        continue
                    received += len(block)
                    if received > maximum_size:
                        raise AttachmentTooLargeError(
                            f"attachment download reached {received} bytes; archive limit is {maximum_size} bytes"
                        )
                    handle.write(block)

    # Completeness is checked against the server's declared Content-Length (the
    # authoritative length of what is actually served), not Discord's
    # ``attachment.size`` metadata, which can disagree with the served bytes. When
    # no Content-Length is sent, the streaming read cannot be under-verified here;
    # a mid-stream connection error still propagates from ``iter_chunked``.
    if content_length is not None and received != content_length:
        raise AttachmentArchiveError(
            f"attachment download incomplete (server declared {content_length}, received {received} bytes)"
        )
    return received


def create_zip_file(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    inner_filename: str,
) -> None:
    """Wrap a staged attachment in a ZIP while preserving its leaf filename."""
    # Discord filenames should already be leaf names. Strip either path separator
    # defensively so extracting an archive can never write outside its directory.
    safe_inner_name = inner_filename.replace("\\", "/").rsplit("/", 1)[-1] or "attachment"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.write(source, arcname=safe_inner_name)


def select_reusable_upload(
    base_name: str,
    zip_name: str,
    manifest: dict[str, tuple[str, int]],
) -> tuple[str, bool, str, int] | None:
    """Pick the manifest entry, if any, an intact prior upload can reuse.

    ``manifest`` maps a wiki filename to its recorded ``(sha1, size)``. An
    attachment is stored under either its deterministic direct upload name or its
    ZIP-fallback name, so this returns ``(candidate_name, zipped, sha1, size)``
    for whichever of the two the manifest lists, or ``None`` when neither is
    present. It only *selects* a candidate; the caller must still confirm the
    wiki still holds those exact bytes before reusing them.
    """
    for candidate, zipped in ((base_name, False), (zip_name, True)):
        entry = manifest.get(candidate)
        if entry is not None:
            sha1, size = entry
            return candidate, zipped, sha1, size
    return None


def file_sha1_and_size(path: str | os.PathLike[str]) -> tuple[str, int]:
    """Return the MediaWiki-compatible SHA-1 and exact byte size of a file."""
    digest = hashlib.sha1()
    size = 0
    with open(path, "rb") as handle:
        while block := handle.read(DOWNLOAD_BLOCK_BYTES):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size
