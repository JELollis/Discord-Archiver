import asyncio
from pathlib import Path
import sys
import tempfile
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

from wiki_client import MediaWikiClient, UnexpectedResponseError, WikiError, load_config


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


class SiteInfoClient(MediaWikiClient):
    def __init__(self, maximum):
        super().__init__("https://wiki.invalid/api.php", "bot", "secret")
        self.logged_in = True
        self.maximum = maximum
        self.query_calls = 0

    async def _get(self, params):
        self.query_calls += 1
        return {"query": {"general": {"maxuploadsize": self.maximum}}}


class ChunkUploadClient(MediaWikiClient):
    def __init__(self, *, bad_intermediate_offset=False, stash_errors=None):
        super().__init__("https://wiki.invalid/api.php", "bot", "secret")
        self.logged_in = True
        self._csrf = "csrf"
        self.calls = []
        self.bad_intermediate_offset = bad_intermediate_offset
        self.stash_errors = stash_errors

    async def _post(self, data, *, files=None):
        self.calls.append({"data": dict(data), "files": files})
        if files:
            chunk = files["chunk"][1]
            offset = int(data["offset"])
            next_offset = offset + len(chunk)
            filesize = int(data["filesize"])
            upload = {"filekey": "stash-key"}
            if next_offset < filesize:
                upload["offset"] = (
                    next_offset + 1 if self.bad_intermediate_offset else next_offset
                )
                upload["result"] = "Continue"
            else:
                # Native MediaWiki final-stash responses omit offset.
                upload["imageinfo"] = {"size": filesize}
                if self.stash_errors:
                    upload["stasherrors"] = self.stash_errors
            return {"upload": upload}
        return {"upload": {"result": "Success", "filename": data["filename"]}}


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
    @staticmethod
    def _config_env(**overrides):
        env = {
            "MEDIAWIKI_API_URL": "https://wiki.invalid/api.php",
            "MEDIAWIKI_BOT_USERNAME": "bot",
            "MEDIAWIKI_BOT_PASSWORD": "secret",
            "MEDIAWIKI_ARCHIVE_NAMESPACE": "Archive",
        }
        env.update(overrides)
        return env

    def test_chunk_and_attachment_limits_have_bounded_defaults_and_overrides(self):
        config = load_config(self._config_env())
        self.assertEqual(config["MEDIAWIKI_UPLOAD_CHUNK_BYTES"], 20 * 1024 * 1024)
        self.assertEqual(config["MEDIAWIKI_MAX_ATTACHMENT_BYTES"], 500 * 1024 * 1024)

        config = load_config(self._config_env(
            MEDIAWIKI_UPLOAD_CHUNK_MIB="8",
            MEDIAWIKI_MAX_ATTACHMENT_MIB="256",
        ))
        self.assertEqual(config["MEDIAWIKI_UPLOAD_CHUNK_BYTES"], 8 * 1024 * 1024)
        self.assertEqual(config["MEDIAWIKI_MAX_ATTACHMENT_BYTES"], 256 * 1024 * 1024)

        with self.assertRaises(WikiError):
            load_config(self._config_env(MEDIAWIKI_UPLOAD_CHUNK_MIB="0"))

    def test_nas_sink_is_disabled_without_dir_and_url(self):
        config = load_config(self._config_env())
        self.assertFalse(config["ARCHIVE_NAS_ENABLED"])
        # A default cap is still provided so callers can rely on the key.
        self.assertEqual(config["ARCHIVE_NAS_MAX_BYTES"], 8192 * 1024 * 1024)

    def test_nas_sink_enables_with_dir_and_url_and_trims_slash(self):
        config = load_config(self._config_env(
            ARCHIVE_NAS_DIR="/mnt/ISO/software",
            ARCHIVE_NAS_URL_BASE="https://completeelectronics.net/software/",
            ARCHIVE_NAS_MAX_MIB="4096",
        ))
        self.assertTrue(config["ARCHIVE_NAS_ENABLED"])
        self.assertEqual(config["ARCHIVE_NAS_DIR"], "/mnt/ISO/software")
        self.assertEqual(config["ARCHIVE_NAS_URL_BASE"], "https://completeelectronics.net/software")
        self.assertEqual(config["ARCHIVE_NAS_MAX_BYTES"], 4096 * 1024 * 1024)

    def test_nas_max_must_be_positive_integer(self):
        with self.assertRaises(WikiError):
            load_config(self._config_env(ARCHIVE_NAS_MAX_MIB="0"))
        with self.assertRaises(WikiError):
            load_config(self._config_env(ARCHIVE_NAS_MAX_MIB="lots"))

    def test_wiki_max_upload_size_is_validated_and_cached(self):
        client = SiteInfoClient("524288000")
        self.assertEqual(asyncio.run(client.max_upload_size()), 524_288_000)
        self.assertEqual(asyncio.run(client.max_upload_size()), 524_288_000)
        self.assertEqual(client.query_calls, 1)

        with self.assertRaises(WikiError):
            asyncio.run(SiteInfoClient("invalid").max_upload_size())

    def test_large_path_upload_uses_native_stash_chunks_and_final_commit(self):
        client = ChunkUploadClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "large.bin"
            path.write_bytes(b"abcdefghij")
            result = asyncio.run(client.upload_file_from_path(
                "large.bin", path, "archive", chunk_size=4
            ))

        self.assertEqual(result["filename"], "large.bin")
        chunk_calls = [call for call in client.calls if call["files"]]
        self.assertEqual([call["data"]["offset"] for call in chunk_calls], ["0", "4", "8"])
        self.assertTrue(all(call["data"]["filesize"] == "10" for call in chunk_calls))
        self.assertNotIn("filekey", chunk_calls[0]["data"])
        self.assertEqual(chunk_calls[1]["data"]["filekey"], "stash-key")
        self.assertEqual([len(call["files"]["chunk"][1]) for call in chunk_calls], [4, 4, 2])
        final = client.calls[-1]
        self.assertIsNone(final["files"])
        self.assertEqual(final["data"]["filekey"], "stash-key")
        self.assertEqual(final["data"]["comment"], "archive")

    def test_chunk_upload_fails_closed_on_wrong_server_offset(self):
        client = ChunkUploadClient(bad_intermediate_offset=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "large.bin"
            path.write_bytes(b"abcdefghij")
            with self.assertRaises(WikiError) as raised:
                asyncio.run(client.upload_file_from_path(
                    "large.bin", path, chunk_size=4
                ))
        self.assertIn("expected 4", str(raised.exception))

    def test_chunk_stash_filetype_failure_remains_eligible_for_zip_fallback(self):
        client = ChunkUploadClient(stash_errors=[
            {"message": "filetype-banned", "params": ["py"]},
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "large.py"
            path.write_bytes(b"abcdefghij")
            with self.assertRaises(WikiError) as raised:
                asyncio.run(client.upload_file_from_path(
                    "large.py", path, chunk_size=4
                ))
        self.assertEqual(raised.exception.code, "stashfailed")
        self.assertTrue(raised.exception.is_file_type_rejection)

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

    def test_badmime_rejection_is_eligible_for_zip_fallback(self):
        # A .js served as text/html (or a .zip detected as application/java):
        # the detected MIME itself is disallowed, which a ZIP resolves.
        failure = ({
            "error": {
                "code": "verification-error",
                "info": 'Files of the MIME type "text/html" are not allowed to be uploaded.',
                "details": ["filetype-badmime", "text/html"],
            }
        }, None)
        client = RetryingClient([failure])
        with self.assertRaises(WikiError) as raised:
            asyncio.run(client.upload_file("project.js", b"<html></html>"))
        self.assertTrue(raised.exception.has_detail("filetype-badmime"))
        self.assertTrue(raised.exception.is_file_type_rejection)

    def test_illegal_filename_rejection_is_eligible_for_zip_fallback(self):
        # Seen on the chunked path for blocked extensions (.exe/.iso/.mkv); a ZIP
        # renames the payload to an accepted filename.
        failure = ({
            "error": {"code": "illegal-filename", "info": "The filename is not allowed."}
        }, None)
        client = RetryingClient([failure])
        with self.assertRaises(WikiError) as raised:
            asyncio.run(client.upload_file("installer.exe", b"MZ..."))
        self.assertEqual(raised.exception.code, "illegal-filename")
        self.assertTrue(raised.exception.is_file_type_rejection)

    def test_unrelated_error_is_not_treated_as_file_type_rejection(self):
        error = WikiError("boom", error={"code": "badtoken", "info": "Invalid CSRF token."})
        self.assertFalse(error.is_file_type_rejection)


if __name__ == "__main__":
    unittest.main()
