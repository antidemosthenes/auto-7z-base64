import base64
import hashlib
import io
import os
import random
import unittest
from itertools import product
from unittest.mock import patch

from auto_7z_base64.codec import (
    BLOCK_BYTES,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    _decode_file,
    _encode_archive,
    parse_limit,
    volume_size,
)
from auto_7z_base64.errors import BundleError
from tests.helpers import HTML_PREFIX, HTML_SUFFIX, WorkspaceTest


class SizeTests(unittest.TestCase):
    def test_oversized_numbers_are_application_errors(self):
        for text in ("9" * 5000, "9999999999999999999999999999MB", str(MAX_LIMIT + 1)):
            with self.subTest(length=len(text)), self.assertRaises(BundleError):
                parse_limit(text)
        self.assertEqual(parse_limit("0" * 5000 + "2000000"), DEFAULT_LIMIT)
        self.assertGreater(volume_size(MAX_LIMIT), 0)

    def test_default_budget(self):
        self.assertEqual(DEFAULT_LIMIT, 2_000_000)
        self.assertEqual(volume_size(DEFAULT_LIMIT), 1_499_997)
        self.assertEqual(4 * (volume_size(DEFAULT_LIMIT) // 3), 1_999_996)

    def test_boundaries(self):
        for limit in range(5, 5000):
            size = volume_size(limit)
            encoded = len(base64.b64encode(bytes(size)))
            self.assertLess(encoded, limit)
            self.assertGreaterEqual(4 * ((size + 3) // 3), limit)

    def test_units(self):
        for text in ("2097152", "2M", "2MiB", "2048KiB", " 2 mib "):
            self.assertEqual(parse_limit(text), 2_097_152)
        self.assertEqual(parse_limit("1GiB"), 1024**3)

    def test_decimal_units(self):
        for text, expected in (
            ("2MB", 2_000_000),
            ("2048KB", 2_048_000),
            ("1GB", 1_000_000_000),
            (" 2 mb ", 2_000_000),
            ("2000000B", 2_000_000),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_limit(text), expected)

    def test_bad_limits(self):
        for text in ("0", "4", "-2M", "2XB", "1.5MiB", "junk"):
            with self.subTest(text=text), self.assertRaises(BundleError):
                parse_limit(text)
        for value in (0, -1, 4, True, 2.5):
            with self.subTest(value=value), self.assertRaises(BundleError):
                volume_size(value)


class Base64Tests(WorkspaceTest):
    def test_direct_part_encoding_preserves_byte_boundaries(self):
        for processors, (size, length) in product(
            (1, 8), ((3, 20), (6, 31), (BLOCK_BYTES + 3, BLOCK_BYTES * 4 + 14))
        ):
            with self.subTest(processors=processors, size=size, length=length):
                archive = self.root / "archive.7z"
                data = random.Random(42).randbytes(length)
                archive.write_bytes(data)
                staged = self.root / f"{processors}-{size}"
                staged.mkdir()
                limit = 4 * (size // 3) + 1
                with patch("auto_7z_base64.codec.os.cpu_count", return_value=processors):
                    count = _encode_archive(archive, staged, size, limit)
                self.assertEqual(count, (length + size - 1) // size)
                joined = bytearray()
                for index in range(1, count + 1):
                    text = (staged / f"archive.7z.{index:03d}.txt").read_bytes()
                    self.assertLess(len(text), limit)
                    expected = data[(index - 1) * size : index * size]
                    self.assertEqual(text, base64.b64encode(expected))
                    joined.extend(base64.b64decode(text, validate=True))
                self.assertEqual(joined, data)

    def test_parts_append_to_one_open_binary_stream(self):
        writer = io.BytesIO()
        for index, data in enumerate((b"\x01", b"\x02\x03", b"\x04\x05\x06")):
            source = self.root / f"{index}.txt"
            source.write_bytes(HTML_PREFIX + base64.b64encode(data) + HTML_SUFFIX)
            self.assertIsNone(_decode_file(source, writer, checksum=False))
            self.assertFalse(writer.closed)
        self.assertEqual(writer.getvalue(), b"\x01\x02\x03\x04\x05\x06")

    def test_hash_only_verification_creates_no_binary_file(self):
        data = os.urandom(BLOCK_BYTES + 1)
        source = self.root / "wrapped.txt"
        source.write_bytes(HTML_PREFIX + base64.b64encode(data) + HTML_SUFFIX)
        self.assertEqual(_decode_file(source), hashlib.sha256(data).hexdigest())
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_utf8_bom_in_plain_and_wrapped_text(self):
        for body in (b"AQID", HTML_PREFIX + b"AQID" + HTML_SUFFIX):
            source = self.root / "bom.txt"
            source.write_bytes(b"\xef\xbb\xbf" + body)
            target = self.root / "output.bin"
            _decode_file(source, target)
            self.assertEqual(target.read_bytes(), b"\x01\x02\x03")

    def test_deterministic_randomized_round_trips(self):
        generator = random.Random(42)
        for index in range(50):
            data = generator.randbytes(generator.randrange(1, BLOCK_BYTES * 2))
            encoded = base64.b64encode(data)
            width = generator.randrange(1, 120)
            wrapped = b"\r\n".join(encoded[i : i + width] for i in range(0, len(encoded), width))
            body = HTML_PREFIX + wrapped + HTML_SUFFIX if index % 2 else wrapped
            source = self.root / "random.txt"
            source.write_bytes(body)
            target = self.root / "output.bin"
            _decode_file(source, target)
            self.assertEqual(target.read_bytes(), data)

    def test_wrapped_base64_streaming_and_line_endings(self):
        data = os.urandom(BLOCK_BYTES * 2 + 2)
        encoded = base64.b64encode(data)
        wrapped = b"\r\n".join(encoded[i : i + 77] for i in range(0, len(encoded), 77))
        for opening in (HTML_PREFIX.replace(b"\n", b"\r\n"), b"<pre>\n"):
            source = self.root / "wrapped.txt"
            source.write_bytes(b" \t" + opening + wrapped + b"\r\n</pre>\r\n ")
            target = self.root / "output.bin"
            _decode_file(source, target)
            self.assertEqual(target.read_bytes(), data)

    def test_wrapper_surrounded_by_large_whitespace(self):
        source = self.root / "wrapped.txt"
        source.write_bytes(
            b" " * (BLOCK_BYTES + 1)
            + HTML_PREFIX
            + b"AQID"
            + HTML_SUFFIX
            + b" " * (BLOCK_BYTES + 1)
        )
        target = self.root / "output.bin"
        _decode_file(source, target)
        self.assertEqual(target.read_bytes(), b"\x01\x02\x03")

    def test_malformed_wrappers(self):
        for data in (
            HTML_PREFIX + b"AQID",
            HTML_PREFIX + b"AQID\n</div>",
            HTML_PREFIX + b"AQID" + HTML_SUFFIX + b"extra",
            HTML_PREFIX + b"AQID</pre>AAAA" + HTML_SUFFIX,
            b"<div>\nAQID\n</div>",
            HTML_PREFIX + b"<span>AQID</span>" + HTML_SUFFIX,
            HTML_PREFIX + HTML_SUFFIX,
        ):
            source = self.root / "malformed.txt"
            source.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(BundleError):
                _decode_file(source, self.root / "output.bin")

    def test_streaming_all_padding_lengths_and_whitespace(self):
        for size in (1, 2, 3, BLOCK_BYTES - 1, BLOCK_BYTES, BLOCK_BYTES + 1, BLOCK_BYTES * 2 + 2):
            data = os.urandom(size)
            encoded = base64.b64encode(data)
            wrapped = b"\r\n".join(encoded[i : i + 77] for i in range(0, len(encoded), 77))
            source = self.root / "input.txt"
            target = self.root / "output.bin"
            source.write_bytes(b" \t" + wrapped + b"\n\v\f")
            _decode_file(source, target)
            self.assertEqual(target.read_bytes(), data)

    def test_invalid_data(self):
        for data in (
            b"",
            b" \n",
            b"abc",
            b"$AAA",
            b"AAAA\xff",
            b"AA==AAAA",
            b"AA=A",
            b"<pre>AAAA</pre>",
        ):
            source = self.root / "input.txt"
            source.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(BundleError):
                _decode_file(source, self.root / "out.bin")

    def test_padding_followed_by_another_chunk(self):
        source = self.root / "input.txt"
        source.write_bytes(b"A" * (BLOCK_BYTES - 4) + b"AA==" + b"AAAA")
        with self.assertRaisesRegex(BundleError, "after Base64 padding"):
            _decode_file(source, self.root / "out.bin")
