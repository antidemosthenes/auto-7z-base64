import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath

from .errors import BundleError
from .filesystem import _absolute, _valid_name


def find_sevenzip() -> Path:
    if sys.platform != "win32":
        raise BundleError("auto-7z-base64 supports Windows only.")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        folder = entry.strip('"')
        if folder:
            executable = _absolute(Path(os.path.expandvars(folder)) / "7z.exe")
            if executable.is_file():
                return executable
    raise BundleError(
        "7-Zip is required and 7z.exe was not found on PATH. "
        "Install 7-Zip, add the folder containing 7z.exe to PATH, and restart your terminal."
    )


def _run_7z(executable: Path, arguments: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            [str(executable), *arguments],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="strict",
        )
    except UnicodeDecodeError as exc:
        raise BundleError(
            "7-Zip returned invalid UTF-8; archive names cannot be verified."
        ) from exc
    if result.returncode != 0:
        if result.returncode == 7:
            raise BundleError(
                "7-Zip rejected the command-line options. Use 7-Zip 21.07 or newer.\n"
                + result.stdout[-4000:].strip()
            )
        raise BundleError(
            f"7-Zip failed (exit {result.returncode}):\n{result.stdout[-4000:].strip()}"
        )
    return result.stdout


def _validate_members(listing: str) -> None:
    lines = listing.replace("\r\n", "\n").split("\n")
    try:
        start = lines.index("----------")
    except ValueError as exc:
        raise BundleError("Cannot read the 7-Zip archive listing.") from exc
    if "Type = 7z" not in lines[:start]:
        raise BundleError("Decoded volumes must contain a 7z archive.")
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    for line in [*lines[start + 1 :], ""]:
        if not line:
            if record:
                records.append(record)
                record = {}
            continue
        key, separator, value = line.partition(" = ")
        if not separator or key in record:
            raise BundleError("Unsupported or ambiguous archive listing.")
        record[key] = value
    entries: dict[str, bool] = {}
    for record in records:
        if "Path" not in record:
            raise BundleError("Archive entry has no path.")
        path = record["Path"]
        if not path or path.startswith(("/", "\\")) or PureWindowsPath(path).drive:
            raise BundleError(f"Unsafe archive path: {path!r}")
        components = path.replace("\\", "/").split("/")
        for component in components:
            _valid_name(component)
        if any("link" in key.lower() or "reparse" in key.lower() for key in record):
            raise BundleError(f"Archive links are unsupported: {path}")
        attributes = record.get("Attributes", "")
        if any(token.startswith("l") for token in attributes.split()):
            raise BundleError(f"Archive links are unsupported: {path}")
        directory = record.get("Folder") == "+" or attributes.startswith("D")
        normalized = "/".join(components).casefold()
        if normalized in entries:
            raise BundleError(f"Conflicting archive paths: {path}")
        entries[normalized] = directory
    for path in entries:
        components = path.split("/")
        for depth in range(1, len(components)):
            parent = "/".join(components[:depth])
            if parent in entries and not entries[parent]:
                raise BundleError(f"Archive file also used as a directory: {parent}")
