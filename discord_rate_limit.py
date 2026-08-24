"""Conservative Discord API pacing helpers for destructive archive cleanup."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar


MIN_DISCORD_ACTION_INTERVAL_SECONDS = 5.0
_T = TypeVar("_T")


def additional_retry_delay(
    retry_after: float,
    minimum: float = MIN_DISCORD_ACTION_INTERVAL_SECONDS,
) -> float:
    """Extra delay needed so Discord's own retry wait totals at least ``minimum``."""
    return max(0.0, minimum - max(0.0, retry_after))


async def _enforce_minimum_429_wait(_session, _context, params) -> None:
    """Add a floor to discord.py's header-aware 429 retry delay.

    discord.py subsequently sleeps for the full Retry-After value parsed from
    the response JSON. Sleeping only the difference here preserves longer
    server-directed waits while turning a 0.8-second retry into a five-second
    total cooldown.
    """
    response = getattr(params, "response", None)
    if response is None or getattr(response, "status", None) != 429:
        return

    raw_retry_after = response.headers.get("Retry-After")
    if raw_retry_after is None:
        # Discord documents Retry-After on 429 responses. If it is unexpectedly
        # absent, add the complete conservative floor before discord.py handles
        # the response body.
        extra_delay = MIN_DISCORD_ACTION_INTERVAL_SECONDS
    else:
        try:
            extra_delay = additional_retry_delay(float(raw_retry_after))
        except (TypeError, ValueError):
            extra_delay = MIN_DISCORD_ACTION_INTERVAL_SECONDS

    if extra_delay > 0:
        logging.warning(
            "Discord returned HTTP 429; adding %.2f seconds so the retry cooldown is at least %.2f seconds.",
            extra_delay,
            MIN_DISCORD_ACTION_INTERVAL_SECONDS,
        )
        await asyncio.sleep(extra_delay)


def build_discord_http_trace():
    """Return an aiohttp trace that enforces the minimum 429 cooldown."""
    import aiohttp

    trace = aiohttp.TraceConfig()
    trace.on_request_end.append(_enforce_minimum_429_wait)
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
