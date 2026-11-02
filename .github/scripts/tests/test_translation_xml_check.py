"""Tests for .github/scripts/translation_xml_check.py.

Run from the repository root with:

    python3 -m unittest discover -s .github/scripts/tests -v
"""

from __future__ import annotations

import codecs
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import translation_xml_check as checker  # noqa: E402

BOM = codecs.BOM_UTF8


def xml_bytes(
    body: str,
    *,
    root: str = "LanguageData",
    bom: bool = True,
    newline: str = "\r\n",
    encoding: str = "UTF-8",
) -> bytes:
    """Build a small translation XML document for a test."""
    text = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        f"\n<{root}>\n{body}\n</{root}>\n"
    )
    text = text.replace("\n", newline)
    encoded = text.encode(encoding)
    return (BOM + encoded) if bom else encoded


class ValidationPairTests(unittest.TestCase):
    def assert_fails(self, base: bytes, head: bytes, *needles: str) -> None:
        errors = checker.validate_pair(base, head)
        self.assertTrue(errors, "expected validation errors")
        message = "\n".join(errors)
        for needle in needles:
            self.assertIn(needle, message)

    def test_normal_translation_edit_passes(self) -> None:
        base = xml_bytes("  <Greeting>Hello {0}</Greeting>")
        head = xml_bytes("  <Greeting>你好 {0}</Greeting>")
        self.assertEqual(checker.validate_pair(base, head), [])

    def test_placeholder_reorder_passes(self) -> None:
        base = xml_bytes("  <Greeting>{0} from {1}</Greeting>")
        head = xml_bytes("  <Greeting>来自{1}的{0}</Greeting>")
        self.assertEqual(checker.validate_pair(base, head), [])

    def test_duplicate_placeholder_occurrences_preserved(self) -> None:
        base = xml_bytes("  <Greeting>{0} {0} {1}</Greeting>")
        head = xml_bytes("  <Greeting>{1} {0} {0}</Greeting>")
        self.assertEqual(checker.validate_pair(base, head), [])

    def test_missing_placeholder_fails(self) -> None:
        base = xml_bytes("  <Greeting>Hello {0}</Greeting>")
        head = xml_bytes("  <Greeting>你好</Greeting>")
        self.assert_fails(base, head, "占位符", "{0}")

    def test_extra_placeholder_fails(self) -> None:
        base = xml_bytes("  <Greeting>Hello</Greeting>")
        head = xml_bytes("  <Greeting>你好 {0}</Greeting>")
        self.assert_fails(base, head, "占位符", "{0}")

    def test_changed_key_fails(self) -> None:
        base = xml_bytes("  <Greeting>Hello {0}</Greeting>")
        head = xml_bytes("  <Greeting2>Hello {0}</Greeting2>")
        self.assert_fails(base, head, "键", "Greeting", "删除或重命名")

    def test_duplicate_key_fails(self) -> None:
        base = xml_bytes("  <Greeting>Hello {0}</Greeting>")
        head = xml_bytes(
            "  <Greeting>Hello {0}</Greeting>\n"
            "  <Greeting>Hello {0}</Greeting>"
        )
        self.assert_fails(base, head, "重复键", "Greeting")

    def test_added_key_passes(self) -> None:
        base = xml_bytes("  <Greeting>Hello {0}</Greeting>")
        head = xml_bytes(
            "  <Greeting>Hello {0}</Greeting>\n"
            "  <Additional>New key</Additional>"
        )
        self.assertEqual(checker.validate_pair(base, head), [])

    def test_invalid_xml_fails(self) -> None:
        base = xml_bytes("  <Greeting>Hello {0}</Greeting>")
        head = xml_bytes("  <Greeting>Hello {0}")
        self.assert_fails(base, head, "有效的 XML")

    def test_bom_removed_fails(self) -> None:
        base = xml_bytes("  <Greeting>Hello {0}</Greeting>", bom=True)
        head = xml_bytes("  <Greeting>Hello {0}</Greeting>", bom=False)
        self.assert_fails(base, head, "BOM", "无 BOM")

    def test_bom_added_fails(self) -> None:
        base = xml_bytes("  <Greeting>Hello {0}</Greeting>", bom=False)
        head = xml_bytes("  <Greeting>Hello {0}</Greeting>", bom=True)
        self.assert_fails(base, head, "BOM")

    def test_line_ending_changed_fails(self) -> None:
        base = xml_bytes("  <Greeting>Hello {0}</Greeting>", newline="\r\n")
        head = xml_bytes("  <Greeting>Hello {0}</Greeting>", newline="\n")
        self.assert_fails(base, head, "换行格式", "LF", "CRLF")

    def test_encoding_changed_fails(self) -> None:
        base = xml_bytes(
            "  <Greeting>Hello {0}</Greeting>", encoding="UTF-8"
        )
        head = xml_bytes(
            "  <Greeting>Hello {0}</Greeting>", encoding="ISO-8859-1"
        )
        self.assert_fails(base, head, "编码被改变")

    def test_equivalent_encoding_spelling_passes(self) -> None:
        base = xml_bytes("  <Greeting>Hello {0}</Greeting>", encoding="UTF-8")
        head = xml_bytes("  <Greeting>Hello {0}</Greeting>", encoding="utf8")
        self.assertEqual(checker.validate_pair(base, head), [])

    def test_removed_node_fails(self) -> None:
        base = xml_bytes("  <Greeting>Hello <b>world</b> {0}</Greeting>")
        head = xml_bytes("  <Greeting>Hello world {0}</Greeting>")
        self.assert_fails(base, head, "节点或属性缺失", "<b>")

    def test_changed_attribute_fails(self) -> None:
        base = xml_bytes(
            '  <Greeting>Hello <span class="old">world</span> {0}</Greeting>'
        )
        head = xml_bytes(
            '  <Greeting>Hello <span class="new">world</span> {0}</Greeting>'
        )
        self.assert_fails(base, head, "节点或属性缺失", "class")

    def test_added_node_passes(self) -> None:
        base = xml_bytes("  <Greeting>Hello {0}</Greeting>")
        head = xml_bytes("  <Greeting>Hello <b>{0}</b></Greeting>")
        self.assertEqual(checker.validate_pair(base, head), [])

    def test_duplicate_li_nodes_are_not_duplicate_keys(self) -> None:
        base = xml_bytes(
            "  <SomeList>\n"
            "    <li>first</li>\n"
            "    <li>second</li>\n"
            "    <li>third</li>\n"
            "  </SomeList>"
        )
        head = xml_bytes(
            "  <SomeList>\n"
            "    <li>第一</li>\n"
            "    <li>第二</li>\n"
            "    <li>第三</li>\n"
            "  </SomeList>"
        )
        self.assertEqual(checker.validate_pair(base, head), [])

    def test_repeated_root_li_nodes_are_not_duplicate_keys(self) -> None:
        base = xml_bytes(
            "  <li>one</li>\n  <li>two</li>",
            root="LanguageInfo",
        )
        head = xml_bytes(
            "  <li>一</li>\n  <li>二</li>",
            root="LanguageInfo",
        )
        self.assertEqual(checker.validate_pair(base, head), [])

    def test_added_li_node_passes(self) -> None:
        base = xml_bytes(
            "  <SomeList>\n    <li>first</li>\n    <li>second</li>\n"
            "  </SomeList>"
        )
        head = xml_bytes(
            "  <SomeList>\n    <li>first</li>\n    <li>second</li>\n"
            "    <li>third</li>\n  </SomeList>"
        )
        self.assertEqual(checker.validate_pair(base, head), [])

    def test_removed_li_node_fails(self) -> None:
        base = xml_bytes(
            "  <SomeList>\n    <li>first</li>\n    <li>second</li>\n"
            "  </SomeList>"
        )
        head = xml_bytes("  <SomeList>\n    <li>first</li>\n  </SomeList>")
        self.assert_fails(base, head, "节点或属性缺失", "<li>")

    def test_newline_format_marker_removed_fails(self) -> None:
        base = xml_bytes(r"  <Greeting>Line one\nLine two {0}</Greeting>")
        head = xml_bytes(r"  <Greeting>Line one Line two {0}</Greeting>")
        self.assert_fails(base, head, r"换行格式标记 \n")

    def test_conditional_placeholder_branch_text_may_change(self) -> None:
        base = xml_bytes(
            "  <Greeting>{PAWN_gender ? 小子 : 女孩}</Greeting>"
        )
        head = xml_bytes(
            "  <Greeting>{PAWN_gender ? 男孩 : 女孩}</Greeting>"
        )
        self.assertEqual(checker.validate_pair(base, head), [])

    def test_conditional_placeholder_name_change_fails(self) -> None:
        base = xml_bytes(
            "  <Greeting>{PAWN_gender ? 小子 : 女孩}</Greeting>"
        )
        head = xml_bytes(
            "  <Greeting>{PAWN_pronoun ? 小子 : 女孩}</Greeting>"
        )
        self.assert_fails(base, head, "占位符", "PAWN_gender", "PAWN_pronoun")

    def test_key_binding_placeholder_change_fails(self) -> None:
        base = xml_bytes("  <Greeting>{Key:Cancel}</Greeting>")
        head = xml_bytes("  <Greeting>{Key:Accept}</Greeting>")
        self.assert_fails(base, head, "占位符", "Key:Cancel", "Key:Accept")

    def test_escaped_brace_literal_changed_to_placeholder_fails(self) -> None:
        base = xml_bytes("  <Greeting>拾取 {1_labelShort} x{{0}}</Greeting>")
        head = xml_bytes("  <Greeting>拾取 {1_labelShort} x{0}</Greeting>")
        self.assert_fails(base, head, "占位符")

    def test_unchanged_file_passes(self) -> None:
        content = xml_bytes("  <Greeting>Hello {0}</Greeting>")
        self.assertEqual(checker.validate_pair(content, content), [])


class NewFileTests(unittest.TestCase):
    def test_valid_new_file_passes(self) -> None:
        content = xml_bytes("  <Greeting>Hello {0}</Greeting>")
        self.assertEqual(checker.validate_new(content), [])

    def test_invalid_new_file_fails(self) -> None:
        content = xml_bytes("  <Greeting>Hello {0}")
        errors = checker.validate_new(content)
        self.assertTrue(errors)
        self.assertIn("有效的 XML", "\n".join(errors))

    def test_duplicate_key_in_new_file_fails(self) -> None:
        content = xml_bytes(
            "  <Greeting>Hello {0}</Greeting>\n"
            "  <Greeting>Hello {0}</Greeting>"
        )
        errors = checker.validate_new(content)
        self.assertTrue(errors)
        self.assertIn("重复键", "\n".join(errors))


class GitRepo:
    """A temporary git repository used by end-to-end CLI tests."""

    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tempdir.name)
        self.git("init", "-q", "-b", "master")
        self.git("config", "user.name", "Translation Check Tests")
        self.git("config", "user.email", "translation-checks@example.invalid")

    def cleanup(self) -> None:
        self._tempdir.cleanup()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def write(self, relative_path: str, data: bytes) -> None:
        target = self.path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def run_check(self, base: str, head: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "translation_xml_check.py"),
                "--repo",
                str(self.path),
                "--base",
                base,
                "--head",
                head,
            ],
            check=False,
            capture_output=True,
            text=True,
        )


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = GitRepo()
        self.addCleanup(self.repo.cleanup)

    def test_no_changed_xml_reports_no_incremental_xml(self) -> None:
        self.repo.write("README.md", b"# test\n")
        base = self.repo.commit("base")
        self.repo.write("README.md", b"# test\nchanged\n")
        head = self.repo.commit("docs only")
        result = self.repo.run_check(base, head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("没有增量 XML 需要检查", result.stdout)
        self.assertIn("通过", result.stdout)

    def test_modified_xml_passes_end_to_end(self) -> None:
        self.repo.write(
            "Core/Keyed/Test.xml",
            xml_bytes("  <Greeting>Hello {0}</Greeting>"),
        )
        base = self.repo.commit("base")
        self.repo.write(
            "Core/Keyed/Test.xml",
            xml_bytes("  <Greeting>你好 {0}</Greeting>"),
        )
        head = self.repo.commit("translate")
        result = self.repo.run_check(base, head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[通过]", result.stdout)
        self.assertIn("Core/Keyed/Test.xml", result.stdout)

    def test_invalid_xml_fails_end_to_end(self) -> None:
        self.repo.write(
            "Core/Keyed/Test.xml",
            xml_bytes("  <Greeting>Hello {0}</Greeting>"),
        )
        base = self.repo.commit("base")
        self.repo.write(
            "Core/Keyed/Test.xml",
            xml_bytes("  <Greeting>Hello {0}"),
        )
        head = self.repo.commit("broken")
        result = self.repo.run_check(base, head)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("有效的 XML", result.stdout + result.stderr)

    def test_deleted_xml_fails_end_to_end(self) -> None:
        self.repo.write(
            "Royalty/Keyed/Test.xml",
            xml_bytes("  <Greeting>Hello {0}</Greeting>"),
        )
        base = self.repo.commit("base")
        self.repo.git("rm", "-q", "Royalty/Keyed/Test.xml")
        head = self.repo.commit("delete")
        result = self.repo.run_check(base, head)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("翻译 XML 文件被删除", result.stdout)

    def test_added_xml_passes_end_to_end(self) -> None:
        self.repo.write("README.md", b"# test\n")
        base = self.repo.commit("base")
        self.repo.write(
            "Ideology/Keyed/New.xml",
            xml_bytes("  <Greeting>Hello {0}</Greeting>"),
        )
        head = self.repo.commit("add file")
        result = self.repo.run_check(base, head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[通过]", result.stdout)
        self.assertIn("Ideology/Keyed/New.xml", result.stdout)

    def test_no_baseline_runs_full_structural_check(self) -> None:
        self.repo.write(
            "Core/Keyed/Test.xml",
            xml_bytes("  <Greeting>Hello {0}</Greeting>"),
        )
        head = self.repo.commit("base")
        result = self.repo.run_check("", head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("全量结构校验", result.stdout)
        self.assertIn("全量检查的翻译 XML: 1 个", result.stdout)

    def test_non_xml_files_in_translation_roots_are_ignored(self) -> None:
        self.repo.write("Core/Keyed/notes.txt", b"one\n")
        base = self.repo.commit("base")
        self.repo.write("Core/Keyed/notes.txt", b"two\n")
        head = self.repo.commit("edit notes")
        result = self.repo.run_check(base, head)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("没有增量 XML 需要检查", result.stdout)


if __name__ == "__main__":
    unittest.main()
