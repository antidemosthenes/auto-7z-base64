import os
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from auto_7z_base64.errors import BundleError
from auto_7z_base64.filesystem import _valid_name
from auto_7z_base64.sevenzip import _run_7z, _validate_members, find_sevenzip


class SafetyTests(unittest.TestCase):
    def test_native_warnings_are_fatal(self):
        result = CompletedProcess(["7z"], 1, stdout="warning: missing source")
        with patch("auto_7z_base64.sevenzip.subprocess.run", return_value=result):
            with self.assertRaisesRegex(BundleError, "exit 1"):
                _run_7z(Path("7z.exe"), ["a"])

    def test_invalid_native_utf8_is_not_silently_replaced(self):
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        with patch("auto_7z_base64.sevenzip.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(BundleError, "invalid UTF-8"):
                _run_7z(Path("7z.exe"), ["l"])

    def test_unsupported_engine_options_have_upgrade_guidance(self):
        result = CompletedProcess(["7z"], 7, stdout="Unknown switch: -ssp")
        with patch("auto_7z_base64.sevenzip.subprocess.run", return_value=result):
            with self.assertRaisesRegex(BundleError, "21.07 or newer"):
                _run_7z(Path("7z.exe"), ["a"])

    def listing(self, records, archive_type="7z"):
        return f"Type = {archive_type}\n----------\n" + "\n\n".join(records) + "\n"

    def test_valid_paths(self):
        _validate_members(
            self.listing(
                ["Path = folder\nAttributes = D", "Path = folder\\résumé.py\nAttributes = A"]
            )
        )

    def test_unicode_line_separators_are_filename_characters(self):
        for separator in ("\u0085", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                _validate_members(self.listing([f"Path = one{separator}two.txt\nAttributes = A"]))

    def test_unsafe_paths(self):
        for name in (
            "../escape",
            "folder/../escape",
            "/absolute",
            "C:\\absolute",
            "\\\\server\\share",
            "a:stream",
            "NUL.txt",
            "trailing.",
            "a//b",
        ):
            with self.subTest(name=name), self.assertRaises(BundleError):
                _validate_members(self.listing([f"Path = {name}\nAttributes = A"]))

    def test_ambiguous_or_link_entries(self):
        listings = [
            self.listing(["Path = a\nAttributes = A", "Path = A\nAttributes = A"]),
            self.listing(["Path = a\nAttributes = A", "Path = a/b\nAttributes = A"]),
            self.listing(["Path = a\nSymbolic Link = elsewhere"]),
            self.listing(["Path = a\nHard Link = elsewhere"]),
            self.listing(["Path = a\nAttributes = A lrwxrwxrwx"]),
            self.listing(["Path = a\nPath = b"]),
            self.listing(["Path = a"], "zip"),
        ]
        for listing in listings:
            with self.subTest(listing=listing), self.assertRaises(BundleError):
                _validate_members(listing)

    def test_archive_names(self):
        for name in ("", "..", "../escape", "NUL", "COM1.txt", "a?b", "trailing "):
            with self.subTest(name=name), self.assertRaises(BundleError):
                _valid_name(name)

    def test_missing_executable(self):
        with (
            patch("auto_7z_base64.sevenzip.sys.platform", "win32"),
            patch.dict(os.environ, {"PATH": ""}),
        ):
            with self.assertRaisesRegex(BundleError, "not found on PATH"):
                find_sevenzip()

    def test_windows_only(self):
        with (
            patch("auto_7z_base64.sevenzip.sys.platform", "linux"),
            patch("auto_7z_base64.sevenzip.Path.is_file") as is_file,
        ):
            with self.assertRaisesRegex(BundleError, "Windows only"):
                find_sevenzip()
            is_file.assert_not_called()

    def test_executable_from_path(self):
        folder = Path.cwd() / "custom-7zip"
        with (
            patch("auto_7z_base64.sevenzip.sys.platform", "win32"),
            patch.dict(os.environ, {"PATH": str(folder)}),
            patch("auto_7z_base64.sevenzip.Path.is_file", return_value=True),
        ):
            self.assertEqual(find_sevenzip(), folder / "7z.exe")
