import argparse
import hashlib
import json
import random
import re
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from auto_7z_base64.cli import limit_argument
from auto_7z_base64.core import decode, encode
from auto_7z_base64.filesystem import _collect_parts
from auto_7z_base64.sevenzip import find_sevenzip

LARGE_WORKLOAD_SEED = 20261119


def _tree_snapshot(root: Path) -> dict[str, tuple[bool, int, str | None]]:
    snapshot = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[relative] = (True, 0, None)
        else:
            with path.open("rb") as reader:
                digest = hashlib.file_digest(reader, "sha256").hexdigest()
            snapshot[relative] = (False, path.stat().st_size, digest)
    return snapshot


def _make_workloads(root: Path, small_files: int, small_file_bytes: int, large_bytes: int):
    small = root / "many-small-files"
    small.mkdir()
    for index in range(small_files):
        content = (
            f"module={index}\n" + "def calculate(value):\n    return value * 17 + 3\n" * 32
        ).encode("utf-8")
        repeats = (small_file_bytes + len(content) - 1) // len(content)
        (small / f"module-{index:05d}.py").write_bytes((content * repeats)[:small_file_bytes])

    large = root / "one-large-file"
    large.mkdir()
    rng = random.Random(LARGE_WORKLOAD_SEED)
    with (large / "random.bin").open("wb") as writer:
        remaining = large_bytes
        while remaining:
            chunk = rng.randbytes(min(1024 * 1024, remaining))
            writer.write(chunk)
            remaining -= len(chunk)
    return (("many-small-files", small), ("one-large-file", large))


def _measure_discovery(
    archive_hint: Path, part_hints: list[Path], iterations: int
) -> dict[str, object]:
    timings = {"single_archive_filename_hint": [], "all_part_filename_hints": []}
    expected = None
    for _ in range(iterations):
        for key, inputs in (
            ("single_archive_filename_hint", [archive_hint]),
            ("all_part_filename_hints", part_hints),
        ):
            start = time.perf_counter()
            groups = _collect_parts(inputs)
            timings[key].append(time.perf_counter() - start)
            signature = {name: [path.name for path in parts] for name, parts in groups.items()}
            if expected is None:
                expected = signature
            elif signature != expected:
                raise RuntimeError("Part discovery returned different groups for equivalent hints.")
    return {key: statistics.median(values) for key, values in timings.items()}


def _base64_decoded_size(path: Path) -> int:
    encoded_size = path.stat().st_size
    with path.open("rb") as reader:
        reader.seek(max(0, encoded_size - 2))
        padding = reader.read().count(b"=")
    return encoded_size // 4 * 3 - padding


def _sevenzip_version(executable: Path) -> str:
    result = subprocess.run(
        [str(executable), "i"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    match = re.search(r"7-Zip[^\r\n]*?(\d+\.\d+(?:\.\d+)?)", result.stdout)
    return match.group(1) if match else result.stdout.splitlines()[0].strip()


def _run(args: argparse.Namespace) -> dict[str, object]:
    executable = find_sevenzip()
    measurements: dict[str, object] = {}
    report: dict[str, object] = {
        "sevenzip_path": str(executable),
        "sevenzip_version": _sevenzip_version(executable),
        "compression_level": 9,
        "solid_mode": True,
        "large_workload_seed": LARGE_WORKLOAD_SEED,
        "text_limit_bytes": args.limit,
        "iterations": args.iterations,
        "workloads": measurements,
    }
    with tempfile.TemporaryDirectory(prefix="auto-7z-base64-benchmark-") as temporary:
        root = Path(temporary)
        workloads = _make_workloads(root, args.small_files, args.small_file_bytes, args.large_bytes)
        for label, source in workloads:
            source_bytes = sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
            source_files = sum(1 for path in source.rglob("*") if path.is_file())
            encoded = root / f"encoded-{label}"
            recovered = root / f"recovered-{label}"

            start = time.perf_counter()
            result = encode([source], encoded, args.limit, label)
            encode_seconds = time.perf_counter() - start

            part_files = sorted(encoded.glob("*.txt"))
            archive_bytes = sum(_base64_decoded_size(part) for part in part_files)
            archive_hint = encoded / f"{label}.7z"
            discovery = _measure_discovery(archive_hint, part_files, args.iterations)

            start = time.perf_counter()
            decode([archive_hint], recovered)
            decode_seconds = time.perf_counter() - start

            recovered_root = recovered / label
            if _tree_snapshot(source) != _tree_snapshot(recovered_root):
                raise RuntimeError(f"Round-trip verification failed for {label}.")

            measurements[label] = {
                "source_files": source_files,
                "source_bytes": source_bytes,
                "archive_bytes": archive_bytes,
                "parts": result.parts,
                "encoded_text_bytes": sum(path.stat().st_size for path in part_files),
                "encode_seconds": encode_seconds,
                "decode_seconds": decode_seconds,
                "round_trip_verified": True,
                "median_discovery_seconds": discovery,
            }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark local encode/decode and equivalent 7z part discovery hints."
    )
    parser.add_argument("--small-files", type=int, default=256)
    parser.add_argument("--small-file-bytes", type=int, default=2048)
    parser.add_argument("--large-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--limit", type=limit_argument, default="256KiB")
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    if args.small_files < 1 or args.small_file_bytes < 1 or args.large_bytes < 1:
        parser.error("workload sizes must be positive")
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    print(json.dumps(_run(args), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
