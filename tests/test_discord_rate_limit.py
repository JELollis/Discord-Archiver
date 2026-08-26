import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from discord_rate_limit import (
    AsyncActionPacer,
    _enforce_buffered_429_wait,
    additional_retry_delay,
)


class DiscordRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_retry_delay_rounds_up_and_adds_one_second(self):
        self.assertAlmostEqual(additional_retry_delay(0.8), 1.2)
        self.assertEqual(additional_retry_delay(5.0), 1.0)
        self.assertAlmostEqual(additional_retry_delay(64.57), 1.43)

    async def test_429_trace_uses_json_and_adds_only_the_rounding_buffer(self):
        params = SimpleNamespace(
            response=SimpleNamespace(
                status=429,
                headers={"Retry-After": "20.0"},
                json=AsyncMock(return_value={"retry_after": 0.8}),
            )
        )
        with patch("discord_rate_limit.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await _enforce_buffered_429_wait(None, None, params)
        sleep.assert_awaited_once()
        self.assertAlmostEqual(sleep.await_args.args[0], 1.2)

    async def test_429_trace_rounds_up_longer_server_delay(self):
        params = SimpleNamespace(
            response=SimpleNamespace(
                status=429,
                headers={},
                json=AsyncMock(return_value={"retry_after": 64.57}),
            )
        )
        with patch("discord_rate_limit.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await _enforce_buffered_429_wait(None, None, params)
        sleep.assert_awaited_once()
        self.assertAlmostEqual(sleep.await_args.args[0], 1.43)

    async def test_429_trace_falls_back_to_retry_after_header(self):
        params = SimpleNamespace(
            response=SimpleNamespace(
                status=429,
                headers={"Retry-After": "2.25"},
                json=AsyncMock(side_effect=ValueError("bad JSON")),
            )
        )
        with patch("discord_rate_limit.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await _enforce_buffered_429_wait(None, None, params)
        self.assertAlmostEqual(sleep.await_args.args[0], 1.75)

    async def test_429_trace_logs_full_json_response(self):
        body = {
            "message": "You are being rate limited.",
            "retry_after": 3.4,
            "global": False,
        }
        params = SimpleNamespace(
            response=SimpleNamespace(
                status=429,
                headers={},
                json=AsyncMock(return_value=body),
            )
        )
        with (
            self.assertLogs(level="WARNING") as logs,
            patch("discord_rate_limit.asyncio.sleep", new_callable=AsyncMock),
        ):
            await _enforce_buffered_429_wait(None, None, params)

        output = "\n".join(logs.output)
        self.assertIn('"global": false', output)
        self.assertIn('"message": "You are being rate limited."', output)
        self.assertIn('"retry_after": 3.4', output)

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
