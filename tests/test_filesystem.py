import os
from pathlib import Path
from unittest.mock import patch

from auto_7z_base64.errors import BundleError
from auto_7z_base64.filesystem import _collect_parts, _source_paths, _transaction, _valid_name
from tests.helpers import WorkspaceTest


class FilesystemTests(WorkspaceTest):
    def test_invalid_unicode_and_long_names(self):
        for name in ("invalid\ud800", "a" * 256, "😀" * 128, "NUL .txt", "COM1 .txt"):
            with self.subTest(name=repr(name)), self.assertRaises(BundleError):
                _valid_name(name)
        _valid_name("😀" * 127)

    def test_matching_directory_is_not_a_part_file(self):
        (self.root / "one.7z.001.txt").mkdir()
        with self.assertRaisesRegex(BundleError, "Expected a TXT file"):
            _collect_parts([self.root])

    def test_failed_publish_preserves_empty_destination(self):
        output = self.root / "output"
        output.mkdir()
        with (
            patch.object(Path, "rename", side_effect=PermissionError("locked")),
            self.assertRaises(PermissionError),
        ):
            with _transaction(output) as (staged, _scratch):
                (staged / "new.txt").write_text("new")
        self.assertTrue(output.is_dir())
        self.assertEqual(list(output.iterdir()), [])
        self.assertFalse(list(self.root.glob(".output-*")))

    def test_destination_populated_during_work_is_not_overwritten(self):
        output = self.root / "output"
        with self.assertRaisesRegex(BundleError, "empty directory"):
            with _transaction(output) as (staged, _scratch):
                (staged / "new.txt").write_text("new")
                output.mkdir()
                (output / "existing.txt").write_text("keep")
        self.assertEqual((output / "existing.txt").read_text(), "keep")
        self.assertEqual(len(list(output.iterdir())), 1)
        self.assertFalse(list(self.root.glob(".output-*")))

    def test_source_walk_rejects_links(self):
        target = self.root / "target.txt"
        target.write_text("original")
        source = self.root / "source"
        source.mkdir()
        try:
            os.symlink(target, source / "link.txt")
        except OSError as exc:
            self.skipTest(f"Symbolic links unavailable: {exc}")
        with self.assertRaisesRegex(BundleError, "Links and reparse points"):
            _source_paths([source])
        self.assertEqual(target.read_text(), "original")

    def part(self, name, number):
        path = self.root / f"{name}.7z.{number:03d}.txt"
        path.write_bytes(b"AAAA")
        return path

    def test_numeric_order_beyond_999(self):
        for index in range(1, 1002):
            self.part("large", index)
        (self.root / "unrelated.txt").write_text("ignored")
        (self.root / "large.7z.001.html").write_text("ignored")
        parts = _collect_parts([self.root])["large"]
        self.assertEqual(len(parts), 1001)
        self.assertEqual(parts[998].name, "large.7z.999.txt")
        self.assertEqual(parts[999].name, "large.7z.1000.txt")

    def test_multiple_sets_and_explicit_order(self):
        first = self.part("one", 1)
        second = self.part("one", 2)
        other = self.part("two", 1)
        self.assertEqual(
            _collect_parts([second, other, first]), {"one": [first, second], "two": [other]}
        )

    def test_missing_and_duplicate_parts(self):
        first = self.part("one", 1)
        third = self.part("one", 3)
        with self.assertRaisesRegex(BundleError, "Missing part 002"):
            _collect_parts([self.root])
        with self.assertRaisesRegex(BundleError, "Duplicate"):
            _collect_parts([first, first])
        with self.assertRaisesRegex(BundleError, "Missing part 001"):
            _collect_parts([third])

    def test_bad_part_names(self):
        for filename in ("one.7z.000.txt", "one.7z.0001.txt", "one.7z.01.txt", "other.txt"):
            path = self.root / filename
            path.write_bytes(b"AAAA")
            with self.subTest(filename=filename), self.assertRaises(BundleError):
                _collect_parts([path])

    def test_no_parts(self):
        with self.assertRaisesRegex(BundleError, "No named"):
            _collect_parts([self.root])
