"""Async MediaWiki API client for the GTC Discord Archive bot.

Authenticates with a MediaWiki BotPassword and exposes the small set of
operations the archiver needs: edit pages, upload attachments, and read a
revision back for post-publish verification.

Configuration comes from the environment (systemd can load it via
``EnvironmentFile=/etc/discord-archiver/wiki.env``); for local development a
``wiki.env`` next to this file, or ``/etc/discord-archiver/wiki.env``, is parsed
as a fallback:

    MEDIAWIKI_API_URL          e.g. https://gtc-wiki.completeelectronics.net/api.php
    MEDIAWIKI_BOT_USERNAME     e.g. DiscordArchiveBot@ArchivePublisher
    MEDIAWIKI_BOT_PASSWORD     the BotPassword secret
    MEDIAWIKI_ARCHIVE_NAMESPACE  e.g. Archive
    MEDIAWIKI_UPLOAD_CHUNK_MIB   optional, defaults to 20
    MEDIAWIKI_MAX_ATTACHMENT_MIB optional bot safety cap, defaults to 500

Note: the wiki sits behind Cloudflare, which rejects requests with a default
library User-Agent, so every request sends an explicit UA below. When the bot
runs on the same host as MediaWiki (WEB1), pointing MEDIAWIKI_API_URL at the
local Apache (a 127.0.0.1 hosts entry for the wiki FQDN) avoids the Cloudflare
round-trip entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import aiohttp

USER_AGENT = (
    "GTC-Archive-Bot/1.0 "
    "(+https://gtc-wiki.completeelectronics.net; archive publisher)"
)

_MAX_REQUEST_ATTEMPTS = 4
_TRANSPORT_RETRY_DELAYS = (1.0, 2.0, 4.0)
# A MediaWiki upload limit commonly uses a one-minute window. These waits total
# 70 seconds, so a request that reaches the final attempt gets a clean window.
_RATE_LIMIT_RETRY_DELAYS = (10.0, 20.0, 40.0)
_RETRYABLE_API_CODES = frozenset({"ratelimited", "maxlag", "readonly"})
_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_DEFAULT_UPLOAD_CHUNK_MIB = 20
_DEFAULT_MAX_ATTACHMENT_MIB = 500


class WikiError(Exception):
    """Raised when the MediaWiki API returns an error or an unexpected result."""

    def __init__(self, message: str, *, error: Optional[dict] = None):
        super().__init__(message)
        self.error = dict(error or {})

    @property
    def code(self) -> Optional[str]:
        code = self.error.get("code")
        return str(code) if code is not None else None

    def has_detail(self, token: str) -> bool:
        details = self.error.get("details", ())
        if isinstance(details, (str, bytes)):
            details = (details,)
        return token in details

    @property
    def is_file_type_rejection(self) -> bool:
        """Whether preserving the upload inside a ZIP is a safe fallback.

        Covers the several ways MediaWiki refuses a file by type or name, all of
        which a ZIP resolves because the archive's own name/extension/MIME are
        accepted while the original bytes are preserved unchanged:
        - ``filetype-banned``      — the extension is on the deny list (e.g. .exe)
        - ``filetype-mime-mismatch`` — extension vs detected MIME disagree
        - ``filetype-badmime``     — the detected MIME itself is not allowed
          (e.g. a .js served as text/html, a .zip detected as application/java)
        - ``illegal-filename``     — the filename/extension is not permitted
          (seen on the chunked path for .exe/.iso/.mkv)
        """
        markers = ("filetype-banned", "filetype-mime-mismatch", "filetype-badmime")
        if self.code in ("filetype-banned", "illegal-filename"):
            return True
        if any(self.has_detail(marker) for marker in markers):
            return True
        # Preserve compatibility with custom/older callers that only put the
        # MediaWiki error token in the exception message.
        text = str(self)
        return any(marker in text for marker in markers) or "illegal-filename" in text


class UnexpectedResponseError(Exception):
    """Raised when the API returns a successful response that is not JSON."""

    def __init__(
        self,
        *,
        status: int,
        content_type: str,
        server: str,
        preview: str,
    ):
        self.status = status
        self.content_type = content_type
        self.server = server
        self.preview = preview
        details = f"HTTP {status}, content-type {content_type or 'missing'}"
        if server:
            details += f", server {server}"
        if preview:
            details += f", body starts: {preview!r}"
        super().__init__(f"MediaWiki API returned non-JSON response ({details})")


def load_config(env: Optional[dict] = None) -> dict:
    """Return the MediaWiki settings from the environment, with a file fallback.

    Order of precedence: real environment variables first, then the first
    readable of ``./wiki.env`` or ``/etc/discord-archiver/wiki.env``. Raises
    ``WikiError`` listing anything still missing so misconfiguration fails loudly.
    """
    values = dict(env or os.environ)
    keys = (
        "MEDIAWIKI_API_URL",
        "MEDIAWIKI_BOT_USERNAME",
        "MEDIAWIKI_BOT_PASSWORD",
        "MEDIAWIKI_ARCHIVE_NAMESPACE",
    )
    if not all(values.get(k) for k in keys):
        for path in ("wiki.env", "/etc/discord-archiver/wiki.env"):
            if os.path.isfile(path):
                for line in _parse_env_file(path).items():
                    values.setdefault(*line)
                break

    config = {k: values.get(k) for k in keys}
    if not config.get("MEDIAWIKI_ARCHIVE_NAMESPACE"):
        config["MEDIAWIKI_ARCHIVE_NAMESPACE"] = "Archive"
    missing = [k for k in keys if not config.get(k)]
    if missing:
        raise WikiError(f"Missing MediaWiki configuration: {', '.join(missing)}")

    for setting, default in (
        ("MEDIAWIKI_UPLOAD_CHUNK_MIB", _DEFAULT_UPLOAD_CHUNK_MIB),
        ("MEDIAWIKI_MAX_ATTACHMENT_MIB", _DEFAULT_MAX_ATTACHMENT_MIB),
    ):
        raw_value = values.get(setting, default)
        try:
            value_mib = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise WikiError(f"{setting} must be a positive whole number") from exc
        if value_mib <= 0:
            raise WikiError(f"{setting} must be a positive whole number")
        config[f"{setting.removesuffix('_MIB')}_BYTES"] = value_mib * 1024 * 1024

    if config["MEDIAWIKI_UPLOAD_CHUNK_BYTES"] > config["MEDIAWIKI_MAX_ATTACHMENT_BYTES"]:
        raise WikiError("MEDIAWIKI_UPLOAD_CHUNK_MIB cannot exceed MEDIAWIKI_MAX_ATTACHMENT_MIB")

    # Optional NAS "oversize sink": attachments too large for the wiki (or of a
    # designated type) are stored on a mounted file share and linked instead of
    # uploaded. Disabled unless both a directory and a public URL base are set.
    config["ARCHIVE_NAS_DIR"] = (values.get("ARCHIVE_NAS_DIR") or "").strip()
    config["ARCHIVE_NAS_URL_BASE"] = (values.get("ARCHIVE_NAS_URL_BASE") or "").strip().rstrip("/")
    nas_max_raw = values.get("ARCHIVE_NAS_MAX_MIB", 8192)
    try:
        nas_max_mib = int(nas_max_raw)
    except (TypeError, ValueError) as exc:
        raise WikiError("ARCHIVE_NAS_MAX_MIB must be a positive whole number") from exc
    if nas_max_mib <= 0:
        raise WikiError("ARCHIVE_NAS_MAX_MIB must be a positive whole number")
    config["ARCHIVE_NAS_MAX_BYTES"] = nas_max_mib * 1024 * 1024
    config["ARCHIVE_NAS_ENABLED"] = bool(
        config["ARCHIVE_NAS_DIR"] and config["ARCHIVE_NAS_URL_BASE"]
    )
    return config


def _parse_env_file(path: str) -> dict:
    """Parse a simple KEY=VALUE env file (ignores blanks/comments, strips quotes)."""
    out: dict = {}
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip("'").strip('"')
    return out


class MediaWikiClient:
    """Minimal async MediaWiki API client scoped to the archiver's needs."""

    def __init__(
        self,
        api_url: str,
        username: str,
        password: str,
        *,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.api_url = api_url
        self.username = username
        self.password = password
        self._session = session
        self._owns_session = session is None
        self._csrf: Optional[str] = None
        self.logged_in = False
        self._max_upload_size: Optional[int] = None
        # One authenticated bot account shares one rate-limit budget and one
        # connection pool. Serializing requests also makes pool resets race-free.
        self._request_lock = asyncio.Lock()

    @classmethod
    def from_config(cls, config: Optional[dict] = None) -> "MediaWikiClient":
        cfg = config or load_config()
        return cls(
            cfg["MEDIAWIKI_API_URL"],
            cfg["MEDIAWIKI_BOT_USERNAME"],
            cfg["MEDIAWIKI_BOT_PASSWORD"],
        )

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            # Timeouts so a stalled wiki/Cloudflare connection fails fast instead of
            # hanging the whole bot forever. No hard total (large uploads may run
            # long), but a connection that goes silent for sock_read seconds aborts.
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=180)
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT}, timeout=timeout)
            self._owns_session = True

    async def _discard_owned_session(self) -> None:
        """Drop a stale connection pool before a transport-level retry."""
        if not self._owns_session or self._session is None:
            return
        session = self._session
        self._session = None
        if not session.closed:
            await session.close()

    async def _sleep_before_retry(self, delay: float) -> None:
        """Test seam for retry delays."""
        await asyncio.sleep(delay)

    @staticmethod
    def _retry_after_seconds(headers) -> Optional[float]:
        if not headers:
            return None
        value = headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    async def _request_once(
        self,
        method: str,
        *,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
    ) -> tuple[dict, Optional[float]]:
        """Issue one request, rebuilding multipart data for every attempt."""
        await self._ensure_session()
        request_kwargs = {}
        if params is not None:
            request_kwargs["params"] = params
        elif files:
            form = aiohttp.FormData()
            for key, value in (data or {}).items():
                form.add_field(key, str(value))
            for key, (filename, content, ctype) in files.items():
                form.add_field(key, content, filename=filename, content_type=ctype)
            request_kwargs["data"] = form
        else:
            request_kwargs["data"] = data

        async with self._session.request(
            method, self.api_url, **request_kwargs
        ) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            if "json" not in content_type.lower():
                body = await resp.text(errors="replace")
                preview = " ".join(body.split())[:240]
                raise UnexpectedResponseError(
                    status=resp.status,
                    content_type=content_type,
                    server=resp.headers.get("Server", ""),
                    preview=preview,
                )
            result = await resp.json()
            return result, self._retry_after_seconds(resp.headers)

    async def _request_json(
        self,
        method: str,
        *,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
    ) -> dict:
        async with self._request_lock:
            return await self._request_json_with_retries(
                method, params=params, data=data, files=files
            )

    async def _request_json_with_retries(
        self,
        method: str,
        *,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
    ) -> dict:
        """Request JSON with bounded retries for transient wiki/network failures."""
        action_data = params if params is not None else data
        action = (action_data or {}).get("action", "request")

        for attempt in range(_MAX_REQUEST_ATTEMPTS):
            retry_after = None
            try:
                result, retry_after = await self._request_once(
                    method, params=params, data=data, files=files
                )
            except (UnexpectedResponseError, aiohttp.ContentTypeError) as exc:
                # A proxy or upstream PHP failure can return HTML with HTTP 200.
                retryable_exc = exc
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                aiohttp.ServerTimeoutError,
                asyncio.TimeoutError,
            ) as exc:
                retryable_exc = exc
            except aiohttp.ClientResponseError as exc:
                if exc.status not in _RETRYABLE_HTTP_STATUSES:
                    raise
                retryable_exc = exc
                retry_after = self._retry_after_seconds(exc.headers)
            else:
                error = result.get("error") if isinstance(result, dict) else None
                code = str(error.get("code", "")) if isinstance(error, dict) else ""
                if code not in _RETRYABLE_API_CODES or attempt == _MAX_REQUEST_ATTEMPTS - 1:
                    return result
                delay = (
                    retry_after
                    if retry_after is not None
                    else _RATE_LIMIT_RETRY_DELAYS[attempt]
                )
                logging.warning(
                    "MediaWiki %s returned %s; retrying in %.1fs (attempt %d/%d)",
                    action, code, delay, attempt + 2, _MAX_REQUEST_ATTEMPTS,
                )
                await self._sleep_before_retry(delay)
                continue

            if attempt == _MAX_REQUEST_ATTEMPTS - 1:
                raise retryable_exc
            await self._discard_owned_session()
            delay = (
                retry_after
                if retry_after is not None
                else _TRANSPORT_RETRY_DELAYS[attempt]
            )
            logging.warning(
                "MediaWiki %s transport failure (%s); retrying in %.1fs (attempt %d/%d)",
                action, type(retryable_exc).__name__, delay,
                attempt + 2, _MAX_REQUEST_ATTEMPTS,
            )
            await self._sleep_before_retry(delay)

        raise AssertionError("unreachable MediaWiki retry state")

    async def _get(self, params: dict) -> dict:
        payload = dict(params)
        payload.setdefault("format", "json")
        return await self._request_json("GET", params=payload)

    async def _post(self, data: dict, *, files: Optional[dict] = None) -> dict:
        payload = dict(data)
        payload.setdefault("format", "json")
        return await self._request_json("POST", data=payload, files=files)

    async def login(self) -> dict:
        """Authenticate with the BotPassword and cache a CSRF token."""
        token = (await self._get(
            {"action": "query", "meta": "tokens", "type": "login"}
        ))["query"]["tokens"]["logintoken"]
        result = await self._post({
            "action": "login",
            "lgname": self.username,
            "lgpassword": self.password,
            "lgtoken": token,
        })
        if result.get("login", {}).get("result") != "Success":
            raise WikiError(f"MediaWiki login failed: {result.get('login')}")
        self.logged_in = True
        self._csrf = (await self._get(
            {"action": "query", "meta": "tokens", "type": "csrf"}
        ))["query"]["tokens"]["csrftoken"]
        logging.info("MediaWiki login successful as %s", self.username)
        return result

    async def _ensure_csrf(self) -> None:
        if not self._csrf or not self.logged_in:
            await self.login()

    async def _retry_after_auth_error(self, operation):
        """Retry one API operation after clearing stale login/CSRF state."""
        try:
            return await operation()
        except WikiError as exc:
            message = str(exc).lower()
            auth_errors = (
                "badtoken", "notloggedin", "not logged in", "assertuserfailed",
                "readapidenied", "permissiondenied",
            )
            if exc.code not in auth_errors and not any(token in message for token in auth_errors):
                raise
            self._csrf = None
            self.logged_in = False
            await self.login()
            return await operation()

    async def _authenticated_get(self, params: dict) -> dict:
        """Run a private-wiki query and re-login once if its session expired."""
        if not self.logged_in:
            await self.login()

        async def submit():
            result = await self._get(params)
            if "error" in result:
                error = result["error"]
                raise WikiError(f"MediaWiki query failed: {error}", error=error)
            return result

        return await self._retry_after_auth_error(submit)

    async def userinfo(self) -> dict:
        return (await self._authenticated_get({
            "action": "query", "meta": "userinfo",
            "uiprop": "groups|rights|ratelimits",
        }))["query"]["userinfo"]

    async def site_generator(self) -> str:
        return (await self._authenticated_get({
            "action": "query", "meta": "siteinfo", "siprop": "general",
        }))["query"]["general"].get("generator", "unknown")

    async def max_upload_size(self) -> int:
        """Return and cache MediaWiki's advertised total-file upload limit."""
        if self._max_upload_size is None:
            general = (await self._authenticated_get({
                "action": "query", "meta": "siteinfo", "siprop": "general",
            }))["query"]["general"]
            try:
                maximum = int(general["maxuploadsize"])
            except (KeyError, TypeError, ValueError) as exc:
                raise WikiError("MediaWiki siteinfo did not provide a valid maxuploadsize") from exc
            if maximum <= 0:
                raise WikiError("MediaWiki siteinfo returned a non-positive maxuploadsize")
            self._max_upload_size = maximum
        return self._max_upload_size

    async def edit_page(
        self,
        title: str,
        text: str,
        summary: str,
        *,
        bot: bool = True,
        createonly: bool = False,
        baserevid: Optional[int] = None,
    ) -> dict:
        """Create or overwrite a page; returns the API ``edit`` result."""
        await self._ensure_csrf()
        data = {
            "action": "edit",
            "title": title,
            "text": text,
            "summary": summary,
            "token": self._csrf,
        }
        if bot:
            data["bot"] = "1"
        if createonly:
            data["createonly"] = "1"
        if baserevid is not None:
            data["baserevid"] = str(baserevid)
        async def submit():
            data["token"] = self._csrf
            result = await self._post(data)
            if "error" in result:
                error = result["error"]
                raise WikiError(f"edit {title!r} failed: {error}", error=error)
            return result
        return (await self._retry_after_auth_error(submit))["edit"]

    async def upload_file(
        self,
        filename: str,
        content: bytes,
        comment: str = "",
        *,
        ignorewarnings: bool = True,
    ) -> dict:
        """Upload binary content as ``filename`` (File: namespace)."""
        await self._ensure_csrf()
        data = {
            "action": "upload",
            "filename": filename,
            "comment": comment,
            "token": self._csrf,
        }
        if ignorewarnings:
            data["ignorewarnings"] = "1"
        files = {"file": (filename, content, "application/octet-stream")}
        async def submit():
            data["token"] = self._csrf
            result = await self._post(data, files=files)
            return self._validate_upload_result(result, filename)
        return (await self._retry_after_auth_error(submit))["upload"]

    @staticmethod
    def _validate_upload_result(result: dict, filename: str) -> dict:
        """Normalize idempotent upload success and preserve structured errors."""
        if "error" not in result:
            return result
        error = result["error"]
        # MediaWiki reports an exact duplicate as an error-shaped response even
        # though the requested file is already present and usable.
        if error.get("code") == "fileexists-no-change":
            return {
                "upload": {
                    "result": "Success",
                    "filename": filename,
                    "duplicate": True,
                }
            }
        raise WikiError(f"upload {filename!r} failed: {error}", error=error)

    async def upload_file_from_path(
        self,
        filename: str,
        path: str | os.PathLike[str],
        comment: str = "",
        *,
        chunk_size: int,
        ignorewarnings: bool = True,
    ) -> dict:
        """Upload a local file, using MediaWiki's native stash chunks when needed."""
        path = Path(path)
        filesize = path.stat().st_size
        chunk_size = int(chunk_size)
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if filesize <= chunk_size:
            content = await asyncio.to_thread(path.read_bytes)
            return await self.upload_file(
                filename, content, comment, ignorewarnings=ignorewarnings
            )
        return await self._upload_file_in_chunks(
            filename,
            path,
            filesize,
            comment,
            chunk_size=chunk_size,
            ignorewarnings=ignorewarnings,
        )

    async def _upload_file_in_chunks(
        self,
        filename: str,
        path: Path,
        filesize: int,
        comment: str,
        *,
        chunk_size: int,
        ignorewarnings: bool,
    ) -> dict:
        """Stage bounded chunks and commit them as one MediaWiki file."""
        await self._ensure_csrf()
        offset = 0
        filekey = None

        with path.open("rb") as handle:
            while offset < filesize:
                chunk = await asyncio.to_thread(
                    handle.read, min(chunk_size, filesize - offset)
                )
                if not chunk:
                    raise WikiError(
                        f"chunk upload {filename!r} ended at {offset} of {filesize} bytes"
                    )
                data = {
                    "action": "upload",
                    "stash": "1",
                    "filename": filename,
                    "filesize": str(filesize),
                    "offset": str(offset),
                    "token": self._csrf,
                }
                if filekey is not None:
                    data["filekey"] = filekey
                if ignorewarnings:
                    data["ignorewarnings"] = "1"
                files = {
                    "chunk": (
                        filename,
                        chunk,
                        "application/octet-stream",
                    )
                }

                async def submit_chunk():
                    data["token"] = self._csrf
                    result = await self._post(data, files=files)
                    if "error" in result:
                        error = result["error"]
                        raise WikiError(
                            f"chunk upload {filename!r} failed at byte {offset}: {error}",
                            error=error,
                        )
                    return result

                result = await self._retry_after_auth_error(submit_chunk)
                upload = result.get("upload", {})
                stash_errors = upload.get("stasherrors")
                if stash_errors:
                    error = {
                        "code": "stashfailed",
                        "info": "MediaWiki rejected the completed upload stash",
                        "details": stash_errors,
                    }
                    raise WikiError(
                        f"chunk upload {filename!r} failed verification: {stash_errors}",
                        error=error,
                    )
                expected_offset = offset + len(chunk)
                next_offset = upload.get("offset")
                if next_offset is None and expected_offset == filesize:
                    # MediaWiki's final stash response contains filekey/imageinfo
                    # but omits the offset present on intermediate Continue replies.
                    next_offset = expected_offset
                else:
                    try:
                        next_offset = int(next_offset)
                    except (TypeError, ValueError) as exc:
                        raise WikiError(
                            f"chunk upload {filename!r} returned an invalid offset: {upload}"
                        ) from exc
                if next_offset != expected_offset:
                    raise WikiError(
                        f"chunk upload {filename!r} returned offset {next_offset}; expected {expected_offset}"
                    )
                filekey = upload.get("filekey") or upload.get("sessionkey") or filekey
                if not filekey:
                    raise WikiError(
                        f"chunk upload {filename!r} did not return a file key"
                    )
                offset = next_offset
                logging.info(
                    "MediaWiki chunk upload %r: %d/%d bytes staged (%.1f%%).",
                    filename,
                    offset,
                    filesize,
                    offset * 100 / filesize,
                )

        data = {
            "action": "upload",
            "filename": filename,
            "filekey": filekey,
            "comment": comment,
            "token": self._csrf,
        }
        if ignorewarnings:
            data["ignorewarnings"] = "1"

        async def commit_upload():
            data["token"] = self._csrf
            result = await self._post(data)
            return self._validate_upload_result(result, filename)

        result = await self._retry_after_auth_error(commit_upload)
        return result["upload"]

    async def get_page(self, title: str) -> Optional[dict]:
        """Return latest content and bounded revision metadata, or ``None``."""
        result = await self._authenticated_get({
            "action": "query",
            "prop": "revisions",
            "rvprop": "ids|timestamp|user|comment|content",
            "rvslots": "main",
            # Two revisions are enough to distinguish an untouched legacy page
            # from anything edited later; no unbounded history fetch is needed.
            "rvlimit": "2",
            "titles": title,
        })
        page = next(iter(result["query"]["pages"].values()))
        if "missing" in page:
            return None
        revision = page["revisions"][0]
        return {
            "pageid": page["pageid"],
            "revid": revision["revid"],
            "content": revision["slots"]["main"]["*"],
            "timestamp": revision.get("timestamp"),
            "user": revision.get("user"),
            "comment": revision.get("comment", ""),
            "revision_count": len(page["revisions"]),
        }

    async def get_file_info(self, filename: str) -> Optional[dict]:
        """Return the current wiki file's SHA-1 and byte size, or ``None``."""
        result = await self._authenticated_get({
            "action": "query",
            "prop": "imageinfo",
            "iiprop": "sha1|size",
            "titles": f"File:{filename}",
        })
        page = next(iter(result["query"]["pages"].values()))
        imageinfo = page.get("imageinfo")
        if "missing" in page or not imageinfo:
            return None
        current = imageinfo[0]
        digest = current.get("sha1")
        size = current.get("size")
        if not digest or size is None:
            return None
        return {"sha1": digest.lower(), "size": int(size)}

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
