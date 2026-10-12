import base64
import binascii
import hashlib
import os
import re
from concurrent.futures import CancelledError
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from typing import BinaryIO

from .errors import BundleError
from .parallel import _parallel

DEFAULT_LIMIT = 2_000_000
MAX_LIMIT = 2**63 - 1
LEGACY_HTML_OPENING = b'<pre style="white-space: pre-wrap; overflow-wrap: anywhere;">'
BLOCK_BYTES = 3 * 64 * 1024
EDGE_BYTES = 4096
ASCII_WHITESPACE = b" \t\r\n\v\f"


def volume_size(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 5 <= limit <= MAX_LIMIT:
        raise BundleError(f"Limit must be an integer between 5 and {MAX_LIMIT} bytes.")
    return 3 * ((limit - 1) // 4)


def parse_limit(value: str) -> int:
    match = re.fullmatch(r"\s*([0-9]+)\s*(B|KB|MB|GB|K|M|G|KiB|MiB|GiB)?\s*", value, re.IGNORECASE)
    if not match:
        raise BundleError(
            "Use integer bytes or a size with units, for example 2000000, 2MB, 2MiB, or 2048KB. "
            "KB/MB/GB are decimal; KiB/MiB/GiB and K/M/G are binary."
        )
    unit = (match[2] or "b").lower()
    multiplier = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "k": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gib": 1024**3,
    }[unit]
    digits = match[1].lstrip("0") or "0"
    if len(digits) > len(str(MAX_LIMIT)):
        raise BundleError(f"Limit exceeds the maximum supported size of {MAX_LIMIT} bytes.")
    limit = int(digits) * multiplier
    volume_size(limit)
    return limit


def _payload_bounds(reader: BinaryIO, source: Path) -> tuple[int, int]:
    start = 3 if reader.read(3) == b"\xef\xbb\xbf" else 0
    reader.seek(start)
    wrapped = False
    while chunk := reader.read(EDGE_BYTES):
        content = chunk.lstrip(ASCII_WHITESPACE)
        if content:
            start = reader.tell() - len(content)
            wrapped = content.startswith(b"<")
            break
    if wrapped:
        reader.seek(start)
        opening = reader.readline(1024).strip(ASCII_WHITESPACE)
        if opening not in (LEGACY_HTML_OPENING, b"<pre>"):
            raise BundleError(f"Unsupported HTML wrapper in {source.name}")
        start = reader.tell()
    end = reader.seek(0, os.SEEK_END)
    if wrapped:
        while end > start:
            offset = max(start, end - EDGE_BYTES)
            reader.seek(offset)
            tail = reader.read(end - offset).rstrip(ASCII_WHITESPACE)
            end = offset + len(tail)
            if tail:
                break
        closing = b"</pre>"
        if end - start < len(closing):
            raise BundleError(f"Missing closing </pre> tag in {source.name}")
        reader.seek(end - len(closing))
        if reader.read(len(closing)) != closing:
            raise BundleError(f"Missing closing </pre> tag or extra content in {source.name}")
        end -= len(closing)
    return start, end


def _decode_file(
    source: Path,
    destination: Path | BinaryIO | None = None,
    *,
    cancel: Event | None = None,
    checksum: bool = True,
) -> str | None:
    digest = hashlib.sha256() if checksum else None
    pending = b""
    finished = False
    written = 0
    with (
        source.open("rb") as reader,
        destination.open("wb")
        if isinstance(destination, Path)
        else nullcontext(destination) as writer,
    ):
        start, end = _payload_bounds(reader, source)
        reader.seek(start)
        remaining = end - start
        while remaining:
            if cancel is not None and cancel.is_set():
                raise CancelledError
            chunk = reader.read(min(BLOCK_BYTES, remaining))
            if not chunk:
                raise BundleError(f"Input was truncated while decoding {source.name}")
            remaining -= len(chunk)
            clean = chunk.translate(None, ASCII_WHITESPACE)
            if finished and clean:
                raise BundleError(f"Data after Base64 padding in {source.name}")
            pending += clean
            length = len(pending) // 4 * 4
            if not length:
                continue
            encoded, pending = pending[:length], pending[length:]
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise BundleError(f"Invalid Base64 in {source.name}: {exc}") from exc
            finished = b"=" in encoded
            if finished and pending:
                raise BundleError(f"Data after Base64 padding in {source.name}")
            if writer is not None:
                writer.write(decoded)
            if digest is not None:
                digest.update(decoded)
            written += len(decoded)
        if pending or not written:
            raise BundleError(f"Empty or truncated Base64 in {source.name}")
    return digest.hexdigest() if digest is not None else None


def _encode_part(
    reader: BinaryIO, text_path: Path, remaining: int, limit: int, cancel: Event | None = None
) -> None:
    digest = hashlib.sha256()
    with text_path.open("wb") as text:
        while remaining:
            if cancel is not None and cancel.is_set():
                raise CancelledError
            data = reader.read(min(BLOCK_BYTES, remaining))
            if not data:
                raise BundleError("Archive was truncated while encoding.")
            remaining -= len(data)
            digest.update(data)
            text.write(base64.b64encode(data))
    text_bytes = text_path.stat().st_size
    if text_bytes >= limit:
        raise BundleError(f"Part exceeded the byte limit: {text_path.name}")
    if _decode_file(text_path, cancel=cancel) != digest.hexdigest():
        raise BundleError(f"Base64 verification failed: {text_path.name}")


def _encode_range(
    archive: Path, staged: Path, index: int, size: int, total: int, limit: int, cancel: Event
) -> None:
    offset = (index - 1) * size
    with archive.open("rb") as reader:
        reader.seek(offset)
        _encode_part(
            reader,
            staged / f"{archive.name}.{index:03d}.txt",
            min(size, total - offset),
            limit,
            cancel,
        )


def _encode_archive(archive: Path, staged: Path, size: int, limit: int) -> int:
    total = archive.stat().st_size
    if not total:
        raise BundleError("7-Zip produced an empty archive.")
    count = (total + size - 1) // size
    workers = min(8 if size < BLOCK_BYTES else 4, os.cpu_count() or 1, count)
    if count < 4 or workers == 1:
        with archive.open("rb") as reader:
            for index in range(1, count + 1):
                _encode_part(
                    reader,
                    staged / f"{archive.name}.{index:03d}.txt",
                    min(size, total - (index - 1) * size),
                    limit,
                )
    else:

        def encode_range(index: int, cancel: Event) -> None:
            _encode_range(archive, staged, index, size, total, limit, cancel)

        _parallel(range(1, count + 1), encode_range, workers)
    if archive.stat().st_size != total:
        raise BundleError("Archive size changed while encoding.")
    return count
