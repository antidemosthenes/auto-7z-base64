import os
import re
import stat
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .errors import BundleError
from .prefetch import PrefetchPlan

PART_PATTERN = re.compile(r"^(.+)\.7z\.([0-9]+)\.txt$", re.IGNORECASE)
ARCHIVE_PATTERN = re.compile(r"^(.+)\.7z$", re.IGNORECASE)
INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"
}


def _valid_name(name: str) -> None:
    try:
        units = len(name.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise BundleError(f"Filename contains invalid Unicode: {name!r}") from exc
    if (
        not name
        or name in {".", ".."}
        or INVALID_NAME.search(name)
        or name.endswith((".", " "))
        or name.split(".")[0].rstrip(" ").upper() in RESERVED_NAMES
        or units > 255
    ):
        raise BundleError(f"Unsupported filename or archive name: {name!r}.")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _check_regular(path: Path) -> bool:
    return _check_stat(path, path.lstat())


def _check_stat(path: Path | str, info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    ):
        raise BundleError(f"Links and reparse points are unsupported: {path}")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise BundleError(f"Unsupported input type: {path}")
    return stat.S_ISDIR(info.st_mode)


def _source_paths(inputs: Sequence[Path], prefetch: PrefetchPlan | None = None) -> list[Path]:
    if not inputs:
        raise BundleError("Provide at least one input file or folder.")
    paths = [_absolute(Path(path)) for path in inputs]
    root_names: set[str] = set()
    directories = []
    for path in paths:
        _valid_name(path.name)
        if path.name.casefold() in root_names:
            raise BundleError(f"Conflicting input names: {path.name}")
        root_names.add(path.name.casefold())
        info = path.lstat()
        if _check_stat(path, info):
            directories.append(path)
        elif prefetch is not None:
            prefetch.add(path, info)
    canonical = {path.resolve(): path for path in paths}
    for resolved, original in canonical.items():
        for parent in resolved.parents:
            if parent in canonical:
                raise BundleError(f"Overlapping inputs: {canonical[parent]} and {original}")
    while directories:
        directory = directories.pop()
        names = set()
        with os.scandir(directory) as entries:
            for entry in entries:
                _valid_name(entry.name)
                folded = entry.name.casefold()
                if folded in names:
                    raise BundleError(f"Case-insensitive filename collision in {directory}")
                names.add(folded)
                info = entry.stat(follow_symlinks=False)
                if _check_stat(entry.path, info):
                    directories.append(Path(entry.path))
                elif prefetch is not None:
                    prefetch.add(entry.path, info)
    return paths


def _empty_destination(output: Path) -> None:
    if output.is_symlink():
        raise BundleError(f"Output cannot be a link: {output}")
    if output.exists():
        if not _check_regular(output) or any(output.iterdir()):
            raise BundleError(f"Output must be a new or empty directory: {output}")


@contextmanager
def _transaction(output: Path) -> Iterator[tuple[Path, Path]]:
    _empty_destination(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temporary:
        scratch = Path(temporary)
        staged = scratch / "result"
        staged.mkdir()
        yield staged, scratch
        _empty_destination(output)
        existed = output.exists()
        if existed:
            output.rmdir()
        try:
            staged.rename(output)
        except OSError:
            if existed and not output.exists():
                output.mkdir()
            raise


def _part_number(digits: str, path: Path) -> int:
    number = int(digits)
    if number < 1 or digits != f"{number:03d}":
        raise BundleError(f"Invalid part number: {path}")
    return number


def _archive_hint(path: Path) -> str:
    if part := PART_PATTERN.fullmatch(path.name):
        _part_number(part[2], path)
        return part[1]
    if archive := ARCHIVE_PATTERN.fullmatch(path.name):
        return archive[1]
    raise BundleError(f"Expected a folder, name.7z, or name.7z.NNN.txt: {path}")


@dataclass
class _FolderSelection:
    all_archives: bool = False
    names: dict[str, str] = field(default_factory=dict)


def _folder_selections(inputs: Sequence[Path]) -> dict[Path, _FolderSelection]:
    if not inputs:
        raise BundleError("Provide a folder, a .7z name, or a numbered TXT part name.")
    selections: dict[Path, _FolderSelection] = {}
    for raw_path in inputs:
        path = _absolute(Path(raw_path))
        try:
            directory = _check_regular(path)
        except FileNotFoundError:
            directory = False
        if directory:
            selections.setdefault(path, _FolderSelection()).all_archives = True
        else:
            name = _archive_hint(path)
            _valid_name(name)
            selection = selections.setdefault(path.parent, _FolderSelection())
            selection.names[name.casefold()] = name
    return selections


def _scan_part_directory(
    folder: Path, selection: _FolderSelection
) -> Iterator[tuple[str, int, Path]]:
    if not _check_regular(folder):
        raise BundleError(f"Expected a folder containing TXT parts: {folder}")
    missing = selection.names.copy()
    with os.scandir(folder) as entries:
        for entry in entries:
            match = PART_PATTERN.fullmatch(entry.name)
            if not match:
                continue
            name, digits = match.groups()
            key = name.casefold()
            if not selection.all_archives and key not in selection.names:
                continue
            _valid_name(name)
            if _check_stat(entry.path, entry.stat(follow_symlinks=False)):
                raise BundleError(f"Expected a TXT file: {entry.path}")
            path = Path(entry.path)
            missing.pop(key, None)
            yield name, _part_number(digits, path), path
    if missing:
        name = next(iter(missing.values()))
        raise BundleError(f"No TXT parts found for archive {name} in folder {folder}")


def _collect_parts(inputs: Sequence[Path]) -> dict[str, list[Path]]:
    selections = _folder_selections(inputs)
    groups: dict[str, tuple[Path, dict[int, Path]]] = {}
    for folder, selection in selections.items():
        for name, number, path in _scan_part_directory(folder, selection):
            previous_folder, parts = groups.setdefault(name.casefold(), (folder, {}))
            if previous_folder != folder:
                raise BundleError(
                    f"Conflicting parts for archive {name} in folders {previous_folder} and {folder}"
                )
            if number in parts:
                raise BundleError(f"Duplicate part {number:03d} for {name} in {folder}")
            parts[number] = path
    if not groups:
        folders = ", ".join(map(str, selections))
        raise BundleError(f"No named .7z.NNN.txt parts were found in: {folders}")
    collected = {}
    for folder, parts in groups.values():
        numbers = sorted(parts)
        name = parts[numbers[0]].name.rsplit(".", 3)[0]
        for expected, actual in enumerate(numbers, 1):
            if expected != actual:
                raise BundleError(f"Missing part {expected:03d} for {name} in {folder}")
        collected[name] = [parts[number] for number in numbers]
    return dict(sorted(collected.items()))
