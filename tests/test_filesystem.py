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

    def part(self, name, number, folder=None):
        folder = self.root if folder is None else folder
        path = folder / f"{name}.7z.{number:03d}.txt"
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

    def test_archive_and_part_hints_select_siblings(self):
        first = self.part("one", 1)
        second = self.part("one", 2)
        archive_hint = self.root / "one.7z"
        archive_hint.touch()
        self.assertEqual(_collect_parts([archive_hint]), {"one": [first, second]})
        archive_hint.unlink()
        self.assertEqual(_collect_parts([self.root / "one.7z.002.txt"]), {"one": [first, second]})

    def test_overlapping_selectors_are_deduplicated_and_sets_stay_local(self):
        first = self.part("one", 1)
        second = self.part("one", 2)
        with patch("auto_7z_base64.filesystem.os.scandir", wraps=os.scandir) as scan:
            groups = _collect_parts([self.root / "one.7z", first, second, self.root])
        self.assertEqual(groups, {"one": [first, second]})
        scan.assert_called_once_with(self.root)
        other_folder = self.root / "other-folder"
        other_folder.mkdir()
        other = self.part("other", 1, folder=other_folder)
        self.assertEqual(_collect_parts([first, other]), {"one": [first, second], "other": [other]})
        self.part("one", 1, folder=other_folder)
        with self.assertRaisesRegex(BundleError, "Conflicting parts for archive one in folders"):
            _collect_parts([self.root, other_folder])

    def test_part_hint_scans_case_insensitively_and_ignores_notes(self):
        first = self.part("one", 1)
        second = self.root / "ONE.7Z.002.TXT"
        second.write_bytes(b"AAAA")
        (self.root / "one.7z.003-note.txt").write_bytes(b"ignored")
        (self.root / "unrelated.7z.001.txt").mkdir()
        (self.root / "unrelated.7z.01.txt").touch()
        self.assertEqual(_collect_parts([self.root / "ONE.7Z.002.TXT"]), {"one": [first, second]})

    def test_selector_rejects_malformed_numbers_and_matching_directories(self):
        for filename in ("one.7z.000.txt", "one.7z.01.txt", "one.7z.0001.txt"):
            (self.root / filename).write_bytes(b"AAAA")
            with (
                self.subTest(filename=filename),
                self.assertRaisesRegex(BundleError, "Invalid part"),
            ):
                _collect_parts([self.root / "one.7z"])
            (self.root / filename).unlink()
        (self.root / "one.7z.001.txt").mkdir()
        with self.assertRaisesRegex(BundleError, "Expected a TXT file"):
            _collect_parts([self.root / "one.7z"])

    def test_named_set_without_parts_fails_even_when_another_set_exists(self):
        self.part("one", 1)
        with self.assertRaisesRegex(BundleError, "No TXT parts found for archive missing"):
            _collect_parts([self.root / "one.7z", self.root / "missing.7z"])

    def test_matching_links_are_rejected(self):
        target = self.part("one", 1)
        try:
            (self.root / "one.7z.002.txt").symlink_to(target)
        except OSError as exc:
            self.skipTest(f"Symbolic links unavailable: {exc}")
        with self.assertRaisesRegex(BundleError, "Links and reparse points"):
            _collect_parts([self.root / "one.7z"])

    def test_multiple_sets_and_explicit_order(self):
        first = self.part("one", 1)
        second = self.part("one", 2)
        other = self.part("two", 1)
        self.assertEqual(
            _collect_parts([second, other, first]), {"one": [first, second], "two": [other]}
        )

    def test_missing_parts_from_folder_and_hints(self):
        first = self.part("one", 1)
        third = self.part("one", 3)
        with self.assertRaisesRegex(BundleError, "Missing part 002"):
            _collect_parts([self.root])
        with self.assertRaisesRegex(BundleError, "Missing part 002"):
            _collect_parts([first, first])
        first.unlink()
        with self.assertRaisesRegex(BundleError, "Missing part 001"):
            _collect_parts([third])

    def test_distinct_files_with_same_folded_name_and_number_conflict(self):
        self.part("straße", 1)
        self.part("strasse", 1)
        with self.assertRaisesRegex(BundleError, "Duplicate part 001"):
            _collect_parts([self.root / "strasse.7z"])

    def test_bad_part_names(self):
        for filename in ("one.7z.000.txt", "one.7z.0001.txt", "one.7z.01.txt", "other.txt"):
            path = self.root / filename
            path.write_bytes(b"AAAA")
            with self.subTest(filename=filename), self.assertRaises(BundleError):
                _collect_parts([path])

    def test_no_parts(self):
        with self.assertRaisesRegex(BundleError, "No named"):
            _collect_parts([self.root])
