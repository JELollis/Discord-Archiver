import tempfile
from pathlib import Path
import unittest
import zipfile

from attachment_archive import (
    AttachmentArchiveError,
    AttachmentTooLargeError,
    create_zip_file,
    file_sha1_and_size,
    select_reusable_upload,
    stage_discord_attachment,
)


class SelectReusableUploadTests(unittest.TestCase):
    def test_direct_name_is_preferred_when_present(self):
        manifest = {"123-9-chan-file.png": ("abc123", 42)}
        self.assertEqual(
            select_reusable_upload("123-9-chan-file.png", "123-9-chan-file_png.zip", manifest),
            ("123-9-chan-file.png", False, "abc123", 42),
        )

    def test_zip_fallback_name_is_matched_and_flagged_zipped(self):
        manifest = {"123-9-chan-file_py.zip": ("def456", 99)}
        self.assertEqual(
            select_reusable_upload("123-9-chan-file.py", "123-9-chan-file_py.zip", manifest),
            ("123-9-chan-file_py.zip", True, "def456", 99),
        )

    def test_no_candidate_present_returns_none(self):
        self.assertIsNone(
            select_reusable_upload("a.png", "a_png.zip", {"unrelated.png": ("x", 1)})
        )
        self.assertIsNone(select_reusable_upload("a.png", "a_png.zip", {}))


class FakeAttachment:
    def __init__(self, data: bytes, url: str = "https://cdn.invalid/file"):
        self.data = data
        self.url = url
        self.read_calls = 0

    async def read(self):
        self.read_calls += 1
        return self.data


class FakeContent:
    def __init__(self, blocks):
        self.blocks = list(blocks)

    async def iter_chunked(self, _size):
        for block in self.blocks:
            yield block


class FakeResponse:
    def __init__(self, blocks):
        self.content = FakeContent(blocks)
        self.raise_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        self.raise_calls += 1


class FakeSession:
    def __init__(self, blocks, **_kwargs):
        self.response = FakeResponse(blocks)
        self.requested_url = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url):
        self.requested_url = url
        return self.response


class AttachmentArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_attachment_uses_discord_authenticated_read(self):
        attachment = FakeAttachment(b"small")
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "attachment.bin"
            size = await stage_discord_attachment(
                attachment,
                destination,
                expected_size=5,
                maximum_size=10,
                memory_threshold=5,
            )

            self.assertEqual(size, 5)
            self.assertEqual(destination.read_bytes(), b"small")
            self.assertEqual(attachment.read_calls, 1)

    async def test_large_attachment_streams_from_cdn_in_blocks(self):
        attachment = FakeAttachment(b"must not be buffered")
        sessions = []

        def session_factory(**kwargs):
            session = FakeSession([b"ab", b"cd", b"ef"], **kwargs)
            sessions.append(session)
            return session

        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "attachment.bin"
            size = await stage_discord_attachment(
                attachment,
                destination,
                expected_size=6,
                maximum_size=10,
                memory_threshold=2,
                session_factory=session_factory,
            )

            self.assertEqual(size, 6)
            self.assertEqual(destination.read_bytes(), b"abcdef")
            self.assertEqual(attachment.read_calls, 0)
            self.assertEqual(sessions[0].requested_url, attachment.url)
            self.assertEqual(sessions[0].response.raise_calls, 1)

    async def test_declared_oversize_attachment_is_rejected_before_download(self):
        attachment = FakeAttachment(b"unused")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(AttachmentTooLargeError):
                await stage_discord_attachment(
                    attachment,
                    Path(temp_dir) / "attachment.bin",
                    expected_size=11,
                    maximum_size=10,
                    memory_threshold=5,
                )
        self.assertEqual(attachment.read_calls, 0)

    async def test_changed_attachment_size_is_rejected(self):
        attachment = FakeAttachment(b"different")
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(AttachmentArchiveError):
                await stage_discord_attachment(
                    attachment,
                    Path(temp_dir) / "attachment.bin",
                    expected_size=8,
                    maximum_size=20,
                    memory_threshold=20,
                )

    async def test_zip_preserves_safe_leaf_name_and_has_verifiable_digest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.bin"
            destination = Path(temp_dir) / "attachment.zip"
            source.write_bytes(b"print('archived')")

            create_zip_file(source, destination, "../../Lab8\\Selenium.py")
            digest, size = file_sha1_and_size(destination)

            self.assertEqual(len(digest), 40)
            self.assertEqual(size, destination.stat().st_size)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(archive.namelist(), ["Selenium.py"])
                self.assertEqual(archive.read("Selenium.py"), b"print('archived')")


if __name__ == "__main__":
    unittest.main()
