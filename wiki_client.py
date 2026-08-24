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

Note: the wiki sits behind Cloudflare, which rejects requests with a default
library User-Agent, so every request sends an explicit UA below. When the bot
runs on the same host as MediaWiki (WEB1), pointing MEDIAWIKI_API_URL at the
local Apache (a 127.0.0.1 hosts entry for the wiki FQDN) avoids the Cloudflare
round-trip entirely.
"""

from __future__ import annotations

import os
import logging
from typing import Optional

import aiohttp

USER_AGENT = (
    "GTC-Archive-Bot/1.0 "
    "(+https://gtc-wiki.completeelectronics.net; archive publisher)"
)


class WikiError(Exception):
    """Raised when the MediaWiki API returns an error or an unexpected result."""


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

    async def _get(self, params: dict) -> dict:
        await self._ensure_session()
        params.setdefault("format", "json")
        async with self._session.get(self.api_url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _post(self, data: dict, *, files: Optional[dict] = None) -> dict:
        await self._ensure_session()
        data.setdefault("format", "json")
        if files:
            form = aiohttp.FormData()
            for key, value in data.items():
                form.add_field(key, str(value))
            for key, (filename, content, ctype) in files.items():
                form.add_field(key, content, filename=filename, content_type=ctype)
            payload = form
        else:
            payload = data
        async with self._session.post(self.api_url, data=payload) as resp:
            resp.raise_for_status()
            return await resp.json()

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
        """Retry one mutation after clearing stale login/CSRF state."""
        try:
            return await operation()
        except WikiError as exc:
            if "badtoken" not in str(exc).lower() and "not logged in" not in str(exc).lower():
                raise
            self._csrf = None
            self.logged_in = False
            await self.login()
            return await operation()

    async def userinfo(self) -> dict:
        return (await self._get({
            "action": "query", "meta": "userinfo", "uiprop": "groups|rights",
        }))["query"]["userinfo"]

    async def site_generator(self) -> str:
        return (await self._get({
            "action": "query", "meta": "siteinfo", "siprop": "general",
        }))["query"]["general"].get("generator", "unknown")

    async def edit_page(
        self,
        title: str,
        text: str,
        summary: str,
        *,
        bot: bool = True,
        createonly: bool = False,
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
        async def submit():
            data["token"] = self._csrf
            result = await self._post(data)
            if "error" in result:
                raise WikiError(f"edit {title!r} failed: {result['error']}")
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
            if "error" in result:
                error = result["error"]
                # MediaWiki reports an exact duplicate as an error-shaped
                # response even though the requested file is already present
                # and usable. Treat this as an idempotent success.
                if error.get("code") == "fileexists-no-change":
                    return {
                        "upload": {
                            "result": "Success",
                            "filename": filename,
                            "duplicate": True,
                        }
                    }
                raise WikiError(f"upload {filename!r} failed: {result['error']}")
            return result
        return (await self._retry_after_auth_error(submit))["upload"]

    async def get_page(self, title: str) -> Optional[dict]:
        """Return {pageid, revid, content} for the latest revision, or None."""
        result = await self._get({
            "action": "query",
            "prop": "revisions",
            "rvprop": "ids|content",
            "rvslots": "main",
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
        }

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
