from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .codec import DEFAULT_LIMIT as DEFAULT_LIMIT
from .codec import _decode_file, _encode_archive, volume_size
from .codec import parse_limit as parse_limit
from .errors import BundleError as BundleError
from .filesystem import (
    _absolute,
    _collect_parts,
    _empty_destination,
    _source_paths,
    _transaction,
    _valid_name,
)
from .prefetch import PrefetchPlan
from .sevenzip import _run_7z, _validate_members, find_sevenzip

DEFAULT_ENCODE_OUTPUT = Path("encode-result")
DEFAULT_DECODE_OUTPUT = Path("decode-result")


@dataclass(frozen=True)
class EncodeResult:
    output: Path
    parts: int
    volume_bytes: int


@dataclass(frozen=True)
class DecodeResult:
    name: str
    output: Path
    parts: int


def encode(
    inputs: Sequence[Path],
    output: Path = DEFAULT_ENCODE_OUTPUT,
    limit: int = DEFAULT_LIMIT,
    name: str | None = None,
    *,
    progress: Callable[[str], None] | None = None,
) -> EncodeResult:
    size = volume_size(limit)
    executable = find_sevenzip()
    destination = _absolute(Path(output))
    _empty_destination(destination)
    if progress:
        progress("Scanning input files...")
    prefetch = PrefetchPlan()
    sources = _source_paths(inputs, prefetch)
    for source in sources:
        if source.is_dir() and destination.resolve().is_relative_to(source.resolve()):
            raise BundleError(f"Output cannot be inside an input folder: {source}")
    if name is not None:
        archive_name = name
    elif len(sources) == 1:
        archive_name = sources[0].name if sources[0].is_dir() else sources[0].stem
    else:
        archive_name = "bundle"
    _valid_name(archive_name)
    try:
        _valid_name(archive_name + ".7z.001.txt")
    except BundleError as exc:
        raise BundleError("Archive name is too long; choose a shorter --name.") from exc
    with _transaction(destination) as (staged, scratch):
        if progress:
            progress(f"Compressing {archive_name} with 7-Zip...")
        prefetch.warm()
        archive = scratch / (archive_name + ".7z")
        listfile = scratch / "inputs.txt"
        listfile.write_text("".join(str(source) + "\n" for source in sources), encoding="utf-8")
        _run_7z(
            executable,
            [
                "a",
                "-t7z",
                "-mx9",
                "-ms=on",
                "-sse",
                "-ssp",
                "-spd",
                "-scsUTF-8",
                "-sccUTF-8",
                "-bd",
                "-y",
                f"-i@{listfile}",
                "--",
                str(archive),
            ],
            cwd=scratch,
        )
        count = (archive.stat().st_size + size - 1) // size
        _valid_name(f"{archive.name}.{count:03d}.txt")
        if progress:
            progress(f"Verifying {archive.name}...")
        _run_7z(executable, ["t", "-sccUTF-8", "-bd", "--", str(archive)])
        _validate_members(_run_7z(executable, ["l", "-slt", "-sccUTF-8", "--", str(archive)]))
        if progress:
            progress(f"Writing and verifying {count} Base64 part(s)...")
        parts = _encode_archive(archive, staged, size, limit)
    return EncodeResult(destination, parts, size)


def decode(
    inputs: Sequence[Path],
    output: Path = DEFAULT_DECODE_OUTPUT,
    *,
    progress: Callable[[str], None] | None = None,
) -> list[DecodeResult]:
    executable = find_sevenzip()
    destination = _absolute(Path(output))
    _empty_destination(destination)
    if progress:
        progress("Finding TXT parts...")
    groups = _collect_parts(inputs)
    results = []
    multiple_archives = len(groups) > 1
    with _transaction(destination) as (staged, scratch):
        for name, parts in groups.items():
            extracted = staged / name if multiple_archives else staged
            if multiple_archives:
                extracted.mkdir()
            archive = scratch / "archive.7z"
            if progress:
                progress(f"Reconstructing {name}.7z from {len(parts)} part(s)...")
            with archive.open("wb") as writer:
                for source in parts:
                    _decode_file(source, writer, checksum=False)
            try:
                if progress:
                    progress(f"Checking archive entries in {name}.7z...")
                listing = _run_7z(executable, ["l", "-slt", "-sccUTF-8", "--", str(archive)])
                _validate_members(listing)
                if progress:
                    progress(f"Extracting {name}.7z...")
                _run_7z(
                    executable,
                    ["x", f"-o{extracted}", "-sccUTF-8", "-bd", "-y", "--", str(archive)],
                )
            except BundleError as exc:
                raise BundleError(
                    f"Could not recover {name}.7z from {parts[0].parent}: {exc}"
                ) from exc
            archive.unlink()
            result_output = destination / name if multiple_archives else destination
            results.append(DecodeResult(name, result_output, len(parts)))
    return results
