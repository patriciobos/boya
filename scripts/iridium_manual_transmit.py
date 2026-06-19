#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any

from modules.iridium_LL import IridiumLowLevel
from modules.support.iridium_payloads import (
    AudioProcPayloadUnavailable,
    build_current_system_status_payload,
    build_latest_audio_proc_payload,
)
from modules.support.iridium_protocol import (
    SYSTEM_STATUS_PAYLOAD_SIZE,
    decode_message,
    expected_audio_band_count,
)
from modules.support.system_config import get_logs_path, utc_minus_3_timestamp

MAX_IRIDIUM_SBD_BYTES = 340
DEFAULT_PREFERRED_PORT = IridiumLowLevel.DEFAULT_PREFERRED_PORT
DEFAULT_BAUDRATE = IridiumLowLevel.DEFAULT_BAUDRATE
DEFAULT_TIMEOUT = IridiumLowLevel.DEFAULT_TIMEOUT


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _decoded_summary(decoded: dict[str, Any]) -> dict[str, Any]:
    if decoded.get("message_type") == "MSG_SYSTEM_STATUS":
        return {
            "message_type": decoded.get("message_type"),
            "fsm_ok_bitmap": decoded.get("fsm_ok_bitmap"),
            "ll_ok_bitmap": decoded.get("ll_ok_bitmap"),
            "status_flags": decoded.get("status_flags"),
            "battery": decoded.get("battery"),
            "storage": decoded.get("storage"),
            "uptime_minutes": decoded.get("uptime_minutes"),
        }
    return {
        "message_type": decoded.get("message_type"),
        "message_type_name": decoded.get("message_type_name"),
        "timestamp": decoded.get("timestamp"),
        "packing": decoded.get("packing"),
        "channel_count": decoded.get("channel_count"),
        "band_count": decoded.get("band_count"),
        "crc16_ccitt_false": decoded.get("crc16_ccitt_false"),
    }


def build_system_status_report() -> dict[str, Any]:
    payload, details = build_current_system_status_payload()
    if len(payload) != SYSTEM_STATUS_PAYLOAD_SIZE:
        raise ValueError(f"MSG_SYSTEM_STATUS payload must be {SYSTEM_STATUS_PAYLOAD_SIZE} bytes")
    decoded = decode_message(payload)
    return {
        "mode": "system-status",
        "payload": payload,
        "payload_hex": payload.hex(),
        "payload_size": len(payload),
        "details": details,
        "decoded": decoded,
        "decoded_summary": _decoded_summary(decoded),
        "bitmaps": {
            "fsm_ok_bitmap": decoded["fsm_ok_bitmap"],
            "ll_ok_bitmap": decoded["ll_ok_bitmap"],
        },
        "flags": decoded["status_flags"],
    }


def build_latest_audio_report() -> dict[str, Any]:
    payload, details = build_latest_audio_proc_payload()
    expected_bands = expected_audio_band_count()
    decoded = decode_message(payload, expected_audio_band_count=expected_bands)
    if len(payload) > MAX_IRIDIUM_SBD_BYTES:
        raise ValueError(f"AudioProc payload exceeds {MAX_IRIDIUM_SBD_BYTES} bytes: {len(payload)}")
    return {
        "mode": "latest-audio",
        "payload": payload,
        "payload_hex": payload.hex(),
        "payload_size": len(payload),
        "source_file": details.get("audio_source_file") or details.get("audio_output"),
        "source_reading": details.get("audio_source_reading"),
        "timestamp": details.get("audio_timestamp"),
        "message_type": decoded.get("message_type"),
        "message_type_name": decoded.get("message_type_name"),
        "packing": decoded.get("packing"),
        "channels": decoded.get("channel_count"),
        "bands": decoded.get("band_count"),
        "details": details,
        "decoded": decoded,
        "decoded_summary": _decoded_summary(decoded),
    }


def _iridium_config() -> dict[str, Any]:
    preferred_port = os.getenv("PREFERRED_PORT", DEFAULT_PREFERRED_PORT)
    try:
        baudrate = int(os.getenv("IRIDIUM_BAUDRATE", str(DEFAULT_BAUDRATE)))
    except ValueError:
        baudrate = DEFAULT_BAUDRATE
    try:
        timeout = float(os.getenv("IRIDIUM_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    return {
        "preferred_port": preferred_port,
        "baudrate": baudrate,
        "timeout": timeout,
        "show_ports": True,
    }


def send_payload_via_iridium(
    payload: bytes,
    *,
    max_attempts: int,
    retry_delay_s: float,
    clear_after_success: bool,
) -> dict[str, Any]:
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if len(payload) > MAX_IRIDIUM_SBD_BYTES:
        raise ValueError(f"Iridium binary payload exceeds {MAX_IRIDIUM_SBD_BYTES} bytes: {len(payload)}")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if retry_delay_s < 0:
        raise ValueError("retry_delay_s must be >= 0")

    modem = IridiumLowLevel(**_iridium_config())
    report: dict[str, Any] = {
        "mode": "binary",
        "payload_size": len(payload),
        "max_attempts": max_attempts,
        "retry_delay_s": retry_delay_s,
        "clear_after_success": clear_after_success,
        "ok": False,
        "result": None,
        "errors": [],
    }
    try:
        if not modem.init():
            error = modem.last_error or "Iridium modem initialization failed"
            report["errors"].append(error)
            return report
        ok, details = modem.send_sbd_binary(
            payload,
            clear_after_success=clear_after_success,
            max_attempts=max_attempts,
            retry_delay_s=retry_delay_s,
        )
        report["ok"] = bool(ok)
        report["result"] = details
        if not ok and isinstance(details, dict):
            report["errors"].extend(details.get("errors") or [])
        return report
    finally:
        report["deinit_ok"] = modem.deinit()


def log_manual_attempt(entry: dict[str, Any]) -> None:
    path = get_logs_path() / "iridium_manual_transmit.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=_json_default) + "\n")


def _print_text(report: dict[str, Any], send_result: dict[str, Any] | None) -> None:
    print(f"mode: {report['mode']}")
    if report.get("source_file"):
        print(f"source: {report['source_file']}")
    if report.get("timestamp"):
        print(f"timestamp: {report['timestamp']}")
    print(f"payload_hex: {report['payload_hex']}")
    print(f"payload_size: {report['payload_size']}")
    if report["mode"] == "system-status":
        print("decoded_message:")
        print(json.dumps(report["decoded"], indent=2, ensure_ascii=False, default=_json_default))
        print("bitmaps_flags:")
        print(json.dumps({"bitmaps": report["bitmaps"], "flags": report["flags"]}, indent=2, ensure_ascii=False))
    else:
        print(f"message_type: {report['message_type']}")
        print(f"packing: {report['packing']}")
        print(f"channels: {report['channels']}")
        print(f"bands: {report['bands']}")
        print("decoded_summary:")
        print(json.dumps(report["decoded_summary"], indent=2, ensure_ascii=False, default=_json_default))
    if send_result is not None:
        print("send_result:")
        print(json.dumps(send_result, indent=2, ensure_ascii=False, default=_json_default))


def _output_report(args: argparse.Namespace, report: dict[str, Any], send_result: dict[str, Any] | None) -> None:
    if args.hex_only:
        print(report["payload_hex"])
        return
    output = {key: value for key, value in report.items() if key != "payload"}
    output["dry_run"] = not args.send
    if send_result is not None:
        output["send_result"] = send_result
    if args.json:
        print(json.dumps(output, indent=2, ensure_ascii=False, default=_json_default))
        return
    _print_text(report, send_result)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely build and optionally transmit Iridium binary payloads.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("system-status", "latest-audio"):
        subparser = subparsers.add_parser(name)
        mode = subparser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", default=True, help="Build and validate only. Default.")
        mode.add_argument("--send", action="store_true", help="Transmit the payload through the Iridium modem.")
        subparser.add_argument("--max-attempts", type=int, default=1)
        subparser.add_argument("--retry-delay-s", type=float, default=5.0)
        subparser.add_argument("--clear-after-success", action="store_true")
        subparser.add_argument("--json", action="store_true")
        subparser.add_argument("--hex-only", action="store_true")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    send_result = None
    error = None
    report: dict[str, Any] | None = None
    exit_code = 0

    try:
        if args.command == "system-status":
            report = build_system_status_report()
        elif args.command == "latest-audio":
            report = build_latest_audio_report()
        else:
            raise ValueError(f"unsupported command: {args.command}")

        if args.send:
            send_result = send_payload_via_iridium(
                report["payload"],
                max_attempts=args.max_attempts,
                retry_delay_s=args.retry_delay_s,
                clear_after_success=args.clear_after_success,
            )
            if not send_result.get("ok"):
                exit_code = 1
        _output_report(args, report, send_result)
    except (AudioProcPayloadUnavailable, FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        error = str(exc)
        exit_code = 2
        print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
    finally:
        log_entry = {
            "timestamp": utc_minus_3_timestamp(),
            "mode": args.command,
            "dry_run": not args.send,
            "send": bool(args.send),
            "payload_size": None if report is None else report.get("payload_size"),
            "payload_hex": None if report is None else report.get("payload_hex"),
            "decoded_summary": None if report is None else report.get("decoded_summary"),
            "send_result": send_result,
            "error": error,
        }
        log_manual_attempt(log_entry)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
