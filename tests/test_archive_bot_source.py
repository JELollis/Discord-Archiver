import ast
from pathlib import Path
import unittest


ARCHIVE_BOT_PATH = Path(__file__).resolve().parents[1] / "Archive_Bot.py"


class ArchiveBotSourceTests(unittest.TestCase):
    @staticmethod
    def _async_function(tree, name):
        return next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        )

    def test_extend_is_not_given_an_async_generator(self):
        """Reject the runtime-invalid ``items.extend(x for x if await ...)`` pattern."""
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        offenders = []

        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "extend"
            ):
                continue
            for argument in node.args:
                if isinstance(argument, ast.GeneratorExp) and any(
                    isinstance(child, ast.Await) for child in ast.walk(argument)
                ):
                    offenders.append(node.lineno)

        self.assertEqual(
            offenders,
            [],
            f"list.extend() cannot consume async generators (lines: {offenders})",
        )

    def test_delete_confirmation_is_claimed_before_first_await(self):
        """Repeat button clicks must not start concurrent destructive callbacks."""
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        view_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DeleteConfirmationView"
        )
        init_method = next(
            node
            for node in view_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        confirm_method = next(
            node
            for node in view_class.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "confirm"
        )

        initializes_processing = any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr == "processing"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and node.value.value is False
            for node in ast.walk(init_method)
        )
        self.assertTrue(initializes_processing)

        first_await_line = min(
            node.lineno for node in ast.walk(confirm_method) if isinstance(node, ast.Await)
        )
        claims_before_await = any(
            isinstance(node, ast.Assign)
            and node.lineno < first_await_line
            and any(
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr == "processing"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
            for node in ast.walk(confirm_method)
        )
        self.assertTrue(
            claims_before_await,
            "The deletion callback must set self.processing before its first await",
        )

    def test_delete_pacer_is_only_used_for_destructive_requests(self):
        """Successful verification reads must not incur the five-second delete delay."""
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        labels = []

        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_delete_discord_pacer"
            ):
                continue
            labels.append(node.args[0])

        self.assertEqual(len(labels), 2)
        self.assertTrue(all(
            isinstance(label, ast.JoinedStr)
            and label.values
            and isinstance(label.values[0], ast.Constant)
            and label.values[0].value.startswith("delete ")
            for label in labels
        ))

    def test_empty_publish_short_circuits_before_upload_or_page_creation(self):
        """A zero-message capture must never reach the per-channel wiki path."""
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        function = self._async_function(tree, "publish_channel_to_wiki")
        empty_branch = next(
            node
            for node in function.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Name)
            and node.test.operand.id == "real_messages"
        )
        calls = {
            node.func.id: node.lineno
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"archive_attachments", "_require_owned_or_missing_page"}
        }
        self.assertLess(empty_branch.lineno, calls["archive_attachments"])
        self.assertLess(empty_branch.lineno, calls["_require_owned_or_missing_page"])
        self.assertTrue(any(isinstance(node, ast.Return) for node in empty_branch.body))

    def test_empty_finalization_rechecks_before_registry_write(self):
        """The announcement-to-finalize window must not hide a newly posted message."""
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        function = self._async_function(tree, "finalize_empty_archive")
        calls = {}
        for node in ast.walk(function):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "empty_archive"
            ):
                continue
            calls[node.func.attr] = node.lineno
        self.assertLess(calls["is_completely_empty"], calls["insert_record"])

    def test_missing_channel_page_can_use_verified_empty_registry(self):
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        function = self._async_function(tree, "is_channel_archived")
        self.assertTrue(any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_is_recorded_channel_still_empty"
            for node in ast.walk(function)
        ))

    @staticmethod
    def _first_named_call_line(function, name):
        return min(
            (
                node.lineno
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name
            ),
            default=None,
        )

    def test_publish_exposes_category_name_list_option(self):
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        function = self._async_function(tree, "publish")
        arg_names = {arg.arg for arg in function.args.args}
        self.assertIn("categories", arg_names)

    def test_publish_validates_locks_before_any_wiki_work(self):
        """No wiki client/edit may run until every target's lock is validated."""
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        function = self._async_function(tree, "publish")
        lock_line = self._first_named_call_line(function, "_is_locked_readonly")
        wiki_client_line = self._first_named_call_line(function, "get_wiki_client")
        publish_channel_line = self._first_named_call_line(function, "publish_channel_to_wiki")
        self.assertIsNotNone(lock_line, "publish must preflight channel locks")
        self.assertIsNotNone(wiki_client_line)
        self.assertLess(lock_line, wiki_client_line)
        self.assertLess(lock_line, publish_channel_line)

    def test_publish_uses_archive_fast_path_before_capturing(self):
        """is_channel_archived must short-circuit before publish_channel_to_wiki."""
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        function = self._async_function(tree, "publish")
        fast_path_line = self._first_named_call_line(function, "is_channel_archived")
        publish_channel_line = self._first_named_call_line(function, "publish_channel_to_wiki")
        self.assertIsNotNone(fast_path_line, "publish must call is_channel_archived")
        self.assertLess(fast_path_line, publish_channel_line)

    def test_oversize_attachments_route_to_the_nas_sink(self):
        """archive_attachments must have a NAS route and a too-large fallback to it."""
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        function = self._async_function(tree, "archive_attachments")
        nas_calls = [
            node for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_archive_to_nas"
        ]
        # One on the pre-routed path, one in the AttachmentTooLargeError fallback.
        self.assertGreaterEqual(len(nas_calls), 2)
        handles_too_large = any(
            isinstance(node, ast.ExceptHandler)
            and node.type is not None
            and "AttachmentTooLargeError" in ast.dump(node.type)
            for node in ast.walk(function)
        )
        self.assertTrue(handles_too_large)

    def test_deletion_gate_verifies_external_nas_manifest(self):
        """is_channel_archived must verify NAS-hosted attachments too."""
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        function = self._async_function(tree, "is_channel_archived")
        self.assertTrue(any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_archive_external_attachments_match"
            for node in ast.walk(function)
        ))

    def test_publish_builds_and_verifies_external_manifest(self):
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        function = self._async_function(tree, "publish_channel_to_wiki")
        names = {
            node.func.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("render_external_manifest", names)
        self.assertTrue(any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_archive_external_attachments_match"
            for node in ast.walk(function)
        ))

    def test_publish_only_trusts_bot_authored_announcements(self):
        """Announcement reuse must gate on the bot's own authorship of #archives posts."""
        tree = ast.parse(ARCHIVE_BOT_PATH.read_text(encoding="utf-8"))
        function = self._async_function(tree, "publish")
        reads_message_author = any(
            isinstance(node, ast.Attribute)
            and node.attr == "author"
            and isinstance(node.value, ast.Name)
            and node.value.id == "message"
            for node in ast.walk(function)
        )
        references_bot_user = any(
            isinstance(node, ast.Attribute)
            and node.attr == "user"
            and isinstance(node.value, ast.Name)
            and node.value.id == "bot"
            for node in ast.walk(function)
        )
        self.assertTrue(reads_message_author and references_bot_user)


if __name__ == "__main__":
    unittest.main()
