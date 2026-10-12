import base64
import ctypes
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from auto_7z_base64.cli import main
from auto_7z_base64.codec import (
    BLOCK_BYTES,
    DEFAULT_LIMIT,
    _decode_file,
    parse_limit,
)
from auto_7z_base64.core import decode, encode
from auto_7z_base64.errors import BundleError
from auto_7z_base64.sevenzip import _run_7z
from tests.helpers import HTML_PREFIX, HTML_SUFFIX, SEVENZIP, WorkspaceTest


@unittest.skipUnless(SEVENZIP, "Windows and 7z.exe on PATH are required")
class IntegrationTests(WorkspaceTest):
    def test_encoding_preserves_source_contents_before_decode(self):
        source = self.create_source("test-case", size=10000)
        for index in range(80):
            (source / f"module-{index}.py").write_bytes(f"value = {index}\n".encode())
        original = {
            str(path.relative_to(source)): None if path.is_dir() else path.read_bytes()
            for path in source.rglob("*")
        }
        self.assertGreater(len(original), 80)
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["encode", "test-case"]), 0)
        finally:
            os.chdir(previous)
        self.assertTrue(source.is_dir())
        remaining = {
            str(path.relative_to(source)): None if path.is_dir() else path.read_bytes()
            for path in source.rglob("*")
        }
        self.assertEqual(remaining, original)

    def test_failed_encoding_preserves_source_contents(self):
        source = self.create_source("test-case", size=10000)
        original = {
            str(path.relative_to(source)): None if path.is_dir() else path.read_bytes()
            for path in source.rglob("*")
        }
        destination = self.root / "encode-result"
        with patch("auto_7z_base64.codec._decode_file", side_effect=BundleError("failed")):
            with self.assertRaises(BundleError):
                encode([source], destination, 4096)
        self.assertFalse(destination.exists())
        self.assertTrue(source.is_dir())
        remaining = {
            str(path.relative_to(source)): None if path.is_dir() else path.read_bytes()
            for path in source.rglob("*")
        }
        self.assertEqual(remaining, original)

    def test_many_small_prefetched_files_roundtrip(self):
        source = self.root / "project"
        source.mkdir()
        for index in range(80):
            (source / f"module-{index}.py").write_text(f"value = {index}\n")
        encoded = self.root / "encoded"
        encode([source], encoded)
        decode([encoded], self.root / "recovered")
        self.assert_tree_equal(source, self.root / "recovered" / "project")

    def test_parallel_encoding_failure_and_cancellation_clean_staged_output(self):
        source = self.create_source()
        destination = self.root / "failed"
        for failure in (BundleError("verification failed"), KeyboardInterrupt()):
            with (
                self.subTest(failure=type(failure).__name__),
                patch("auto_7z_base64.codec._decode_file", side_effect=failure),
            ):
                with self.assertRaises(type(failure)):
                    encode([source], destination, 4096)
                self.assertFalse(destination.exists())
                self.assertFalse(list(self.root.glob(".failed-*")))

    def test_generated_parts_extract_with_native_volume_handling(self):
        source = self.create_source()
        encoded = self.root / "encoded"
        result = encode([source], encoded, 4096)
        volumes = self.root / "native-volumes"
        volumes.mkdir()
        for index in range(1, result.parts + 1):
            part = encoded / f"project.7z.{index:03d}.txt"
            _decode_file(part, volumes / part.stem)
        first = volumes / "project.7z.001"
        _run_7z(SEVENZIP, ["t", "-sccUTF-8", "--", str(first)])
        extracted = self.root / "native-extracted"
        _run_7z(SEVENZIP, ["x", f"-o{extracted}", "-sccUTF-8", "-y", "--", str(first)])
        self.assert_tree_equal(source, extracted / "project")

    def test_native_split_parts_decode_into_one_archive(self):
        source = self.create_source()
        native = self.root / "native-volumes"
        native.mkdir()
        _run_7z(
            SEVENZIP,
            ["a", "-t7z", "-v3072b", "-sccUTF-8", "--", str(native / "project.7z"), str(source)],
        )
        encoded = self.root / "encoded"
        encoded.mkdir()
        for volume in native.iterdir():
            (encoded / (volume.name + ".txt")).write_bytes(
                HTML_PREFIX + base64.b64encode(volume.read_bytes()) + HTML_SUFFIX
            )
        decode([encoded], self.root / "recovered")
        self.assert_tree_equal(source, self.root / "recovered" / "project")

    def test_windows_hidden_system_readonly_file_roundtrip(self):
        source = self.root / "attributes"
        source.mkdir()
        special = source / "special.bin"
        special.write_bytes(b"preserve these bytes")
        set_attributes = ctypes.WinDLL("kernel32", use_last_error=True).SetFileAttributesW
        set_attributes.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        set_attributes.restype = ctypes.c_int
        if not set_attributes(str(special), 0x01 | 0x02 | 0x04):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            encoded = self.root / "encoded"
            encode([source], encoded)
            decode([encoded], self.root / "recovered")
            self.assert_tree_equal(source, self.root / "recovered" / "attributes")
        finally:
            set_attributes(str(special), 0x80)

    def test_unicode_line_separator_names_roundtrip(self):
        source = self.root / "unicode"
        source.mkdir()
        for separator in ("\u0085", "\u2028", "\u2029"):
            (source / f"one{separator}two.txt").write_bytes(b"exact bytes")
        encoded = self.root / "encoded"
        encode([source], encoded)
        decode([encoded], self.root / "recovered")
        self.assert_tree_equal(source, self.root / "recovered" / "unicode")

    def test_corrupt_data_is_rejected_during_staged_extraction(self):
        source = self.create_source()
        encoded = self.root / "encoded"
        encode([source], encoded)
        part = encoded / "project.7z.001.txt"
        data = bytearray(base64.b64decode(part.read_bytes(), validate=True))
        data[100] ^= 1
        part.write_bytes(base64.b64encode(data))
        destination = self.root / "corrupt-data"
        with patch("auto_7z_base64.core._run_7z", wraps=_run_7z) as run:
            with self.assertRaises(BundleError):
                decode([encoded], destination)
        commands = [call.args[1][0] for call in run.call_args_list]
        self.assertEqual(commands, ["l", "x"])
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".corrupt-data-*")))

    def test_long_archive_names_fail_before_compression(self):
        source = self.create_source(size=100)
        with patch("auto_7z_base64.core._run_7z") as run:
            with self.assertRaisesRegex(BundleError, "shorter --name"):
                encode([source], self.root / "encoded", name="a" * 245)
        run.assert_not_called()

    def test_bom_saved_part_recovers_archive(self):
        source = self.create_source(size=100)
        encoded = self.root / "encoded"
        encode([source], encoded)
        part = encoded / "project.7z.001.txt"
        part.write_bytes(b"\xef\xbb\xbf" + part.read_bytes())
        decode([encoded], self.root / "recovered")
        self.assert_tree_equal(source, self.root / "recovered" / "project")

    def create_source(self, name="project", size=9000):
        source = self.root / name
        source.mkdir()
        (source / "random.bin").write_bytes(os.urandom(size))
        (source / "résumé.py").write_text("print('こんにちは')\n", encoding="utf-8")
        (source / ".hidden").write_bytes(b"hidden")
        (source / "zero.txt").touch()
        (source / "empty").mkdir()
        return source

    def assert_tree_equal(self, source, recovered):
        left = {str(path.relative_to(source)): path.is_dir() for path in source.rglob("*")}
        right = {str(path.relative_to(recovered)): path.is_dir() for path in recovered.rglob("*")}
        self.assertEqual(left, right)
        for relative, directory in left.items():
            if not directory:
                self.assertEqual(
                    (source / relative).read_bytes(), (recovered / relative).read_bytes()
                )

    def test_single_and_split_roundtrip(self):
        source = self.create_source()
        for limit in (DEFAULT_LIMIT, 4096):
            with self.subTest(limit=limit):
                encoded = self.root / f"encoded-{limit}"
                result = encode([source], encoded, limit)
                texts = sorted(encoded.glob("*.txt"))
                self.assertEqual(result.parts, len(texts))
                self.assertEqual(len(texts), 1 if limit == DEFAULT_LIMIT else 4)
                self.assertFalse(list(encoded.glob("*.001")))
                self.assertEqual(len(list(encoded.iterdir())), len(texts))
                self.assertFalse(list(encoded.glob("*.html")))
                for text in texts:
                    data = text.read_bytes()
                    self.assertNotIn(b"\n", data)
                    self.assertNotIn(b"\r", data)
                    self.assertNotIn(b"<", data)
                    self.assertTrue(base64.b64decode(data, validate=True))
                    self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
                    self.assertLess(len(data), limit)
                recovered = self.root / f"recovered-{limit}"
                results = decode(list(reversed(texts)), recovered)
                self.assertEqual(results[0].output, recovered)
                self.assert_tree_equal(source, recovered / "project")
                self.assertEqual([path.name for path in recovered.iterdir()], ["project"])
                self.assertFalse(list(recovered.rglob("*.7z.*")))
                self.assertFalse(list(self.root.glob(f".recovered-{limit}-*")))

    def test_multi_parent_inputs_and_whitespace(self):
        folder = self.create_source("assets", 100)
        parent = self.root / "different-parent"
        parent.mkdir()
        file = parent / "script.py"
        file.write_text("pass\n")
        encoded = self.root / "encoded"
        encode([folder, file], encoded, name="selected")
        part = encoded / "selected.7z.001.txt"
        data = part.read_bytes()
        wrapped = b"\r\n".join(data[i : i + 76] for i in range(0, len(data), 76))
        part.write_bytes(HTML_PREFIX.replace(b"\n", b"\r\n") + wrapped + b"\r\n</pre>\r\n")
        recovered = self.root / "recovered"
        decode([encoded], recovered)
        self.assert_tree_equal(folder, recovered / "assets")
        self.assertEqual((recovered / "script.py").read_bytes(), file.read_bytes())

    def test_empty_folder(self):
        source = self.root / "empty"
        source.mkdir()
        encoded = self.root / "encoded"
        encode([source], encoded)
        decode([encoded], self.root / "recovered")
        self.assertTrue((self.root / "recovered" / "empty").is_dir())

    def test_legacy_html_wrapped_base64_parts(self):
        source = self.create_source(size=100)
        encoded = self.root / "encoded"
        encode([source], encoded)
        for part in encoded.glob("*.txt"):
            part.write_bytes(HTML_PREFIX + part.read_bytes() + HTML_SUFFIX)
        decode([encoded], self.root / "recovered")
        self.assert_tree_equal(source, self.root / "recovered" / "project")

    def test_full_part_at_decimal_2mb_limit(self):
        source = self.create_source(size=1_500_100)
        encoded = self.root / "encoded"
        result = encode([source], encoded, parse_limit("2MB"), name="case")
        self.assertEqual(result.parts, 2)
        self.assertEqual((encoded / "case.7z.001.txt").stat().st_size, 1_999_996)
        self.assertTrue(all(path.suffix == ".txt" for path in encoded.iterdir()))
        self.assertTrue(all(path.stat().st_size <= 2_000_000 for path in encoded.iterdir()))
        decode([encoded], self.root / "recovered")
        self.assert_tree_equal(source, self.root / "recovered" / "project")

    def test_streaming_large_volume(self):
        source = self.create_source(size=BLOCK_BYTES * 2 + 2)
        encoded = self.root / "encoded"
        encode([source], encoded)
        decode([encoded], self.root / "recovered")
        self.assert_tree_equal(source, self.root / "recovered" / "project")

    def test_minimum_budget_and_more_than_999_real_volumes(self):
        source = self.create_source("tiny parts", size=3100)
        encoded = self.root / "encoded"
        result = encode([source], encoded, 5)
        self.assertGreater(result.parts, 999)
        self.assertTrue((encoded / "tiny parts.7z.1000.txt").is_file())
        self.assertTrue(all(part.stat().st_size < 5 for part in encoded.iterdir()))
        decode([encoded], self.root / "recovered")
        self.assert_tree_equal(source, self.root / "recovered" / "tiny parts")

    def test_single_file_with_special_characters_and_empty_destination(self):
        parent = self.root / "source with spaces"
        parent.mkdir()
        source = parent / "-@résumé[1].py"
        original = b"print('exact file bytes')\r\n"
        source.write_bytes(original)
        encoded = self.root / "encoded"
        encoded.mkdir()
        encode([source], encoded)
        decode([encoded], self.root / "recovered")
        recovered = self.root / "recovered" / source.name
        self.assertEqual(recovered.read_bytes(), original)
        self.assertEqual(source.read_bytes(), original)

    def test_batch_recovery_and_atomic_failure(self):
        combined = self.root / "combined"
        combined.mkdir()
        for name in ("alpha", "zeta"):
            source = self.create_source(name, 100)
            encoded = self.root / f"encoded-{name}"
            encode([source], encoded)
            for file in encoded.glob("*.txt"):
                (combined / file.name).write_bytes(file.read_bytes())
        decoded = decode([combined], self.root / "good")
        self.assertEqual([result.name for result in decoded], ["alpha", "zeta"])
        for result in decoded:
            self.assertEqual(result.output, self.root / "good" / result.name)
            self.assert_tree_equal(self.root / result.name, result.output / result.name)
            self.assertEqual([path.name for path in result.output.iterdir()], [result.name])
        self.assertFalse(list((self.root / "good").rglob("*.7z.*")))
        (combined / "zeta.7z.001.txt").write_bytes(b"bad!")
        destination = self.root / "bad"
        destination.mkdir()
        with self.assertRaises(BundleError):
            decode([combined], destination)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(list(self.root.glob(".bad-*")))

    def test_corruption_and_missing_final_volume(self):
        source = self.create_source()
        encoded = self.root / "encoded"
        encode([source], encoded, 4096)
        parts = sorted(encoded.glob("*.txt"))
        with self.assertRaises(BundleError):
            decode(parts[:-1], self.root / "truncated")
        first = parts[0]
        original = first.read_bytes()
        index = 0
        changed = b"A" if original[index : index + 1] != b"A" else b"B"
        first.write_bytes(original[:index] + changed + original[index + 1 :])
        with self.assertRaises(BundleError):
            decode(parts, self.root / "corrupt")
        self.assertFalse((self.root / "truncated").exists())
        self.assertFalse((self.root / "corrupt").exists())

    def test_source_and_destination_rejections(self):
        source = self.create_source(size=100)
        with self.assertRaisesRegex(BundleError, "inside"):
            encode([source], source / "output")
        with self.assertRaisesRegex(BundleError, "Overlapping"):
            encode([source, source / "random.bin"], self.root / "output")
        second_parent = self.root / "other"
        second_parent.mkdir()
        conflict = second_parent / source.name
        conflict.mkdir()
        with self.assertRaisesRegex(BundleError, "Conflicting"):
            encode([source, conflict], self.root / "output")
        destination = self.root / "occupied"
        destination.mkdir()
        existing = destination / "keep.txt"
        existing.write_bytes(b"keep")
        with self.assertRaises(BundleError):
            encode([source], destination)
        self.assertEqual(existing.read_bytes(), b"keep")

    def test_archive_failure_cleanup(self):
        source = self.create_source(size=100)
        destination = self.root / "failed"
        with patch("auto_7z_base64.core._run_7z", side_effect=BundleError("compression failed")):
            with self.assertRaises(BundleError):
                encode([source], destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob(".failed-*")))

    def test_cli(self):
        source = self.create_source(size=10)
        encoded = self.root / "encoded"
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(["encode", str(source), "--output", str(encoded)]), 0)
            self.assertEqual(
                main(["decode", str(encoded), "--output", str(self.root / "recovered")]), 0
            )
            self.assertEqual(
                main(["decode", str(encoded), "--output", str(self.root / "recovered")]), 1
            )
        self.assertIn("Encoded", stdout.getvalue())
        self.assertIn("Recovered", stdout.getvalue())
        self.assertIn(f'Encoded TXT files saved to: "{encoded}"', stdout.getvalue())
        self.assertIn(f'Decoded files saved to: "{self.root / "recovered"}"', stdout.getvalue())
        self.assertIn("empty directory", stderr.getvalue())

    def test_cli_requires_only_inputs_for_both_commands(self):
        source = self.create_source(size=100)
        previous = Path.cwd()
        stdout = io.StringIO()
        try:
            os.chdir(self.root)
            with redirect_stdout(stdout):
                self.assertEqual(main(["encode", str(source)]), 0)
                encoded = self.root / "encode-result"
                self.assertTrue((encoded / "project.7z.001.txt").is_file())
                self.assertEqual(main(["decode", str(encoded)]), 0)
            self.assert_tree_equal(source, self.root / "decode-result" / "project")
            self.assertIn("text budget: 2,000,000 bytes", stdout.getvalue())
            self.assertIn(
                f'Encoded TXT files saved to: "{self.root / "encode-result"}"', stdout.getvalue()
            )
            self.assertIn(
                f'Decoded files saved to: "{self.root / "decode-result"}"', stdout.getvalue()
            )
        finally:
            os.chdir(previous)
