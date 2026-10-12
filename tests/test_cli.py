import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from auto_7z_base64.cli import main
from auto_7z_base64.core import EncodeResult, decode, encode
from auto_7z_base64.errors import BundleError
from auto_7z_base64.sevenzip import find_sevenzip
from tests.helpers import WorkspaceTest


class PrerequisiteTests(WorkspaceTest):
    def test_oversized_limit_is_an_argparse_error(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_context:
            main(["encode", "source", "--limit", "9" * 5000])
        self.assertEqual(exit_context.exception.code, 2)
        self.assertIn("maximum supported size", stderr.getvalue())

    def test_non_unicode_terminal_can_report_unicode_paths(self):
        destination = self.root / "résumé"
        result = EncodeResult(destination, 1, 100)
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="ascii", write_through=True)
        try:
            with (
                patch("auto_7z_base64.cli.encode", return_value=result),
                patch("sys.stdout", stream),
            ):
                self.assertEqual(main(["encode", "source"]), 0)
            self.assertIn(
                str(destination).encode("ascii", errors="backslashreplace"), buffer.getvalue()
            )
        finally:
            stream.detach()

    def test_help_documents_command_name_and_limit_formats(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as exit_context:
            main(["encode", "--help"])
        self.assertEqual(exit_context.exception.code, 0)
        help_text = " ".join(stdout.getvalue().split())
        for text in (
            "auto-7z-base64",
            "--limit SIZE",
            "2MB",
            "2MiB",
            "2048KB",
            "decimal",
            "binary",
            "Default: 2MB (2000000 bytes)",
            "[--output OUTPUT]",
            "default: encode-result",
        ):
            self.assertIn(text, help_text)

    def test_both_commands_refuse_before_processing_inputs(self):
        for operation in (encode, decode):
            destination = self.root / operation.__name__
            with (
                self.subTest(operation=operation.__name__),
                patch("auto_7z_base64.sevenzip.sys.platform", "win32"),
                patch.dict(os.environ, {"PATH": ""}),
            ):
                with self.assertRaisesRegex(BundleError, "not found on PATH"):
                    operation([self.root / "missing-input"], destination)
                self.assertFalse(destination.exists())
                self.assertFalse(list(self.root.glob(f".{operation.__name__}-*")))

    def test_cli_reports_missing_path_executable(self):
        for command in ("encode", "decode"):
            stderr = io.StringIO()
            with (
                self.subTest(command=command),
                patch("auto_7z_base64.sevenzip.sys.platform", "win32"),
                patch.dict(os.environ, {"PATH": ""}),
                redirect_stderr(stderr),
            ):
                code = main(
                    [
                        command,
                        str(self.root / "missing-input"),
                        "--output",
                        str(self.root / "output"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("add the folder containing 7z.exe to PATH", stderr.getvalue())
            self.assertFalse((self.root / "output").exists())

    def test_current_folder_executable_does_not_bypass_path(self):
        (self.root / "7z.exe").touch()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with (
                patch("auto_7z_base64.sevenzip.sys.platform", "win32"),
                patch.dict(os.environ, {"PATH": ""}),
            ):
                with self.assertRaisesRegex(BundleError, "not found on PATH"):
                    find_sevenzip()
        finally:
            os.chdir(previous)

    def test_quoted_path_entries_and_search_order(self):
        first = self.root / "first folder"
        second = self.root / "second folder"
        first.mkdir()
        second.mkdir()
        (second / "7z.exe").touch()
        search_path = os.pathsep.join([str(first), f'"{second}"'])
        with (
            patch("auto_7z_base64.sevenzip.sys.platform", "win32"),
            patch.dict(os.environ, {"PATH": search_path}),
        ):
            self.assertEqual(find_sevenzip(), second / "7z.exe")
            (first / "7z.exe").touch()
            self.assertEqual(find_sevenzip(), first / "7z.exe")
