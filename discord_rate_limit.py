"""Conservative Discord API pacing helpers for destructive archive cleanup."""

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar


MIN_DISCORD_ACTION_INTERVAL_SECONDS = 5.0
DISCORD_RETRY_BUFFER_SECONDS = 1.0
_T = TypeVar("_T")


def additional_retry_delay(
    retry_after: float,
    buffer_seconds: float = DISCORD_RETRY_BUFFER_SECONDS,
) -> float:
    """Extra delay needed to make the total wait ``ceil(retry_after) + buffer``."""
    retry_after = max(0.0, float(retry_after))
    target_wait = math.ceil(retry_after) + max(0.0, float(buffer_seconds))
    return target_wait - retry_after


async def _enforce_buffered_429_wait(_session, _context, params) -> None:
    """Round Discord's JSON retry delay up and add a one-second buffer.

    discord.py subsequently sleeps for the unrounded ``retry_after`` value from
    the same response JSON. Sleeping only the difference here makes the combined
    cooldown ``ceil(retry_after) + 1`` without doubling the server-directed wait.
    """
    response = getattr(params, "response", None)
    if response is None or getattr(response, "status", None) != 429:
        return

    raw_retry_after = None
    try:
        data = await response.json(content_type=None)
        logging.warning(
            "Discord HTTP 429 response JSON: %s",
            json.dumps(data, ensure_ascii=False, sort_keys=True, default=str),
        )
        if isinstance(data, dict):
            raw_retry_after = data.get("retry_after")
    except Exception:
        logging.warning("Could not parse Discord's HTTP 429 JSON response; using Retry-After header.")

    if raw_retry_after is None:
        raw_retry_after = response.headers.get("Retry-After")

    try:
        retry_after = max(0.0, float(raw_retry_after))
        extra_delay = additional_retry_delay(retry_after)
    except (TypeError, ValueError):
        # Discord documents retry_after in every 429 JSON response. If both the
        # JSON and fallback header are malformed, add the one-second safety
        # buffer while discord.py handles (or rejects) the response itself.
        retry_after = 0.0
        extra_delay = DISCORD_RETRY_BUFFER_SECONDS

    if extra_delay > 0:
        logging.warning(
            "Discord returned HTTP 429 (retry_after=%.2f); adding %.2f seconds for a %.2f-second total cooldown.",
            retry_after,
            extra_delay,
            retry_after + extra_delay,
        )
        await asyncio.sleep(extra_delay)


def build_discord_http_trace():
    """Return an aiohttp trace that adds a rounded buffer to 429 cooldowns."""
    import aiohttp

    trace = aiohttp.TraceConfig()
    trace.on_request_end.append(_enforce_buffered_429_wait)
    return trace


class AsyncActionPacer:
    """Serialize actions and leave a minimum interval after every attempt."""

    def __init__(
        self,
        minimum_interval: float = MIN_DISCORD_ACTION_INTERVAL_SECONDS,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.minimum_interval = max(0.0, float(minimum_interval))
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or asyncio.sleep
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0

    async def run(self, label: str, operation: Callable[[], Awaitable[_T]]) -> _T:
        """Run one operation after the shared cooldown, even after failures."""
        async with self._lock:
            delay = max(0.0, self._next_allowed_at - self._clock())
            if delay > 0:
                logging.info("Discord API pacing: waiting %.2f seconds before %s.", delay, label)
                await self._sleeper(delay)
            try:
                return await operation()
            finally:
                self._next_allowed_at = self._clock() + self.minimum_interval
