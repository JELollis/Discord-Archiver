import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from discord_rate_limit import (
    AsyncActionPacer,
    _enforce_minimum_429_wait,
    additional_retry_delay,
)


class DiscordRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_retry_delay_has_five_second_floor(self):
        self.assertAlmostEqual(additional_retry_delay(0.8), 4.2)
        self.assertEqual(additional_retry_delay(5.0), 0.0)
        self.assertEqual(additional_retry_delay(64.57), 0.0)

    async def test_429_trace_adds_only_the_missing_floor_delay(self):
        params = SimpleNamespace(
            response=SimpleNamespace(status=429, headers={"Retry-After": "0.8"})
        )
        with patch("discord_rate_limit.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await _enforce_minimum_429_wait(None, None, params)
        sleep.assert_awaited_once()
        self.assertAlmostEqual(sleep.await_args.args[0], 4.2)

    async def test_429_trace_preserves_longer_server_delay(self):
        params = SimpleNamespace(
            response=SimpleNamespace(status=429, headers={"Retry-After": "64.57"})
        )
        with patch("discord_rate_limit.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await _enforce_minimum_429_wait(None, None, params)
        sleep.assert_not_awaited()

    async def test_actions_are_spaced_five_seconds_apart(self):
        now = [100.0]
        sleeps = []
        calls = []

        async def sleeper(delay):
            sleeps.append(delay)
            now[0] += delay

        async def operation():
            calls.append(now[0])
            return len(calls)

        pacer = AsyncActionPacer(5.0, clock=lambda: now[0], sleeper=sleeper)
        self.assertEqual(await pacer.run("first action", operation), 1)
        self.assertEqual(await pacer.run("second action", operation), 2)

        self.assertEqual(calls, [100.0, 105.0])
        self.assertEqual(sleeps, [5.0])

    async def test_failed_attempt_still_starts_cooldown(self):
        now = [200.0]
        sleeps = []

        async def sleeper(delay):
            sleeps.append(delay)
            now[0] += delay

        async def fail():
            raise RuntimeError("request failed")

        async def succeed():
            return "ok"

        pacer = AsyncActionPacer(5.0, clock=lambda: now[0], sleeper=sleeper)
        with self.assertRaises(RuntimeError):
            await pacer.run("failed action", fail)
        self.assertEqual(await pacer.run("retry", succeed), "ok")
        self.assertEqual(sleeps, [5.0])


if __name__ == "__main__":
    unittest.main()
