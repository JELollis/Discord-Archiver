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


class CapturingEditClient(MediaWikiClient):
    def __init__(self):
        super().__init__("https://wiki.invalid/api.php", "bot", "secret")
        self.logged_in = True
        self._csrf = "csrf"
        self.submitted = None

    async def _post(self, data, *, files=None):
        self.submitted = dict(data)
        return {"edit": {"result": "Success", "newrevid": 43}}


class FileInfoClient(MediaWikiClient):
    def __init__(self, *, missing=False):
        super().__init__("https://wiki.invalid/api.php", "bot", "secret")
        self.logged_in = True
        self.missing = missing
        self.params = None

    async def _get(self, params):
        self.params = dict(params)
        if self.missing:
            return {"query": {"pages": {"-1": {"missing": ""}}}}
        return {"query": {"pages": {"7": {"imageinfo": [{
            "sha1": "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
            "size": 987,
        }]}}}}


class WikiClientTests(unittest.TestCase):
    def test_private_read_reauthenticates_once_after_session_expiry(self):
        client = ExpiredReadClient()
        result = asyncio.run(client.userinfo())
        self.assertEqual(result["name"], "bot")
        self.assertEqual(client.login_calls, 1)
        self.assertEqual(client.query_calls, 2)

    def test_edit_can_atomically_guard_existing_or_new_pages(self):
        client = CapturingEditClient()
        asyncio.run(client.edit_page(
            "Archive:Test", "content", "summary", baserevid=42
        ))
        self.assertEqual(client.submitted["baserevid"], "42")
        self.assertNotIn("createonly", client.submitted)

        asyncio.run(client.edit_page(
            "Archive:New", "content", "summary", createonly=True
        ))
        self.assertEqual(client.submitted["createonly"], "1")
        self.assertNotIn("baserevid", client.submitted)

    def test_file_info_returns_current_digest_and_size(self):
        client = FileInfoClient()
        result = asyncio.run(client.get_file_info("archive.png"))
        self.assertEqual(result, {
            "sha1": "abcdef0123456789abcdef0123456789abcdef01",
            "size": 987,
        })
        self.assertEqual(client.params["titles"], "File:archive.png")
        self.assertEqual(client.params["iiprop"], "sha1|size")
        self.assertIsNone(asyncio.run(FileInfoClient(missing=True).get_file_info("gone.png")))


if __name__ == "__main__":
    unittest.main()
