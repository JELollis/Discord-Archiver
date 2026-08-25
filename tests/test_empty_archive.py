import asyncio
import sys
import types
import unittest


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

import empty_archive
from wiki_client import WikiError


def record(channel_id=20, *, name="cpt-239-fall-2023", category="Fall 2023 Archive"):
    return empty_archive.make_record(
        guild_id=10,
        channel_id=channel_id,
        channel_name=name,
        category_name=category,
        verified_at="2026-08-24T19:17:00.000000Z",
    )


class Stream:
    def __init__(self, messages=()):
        self.messages = list(messages)

    def history(self, **_kwargs):
        async def iterator():
            for message in self.messages:
                yield message
        return iterator()


async def iter_threads(_channel, threads=(), error=None):
    if error:
        raise error
    for thread in threads:
        yield thread


class RegistryClient:
    def __init__(self, page=None, *, conflicts=0):
        self.page = page
        self.conflicts = conflicts
        self.edits = []
        self.next_revid = 1 if page is None else page["revid"] + 1

    async def get_page(self, _title):
        return self.page

    async def edit_page(self, title, content, summary, **kwargs):
        self.edits.append({
            "title": title,
            "content": content,
            "summary": summary,
            **kwargs,
        })
        if self.conflicts:
            self.conflicts -= 1
            raise WikiError("conflict", error={"code": "editconflict"})
        self.page = {"revid": self.next_revid, "content": content}
        self.next_revid += 1
        return {"result": "Success", "newrevid": self.page["revid"]}


class EmptyArchiveTests(unittest.TestCase):
    def test_registry_round_trip_is_deterministic_and_escaped(self):
        records = {
            30: record(30, name="ist-272-<test>", category="Fall | Archive"),
            20: record(20),
        }
        content = empty_archive.render_empty_channel_list(records)

        self.assertEqual(empty_archive.parse_empty_channel_list(content), records)
        self.assertIn('id="empty-channel-20"', content)
        self.assertIn("&lt;test&gt;", content)
        self.assertIn("&#124;", content)
        self.assertLess(content.index("channel_id=20"), content.index("channel_id=30"))

    def test_registry_rejects_tampering_duplicates_and_noncanonical_encoding(self):
        content = empty_archive.render_empty_channel_list({20: record(20)})
        self.assertIsNone(empty_archive.parse_empty_channel_list(
            content.replace("cpt-239", "cpt-999", 1)
        ))

        duplicate = content.replace(
            "<!-- empty_channel_count=1 -->",
            "<!-- empty_channel_count=2 -->",
        ).replace(
            "<!-- archive_canonical_sha256=",
            "<!-- empty_channel guild_id=10 channel_id=20 "
            "verified_at=2026-08-24T19:17:00.000000Z "
            "channel_name=cpt-239-fall-2023 category_name=Fall%202023%20Archive -->\n"
            "<!-- archive_canonical_sha256=",
        )
        # Reseal the deliberately duplicated machine content so duplicate-ID
        # validation, rather than only the outer integrity seal, is exercised.
        duplicate = duplicate.rsplit("<!-- archive_canonical_sha256=", 1)[0].rstrip()
        duplicate = empty_archive.discord_export.seal_canonical_content(duplicate)
        self.assertIsNone(empty_archive.parse_empty_channel_list(duplicate))

        noncanonical = content.rsplit("<!-- archive_canonical_sha256=", 1)[0]
        noncanonical = noncanonical.replace("Fall%202023", "Fall%20%32%30%32%33")
        noncanonical = empty_archive.discord_export.seal_canonical_content(noncanonical.rstrip())
        self.assertIsNone(empty_archive.parse_empty_channel_list(noncanonical))

    def test_upsert_retries_conflict_and_preserves_existing_records(self):
        original = empty_archive.render_empty_channel_list({20: record(20)})
        client = RegistryClient({"revid": 7, "content": original}, conflicts=1)

        saved = asyncio.run(empty_archive.upsert_record(
            client, "Archive", record(30, name="ist-272-fall-2023"),
        ))

        parsed = empty_archive.parse_empty_channel_list(saved["content"])
        self.assertEqual(set(parsed), {20, 30})
        self.assertEqual(len(client.edits), 2)
        self.assertEqual(client.edits[-1]["baserevid"], 7)
        self.assertFalse(client.edits[-1]["createonly"])

    def test_upsert_creates_missing_registry(self):
        client = RegistryClient()
        saved = asyncio.run(empty_archive.upsert_record(
            client, "Archive", record(20),
        ))
        self.assertEqual(
            empty_archive.parse_empty_channel_list(saved["content"])[20],
            record(20),
        )
        self.assertTrue(client.edits[0]["createonly"])
        self.assertIsNone(client.edits[0]["baserevid"])

    def test_malformed_existing_registry_fails_closed(self):
        client = RegistryClient({"revid": 7, "content": "manual page"})
        with self.assertRaisesRegex(WikiError, "malformed"):
            asyncio.run(empty_archive.upsert_record(
                client, "Archive", record(20),
            ))
        self.assertEqual(client.edits, [])

    def test_empty_check_includes_parent_and_all_threads(self):
        channel = Stream()
        self.assertTrue(asyncio.run(empty_archive.is_completely_empty(
            channel, lambda value: iter_threads(value, [Stream(), Stream()]),
        )))
        self.assertFalse(asyncio.run(empty_archive.is_completely_empty(
            Stream([object()]), lambda value: iter_threads(value),
        )))
        self.assertFalse(asyncio.run(empty_archive.is_completely_empty(
            channel, lambda value: iter_threads(value, [Stream([object()])]),
        )))

    def test_empty_check_propagates_thread_enumeration_failure(self):
        with self.assertRaisesRegex(RuntimeError, "threads unavailable"):
            asyncio.run(empty_archive.is_completely_empty(
                Stream(),
                lambda value: iter_threads(
                    value, error=RuntimeError("threads unavailable"),
                ),
            ))


if __name__ == "__main__":
    unittest.main()
