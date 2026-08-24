import asyncio
import sys
import types
import unittest


# The Windows development interpreter does not need aiohttp for these pure retry
# tests; production and the project environment provide the real package.
try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    aiohttp = types.ModuleType("aiohttp")

    class _ClientError(Exception):
        pass

    class _ClientResponseError(_ClientError):
        status = 0
        headers = None

    aiohttp.ClientConnectionError = _ClientError
    aiohttp.ClientPayloadError = _ClientError
    aiohttp.ServerTimeoutError = _ClientError
    aiohttp.ServerDisconnectedError = _ClientError
    aiohttp.ClientResponseError = _ClientResponseError
    aiohttp.ContentTypeError = _ClientResponseError
    sys.modules["aiohttp"] = aiohttp

from wiki_client import MediaWikiClient, UnexpectedResponseError, WikiError


class ExpiredReadClient(MediaWikiClient):
    def __init__(self):
        super().__init__("https://wiki.invalid/api.php", "bot", "secret")
        self.logged_in = True
        self.login_calls = 0
        self.query_calls = 0
        self.last_params = None

    async def login(self):
        self.login_calls += 1
        self.logged_in = True
        self._csrf = "fresh"
        return {"login": {"result": "Success"}}

    async def _get(self, params):
        self.query_calls += 1
        self.last_params = dict(params)
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


class PageInfoClient(MediaWikiClient):
    def __init__(self):
        super().__init__("https://wiki.invalid/api.php", "bot", "secret")
        self.logged_in = True
        self.params = None

    async def _get(self, params):
        self.params = dict(params)
        return {"query": {"pages": {"7": {
            "pageid": 7,
            "revisions": [
                {
                    "revid": 42,
                    "timestamp": "2026-08-24T18:00:00Z",
                    "user": "DiscordArchiveBot",
                    "comment": "Archive #test (3 messages)",
                    "slots": {"main": {"*": "latest"}},
                },
                {
                    "revid": 41,
                    "timestamp": "2026-08-24T17:00:00Z",
                    "user": "DiscordArchiveBot",
                    "comment": "older",
                    "slots": {"main": {"*": "older"}},
                },
            ],
        }}}}


class RetryingClient(MediaWikiClient):
    def __init__(self, outcomes):
        super().__init__("https://wiki.invalid/api.php", "bot", "secret")
        self.logged_in = True
        self._csrf = "csrf"
        self.outcomes = list(outcomes)
        self.calls = []
        self.delays = []
        self.discard_calls = 0

    async def _request_once(self, method, *, params=None, data=None, files=None):
        self.calls.append({
            "method": method,
            "params": params,
            "data": data,
            "files": files,
        })
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def _sleep_before_retry(self, delay):
        self.delays.append(delay)

    async def _discard_owned_session(self):
        self.discard_calls += 1


class WikiClientTests(unittest.TestCase):
    def test_private_read_reauthenticates_once_after_session_expiry(self):
        client = ExpiredReadClient()
        result = asyncio.run(client.userinfo())
        self.assertEqual(result["name"], "bot")
        self.assertEqual(client.login_calls, 1)
        self.assertEqual(client.query_calls, 2)
        self.assertEqual(client.last_params["uiprop"], "groups|rights|ratelimits")

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

    def test_get_page_includes_bounded_revision_provenance(self):
        client = PageInfoClient()
        result = asyncio.run(client.get_page("Archive:Test"))

        self.assertEqual(result, {
            "pageid": 7,
            "revid": 42,
            "content": "latest",
            "timestamp": "2026-08-24T18:00:00Z",
            "user": "DiscordArchiveBot",
            "comment": "Archive #test (3 messages)",
            "revision_count": 2,
        })
        self.assertEqual(client.params["rvlimit"], "2")
        self.assertEqual(client.params["rvprop"], "ids|timestamp|user|comment|content")

    def test_upload_retries_rate_limit_through_full_window(self):
        limited = ({
            "error": {
                "code": "ratelimited",
                "info": "Please wait and try again",
            }
        }, None)
        success = ({
            "upload": {"result": "Success", "filename": "archive.png"}
        }, None)
        client = RetryingClient([limited, limited, limited, success])

        result = asyncio.run(client.upload_file("archive.png", b"content"))

        self.assertEqual(result["filename"], "archive.png")
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(client.delays, [10.0, 20.0, 40.0])
        self.assertTrue(all(call["files"] for call in client.calls))

    def test_upload_stops_after_bounded_rate_limit_retries(self):
        limited = ({
            "error": {
                "code": "ratelimited",
                "info": "Please wait and try again",
            }
        }, None)
        client = RetryingClient([limited, limited, limited, limited])

        with self.assertRaises(WikiError) as raised:
            asyncio.run(client.upload_file("archive.png", b"content"))

        self.assertEqual(raised.exception.code, "ratelimited")
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(client.delays, [10.0, 20.0, 40.0])

    def test_edit_retries_transient_disconnect_with_fresh_request(self):
        disconnected = aiohttp.ServerDisconnectedError()
        success = ({"edit": {"result": "Success", "newrevid": 43}}, None)
        client = RetryingClient([disconnected, success])

        result = asyncio.run(client.edit_page("Archive:Test", "text", "summary"))

        self.assertEqual(result["newrevid"], 43)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.delays, [1.0])
        self.assertEqual(client.discard_calls, 1)

    def test_non_json_api_response_is_diagnostic_and_retryable(self):
        unexpected = UnexpectedResponseError(
            status=200,
            content_type="text/html; charset=UTF-8",
            server="Apache",
            preview="MediaWiki API help",
        )
        success = ({"upload": {"result": "Success", "filename": "archive.bin"}}, None)
        client = RetryingClient([unexpected, success])

        result = asyncio.run(client.upload_file("archive.bin", b"content"))

        self.assertEqual(result["filename"], "archive.bin")
        self.assertEqual(client.delays, [1.0])
        self.assertEqual(client.discard_calls, 1)
        self.assertIn("HTTP 200", str(unexpected))
        self.assertIn("text/html", str(unexpected))
        self.assertIn("MediaWiki API help", str(unexpected))

    def test_mime_mismatch_remains_structured_for_zip_fallback(self):
        failure = ({
            "error": {
                "code": "verification-error",
                "info": "extension does not match text/plain",
                "details": ["filetype-mime-mismatch", "sql", "text/plain"],
            }
        }, None)
        client = RetryingClient([failure])

        with self.assertRaises(WikiError) as raised:
            asyncio.run(client.upload_file("schema.sql", b"SELECT 1;"))

        self.assertEqual(raised.exception.code, "verification-error")
        self.assertTrue(raised.exception.has_detail("filetype-mime-mismatch"))
        self.assertTrue(raised.exception.is_file_type_rejection)


if __name__ == "__main__":
    unittest.main()
