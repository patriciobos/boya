#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime
from pathlib import Path

# Ensure repo root is on PYTHONPATH when running as a script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.support.iridium_protocol import decode_message


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _payload_from_args(args: argparse.Namespace) -> bytes:
    selected = [
        args.hex_payload is not None,
        args.base64_payload is not None,
        args.file is not None,
    ]
    if sum(selected) != 1:
        raise ValueError("select exactly one input: --hex, --base64, or --file")
    if args.hex_payload is not None:
        return bytes.fromhex(args.hex_payload.strip())
    if args.base64_payload is not None:
        return base64.b64decode(args.base64_payload.strip(), validate=True)
    return Path(args.file).read_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decode a binary Iridium payload as JSON."
    )
    parser.add_argument("--hex", dest="hex_payload", help="Payload as a hex string")
    parser.add_argument(
        "--base64", dest="base64_payload", help="Payload as a base64 string"
    )
    parser.add_argument("--file", help="Path to a binary payload file")
    parser.add_argument(
        "--expected-audio-band-count",
        type=int,
        default=None,
        help="Expected AudioProc band count for validating audio payloads",
    )
    args = parser.parse_args(argv)

    try:
        payload = _payload_from_args(args)
        decoded = decode_message(
            payload, expected_audio_band_count=args.expected_audio_band_count
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps(decoded, indent=2, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
