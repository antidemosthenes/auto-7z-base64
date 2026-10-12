import argparse
import sys
from io import TextIOWrapper
from pathlib import Path

from .core import (
    DEFAULT_DECODE_OUTPUT,
    DEFAULT_ENCODE_OUTPUT,
    DEFAULT_LIMIT,
    BundleError,
    decode,
    encode,
    parse_limit,
)


def limit_argument(value: str) -> int:
    try:
        return parse_limit(value)
    except BundleError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(
        prog="auto-7z-base64",
        description="Transfer binary data as size-limited Base64 parts and recover it.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    encode_parser = commands.add_parser(
        "encode", help="Compress files/folders into plain Base64 TXT parts."
    )
    encode_parser.add_argument("inputs", type=Path, nargs="+")
    encode_parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_ENCODE_OUTPUT,
        help="New or empty output folder; default: encode-result in the current directory.",
    )
    encode_parser.add_argument(
        "--limit",
        metavar="SIZE",
        type=limit_argument,
        default=DEFAULT_LIMIT,
        help=(
            "Byte budget per plain Base64 TXT part; its size is strictly below it. "
            "Integer bytes, B, KB/MB/GB (decimal), or KiB/MiB/GiB (binary). "
            "Examples: 2000000, 2MB, 2MiB, 2048KB. K/M/G are binary aliases. "
            "Default: 2MB (2000000 bytes)."
        ),
    )
    encode_parser.add_argument("--name", help="Archive name; default: input name or bundle.")
    decode_parser = commands.add_parser("decode", help="Restore and extract TXT parts.")
    decode_parser.add_argument("inputs", type=Path, nargs="+")
    decode_parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_DECODE_OUTPUT,
        help="New or empty output folder; default: decode-result in the current directory.",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "encode":
            encoded = encode(args.inputs, args.output, args.limit, args.name)
            print(f"Encoded {encoded.parts} part(s).")
            print(
                f"Binary volume limit: {encoded.volume_bytes:,} bytes; text budget: {args.limit:,} bytes."
            )
            print(f'Encoded TXT files saved to: "{encoded.output}"')
        else:
            results = decode(args.inputs, args.output)
            for result in results:
                print(f"Recovered {result.name}: {result.parts} part(s).")
                print(f'Decoded files saved to: "{result.output}"')
    except (BundleError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled; temporary output removed.", file=sys.stderr)
        return 130
    return 0
