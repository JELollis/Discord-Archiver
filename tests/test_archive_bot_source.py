import ast
from pathlib import Path
import unittest


ARCHIVE_BOT_PATH = Path(__file__).resolve().parents[1] / "Archive_Bot.py"


class ArchiveBotSourceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
