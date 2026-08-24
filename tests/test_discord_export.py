import unittest

import discord_export


def message(message_id: int, content: str, *, stream_id: int = 10) -> dict:
    return {
        "id": message_id,
        "stream_id": stream_id,
        "author": "Display Name",
        "author_username": "username",
        "author_id": 123,
        "created": None,
        "edited": None,
        "content": content,
        "attachments": [],
        "reply_to": None,
    }


class DiscordExportTests(unittest.TestCase):
    def setUp(self):
        self.meta = {
            "channel_id": 10,
            "captured_at": "2026-08-24T00:00:00.000000Z",
            "captured_until": 99,
            "stream_boundaries": {10: 99, 20: 88},
            "message_count": 2,
            "complete": True,
        }

    def test_title_and_filename_limits_are_utf8_bytes(self):
        title = discord_export.sanitize_title_part("界" * 200)
        self.assertLessEqual(len(title.encode("utf-8")), 200)

        filename = discord_export.sanitize_filename(("😀" * 100) + ".png")
        self.assertLessEqual(len(filename.encode("utf-8")), 200)
        self.assertTrue(filename.endswith(".png"))

    def test_attachment_ids_survive_multibyte_name_truncation(self):
        channel = "界" * 100
        original = ("😀" * 100) + ".png"
        first = discord_export.attachment_upload_name(channel, 111, 222, original)
        second = discord_export.attachment_upload_name(channel, 333, 444, original)
        self.assertTrue(first.startswith("111-222-"))
        self.assertTrue(second.startswith("333-444-"))
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first.encode("utf-8")), 200)
        self.assertTrue(first.endswith(".png"))

    def test_part_manifest_is_contiguous_and_hashes_saved_content(self):
        manifest = [
            (1, discord_export.content_sha256("part one")),
            (2, discord_export.content_sha256("part two")),
        ]
        rendered = discord_export.render_part_manifest(manifest)
        self.assertEqual(discord_export.parse_part_manifest(rendered), manifest)
        self.assertIsNone(discord_export.parse_part_manifest(
            rendered.replace("archive_part=2", "archive_part=3")
        ))
        self.assertEqual(
            discord_export.parse_part_manifest("<!-- archive_part_count=0 -->"), []
        )

    def test_behavior_switches_are_neutralized(self):
        escaped = discord_export.escape_wikitext("__NOTOC__ __NOINDEX__")
        self.assertNotIn("__NOTOC__", escaped)
        self.assertNotIn("__NOINDEX__", escaped)
        self.assertIn("&#95;", escaped)

    def test_complete_page_has_owner_and_per_stream_boundaries(self):
        page = discord_export.render_page("test", [], self.meta)
        self.assertIn("<!-- archive_owner_channel_id=10 -->", page)
        self.assertIn("<!-- source_stream_id=10 captured_until=99 -->", page)
        self.assertIn("<!-- source_stream_id=20 captured_until=88 -->", page)
        self.assertIn("<!-- source_channel_id=10 ", page)

    def test_incomplete_page_keeps_owner_but_not_clearance(self):
        page = discord_export.render_page("test", [], {**self.meta, "complete": False})
        self.assertIn("<!-- archive_owner_channel_id=10 -->", page)
        self.assertNotIn("<!-- source_stream_id=", page)
        self.assertNotIn("<!-- source_channel_id=", page)
        self.assertIn("deletion is blocked", page)

    def test_archive_part_is_not_labeled_incomplete(self):
        page = discord_export.render_page(
            "test", [message(1, "hello")], {**self.meta, "complete": False}, part=True
        )
        self.assertIn("deletion clearance is recorded on the canonical index page", page)
        self.assertNotIn("INCOMPLETE", page)

    def test_split_pages_stay_under_the_byte_limit(self):
        messages = [message(i, "short message") for i in range(1, 101)]
        limit = 1_200
        chunks = discord_export.split_messages(messages, "test", self.meta, max_bytes=limit)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            rendered = discord_export.render_page(
                "test", chunk, {**self.meta, "complete": False}, part=True
            )
            self.assertLessEqual(len(rendered.encode("utf-8")), limit)

    def test_split_inside_thread_repeats_thread_context(self):
        messages = [
            {"thread_header": "answers", "id": 20, "attachments": [], "content": "", "reply_to": None},
            *[message(i, "x" * 250, stream_id=20) for i in range(21, 31)],
        ]
        chunks = discord_export.split_messages(messages, "test", self.meta, max_bytes=1_200)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0][0]["thread_header"], "answers")
        for chunk in chunks[1:]:
            self.assertEqual(chunk[0]["thread_header"], "answers (continued)")


if __name__ == "__main__":
    unittest.main()
