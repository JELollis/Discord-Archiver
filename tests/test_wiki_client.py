import asyncio
import sys
import types
import unittest


# The Windows development interpreter does not need aiohttp for these pure retry
# tests; production and the project environment provide the real package.
try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")

from wiki_client import MediaWikiClient


class ExpiredReadClient(MediaWikiClient):
    def __init__(self):
        super().__init__("https://wiki.invalid/api.php", "bot", "secret")
        self.logged_in = True
        self.login_calls = 0
        self.query_calls = 0

    async def login(self):
        self.login_calls += 1
        self.logged_in = True
        self._csrf = "fresh"
        return {"login": {"result": "Success"}}

    async def _get(self, params):
        self.query_calls += 1
        if self.query_calls == 1:
            return {"error": {"code": "readapidenied", "info": "session expired"}}
        return {"query": {"userinfo": {"name": "bot"}}}


class WikiClientTests(unittest.TestCase):
    def test_private_read_reauthenticates_once_after_session_expiry(self):
        client = ExpiredReadClient()
        result = asyncio.run(client.userinfo())
        self.assertEqual(result["name"], "bot")
        self.assertEqual(client.login_calls, 1)
        self.assertEqual(client.query_calls, 2)


if __name__ == "__main__":
    unittest.main()
