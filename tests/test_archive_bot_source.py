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


if __name__ == "__main__":
    unittest.main()
