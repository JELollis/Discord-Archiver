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

    def test_zip_fallback_removes_compound_extensions_from_upload_name(self):
        upload_name = (
            "1211726263379689482-1211726262968655902-"
            "cpt-187-spring-2024-Lab8-4_Selenium.py"
        )
        zip_name = discord_export.zip_fallback_upload_name(upload_name)

        self.assertTrue(zip_name.endswith("-Lab8-4_Selenium_py.zip"))
        self.assertEqual(zip_name.count("."), 1)
        self.assertLessEqual(len(zip_name.encode("utf-8")), 200)

    def test_zip_fallback_keeps_ids_when_long_names_are_truncated(self):
        upload_name = discord_export.attachment_upload_name(
            "界" * 100,
            111,
            222,
            ("😀" * 100) + ".tar.py",
        )
        zip_name = discord_export.zip_fallback_upload_name(upload_name)

        self.assertTrue(zip_name.startswith("111-222-"))
        self.assertTrue(zip_name.endswith(".zip"))
        self.assertEqual(zip_name.count("."), 1)
        self.assertLessEqual(len(zip_name.encode("utf-8")), 200)

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

    def test_canonical_seal_detects_content_edits(self):
        sealed = discord_export.seal_canonical_content("archived message\nfooter")
        self.assertTrue(discord_export.verify_canonical_content(sealed))
        self.assertFalse(discord_export.verify_canonical_content(
            sealed.replace("archived message", "removed message")
        ))
        self.assertFalse(discord_export.verify_canonical_content(
            sealed + "unexpected suffix"
        ))

    def test_attachment_manifest_is_complete_unique_and_byte_exact(self):
        manifest = [
            ("111-222-界.png", "a" * 40, 123),
            ("333-444-report.zip", "b" * 40, 456),
        ]
        rendered = discord_export.render_attachment_manifest(manifest)
        self.assertEqual(discord_export.parse_attachment_manifest(rendered), sorted(manifest))
        self.assertIsNone(discord_export.parse_attachment_manifest(
            rendered.replace("archive_attachment_count=2", "archive_attachment_count=3")
        ))

    def test_external_manifest_is_complete_unique_and_byte_exact(self):
        manifest = [
            ("111-222-cpt-winPreVista.iso", "c" * 40, 3_221_225_472),
            ("333-444-ist-lecture.mkv", "d" * 40, 512_000_000),
        ]
        rendered = discord_export.render_external_manifest(manifest)
        self.assertEqual(discord_export.parse_external_manifest(rendered), sorted(manifest))
        self.assertIsNone(discord_export.parse_external_manifest(
            rendered.replace("archive_external_count=2", "archive_external_count=3")
        ))
        # Empty manifest round-trips to a zero count (new pages always carry it).
        self.assertEqual(discord_export.parse_external_manifest(
            discord_export.render_external_manifest([])
        ), [])

    def test_nas_attachment_renders_as_external_link(self):
        wikitext = discord_export._render_attachment({
            "filename": "winPreVista.iso",
            "nas_url": "https://completeelectronics.net/software/111-222-winPreVista.iso",
            "nas_size": 3_221_225_472,
        })
        self.assertIn("[https://completeelectronics.net/software/111-222-winPreVista.iso winPreVista.iso]", wikitext)
        self.assertIn("external", wikitext)
        self.assertNotIn("[[File:", wikitext)
        self.assertNotIn("[[Media:", wikitext)

    def test_thread_manifest_detects_renames_by_id(self):
        rendered = discord_export.render_thread_manifest({20: "answers", 30: "界-thread"})
        parsed = discord_export.parse_thread_manifest(rendered)
        self.assertEqual(parsed[20], discord_export.content_sha256("answers"))
        self.assertNotEqual(parsed[20], discord_export.content_sha256("renamed"))
        self.assertEqual(set(parsed), {20, 30})

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

    def test_verified_legacy_incomplete_page_can_be_adopted(self):
        channel_name = "cpt-267-summer-2023"
        content = (
            "''Automated archive of Discord channel'' "
            "'''#cpt-267-summer-2023''' ''from the GTC Tech Student server.''\n"
            "archive body\n"
            "<!-- INCOMPLETE: attachment or capture errors; deletion is blocked. -->"
        )
        page = {
            "revision_count": 1,
            "user": "DiscordArchiveBot",
            "comment": "Archive #cpt-267-summer-2023 (800 messages)",
            "content": content,
        }

        self.assertTrue(discord_export.is_adoptable_legacy_incomplete_page(
            page, channel_name, "DiscordArchiveBot",
        ))
        self.assertFalse(discord_export.is_adoptable_legacy_incomplete_page(
            {**page, "revision_count": 2}, channel_name, "DiscordArchiveBot",
        ))
        self.assertFalse(discord_export.is_adoptable_legacy_incomplete_page(
            {**page, "user": "Jonathan"}, channel_name, "DiscordArchiveBot",
        ))
        self.assertFalse(discord_export.is_adoptable_legacy_incomplete_page(
            {**page, "comment": "manual edit"}, channel_name, "DiscordArchiveBot",
        ))
        self.assertFalse(discord_export.is_adoptable_legacy_incomplete_page(
            {**page, "content": content + "\n<!-- source_channel_id=123 captured_at=x captured_until=1 -->"},
            channel_name,
            "DiscordArchiveBot",
        ))

    def test_verified_legacy_part_requires_matching_part_summary(self):
        channel_name = "ist-272-summer-2023"
        page = {
            "revision_count": 1,
            "user": "DiscordArchiveBot",
            "comment": "Archive part 2 for #ist-272-summer-2023",
            "content": (
                "''Automated archive of Discord channel'' "
                "'''#ist-272-summer-2023''' ''from the GTC Tech Student server.''\n"
                "<!-- INCOMPLETE: attachment or capture errors; deletion is blocked. -->"
            ),
        }
        self.assertTrue(discord_export.is_adoptable_legacy_incomplete_page(
            page, channel_name, "DiscordArchiveBot", part_number=2,
        ))
        self.assertFalse(discord_export.is_adoptable_legacy_incomplete_page(
            page, channel_name, "DiscordArchiveBot", part_number=1,
        ))

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

    def test_cross_part_reply_urls_are_included_in_size_accounting(self):
        title = "Archive:Misc/" + ("long-title-" * 12)
        messages = [message(1, "x" * 420)]
        for message_id in range(2, 20):
            reply = message(message_id, "y" * 170)
            reply["reply_to"] = 1
            messages.append(reply)
        limit = 1_250
        chunks = discord_export.split_messages(
            messages, "test", self.meta, max_bytes=limit, page_title=title,
        )
        self.assertGreater(len(chunks), 1)
        part_titles = [f"{title}/Part {index}" for index in range(1, len(chunks) + 1)]
        anchor_index = {
            item["id"]: part_titles[index]
            for index, chunk in enumerate(chunks)
            for item in chunk
            if not item.get("thread_header")
        }
        for index, chunk in enumerate(chunks):
            rendered = discord_export.render_page(
                "test", chunk, {**self.meta, "complete": False}, part=True,
                anchor_index=anchor_index, self_title=part_titles[index],
            )
            self.assertLessEqual(len(rendered.encode("utf-8")), limit)

    def test_single_message_that_cannot_fit_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            discord_export.split_messages(
                [message(1, "x" * 2_000)], "test", self.meta,
                max_bytes=1_000, page_title="Archive:Misc/test",
            )


class ParseNameListTests(unittest.TestCase):
    def test_trims_casefolds_and_preserves_input_order(self):
        self.assertEqual(
            discord_export.parse_name_list("  Summer 2024 Archive , Fall 2024 Archive "),
            ["summer 2024 archive", "fall 2024 archive"],
        )

    def test_deduplicates_case_insensitively_keeping_first_position(self):
        self.assertEqual(
            discord_export.parse_name_list("Fall 2024, spring 2025, FALL 2024, Spring 2025"),
            ["fall 2024", "spring 2025"],
        )

    def test_drops_empty_entries_and_leading_hash(self):
        self.assertEqual(
            discord_export.parse_name_list(",  , #cpt-257 , ,#IST-201,"),
            ["cpt-257", "ist-201"],
        )

    def test_none_and_blank_yield_empty_list(self):
        self.assertEqual(discord_export.parse_name_list(None), [])
        self.assertEqual(discord_export.parse_name_list("   , ,"), [])


if __name__ == "__main__":
    unittest.main()
