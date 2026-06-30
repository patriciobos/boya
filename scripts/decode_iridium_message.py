#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import math
import re
import struct
import sys
import textwrap
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure repo root is on PYTHONPATH when running as a script.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.support.iridium_protocol import MODULE_ORDER, decode_message

A4_LANDSCAPE = (841.89, 595.28)
A4_PORTRAIT = (595.28, 841.89)
PAGE_MARGIN = 34.0
STATUS_FLAG_ORDER = (
    "storage_unavailable",
    "storage_not_writable",
    "storage_warning",
    "storage_critical",
    "storage_quota_exceeded",
    "battery_warning",
    "battery_critical",
    "last_acquisition_incomplete",
)


@dataclass(frozen=True)
class _DecodedPayload:
    filename: str
    imei: str | None
    momsn: int | None
    size_bytes: int
    ok: bool
    kind: str
    observation: str
    decoded: dict[str, Any] | None
    error: str | None


class _PdfPage:
    def __init__(
        self, *, width: float = A4_LANDSCAPE[0], height: float = A4_LANDSCAPE[1]
    ):
        self.width = width
        self.height = height
        self._commands: list[str] = []

    def rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        fill: tuple[float, float, float] | None = None,
        stroke: tuple[float, float, float] | None = None,
    ) -> None:
        if fill is not None:
            self._commands.append(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg")
            self._commands.append(f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re f")
        if stroke is not None:
            self._commands.append(f"{stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG")
            self._commands.append(f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re S")

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        color: tuple[float, float, float] = (0.55, 0.58, 0.62),
    ) -> None:
        self._commands.append(f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG")
        self._commands.append(f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def text(
        self,
        x: float,
        y: float,
        text: str,
        *,
        size: float = 10.0,
        font: str = "F1",
        color: tuple[float, float, float] = (0.10, 0.12, 0.16),
    ) -> None:
        safe = _pdf_escape(_ascii_text(text))
        self._commands.append(f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg")
        self._commands.append(
            f"BT /{font} {size:.1f} Tf {x:.2f} {y:.2f} Td ({safe}) Tj ET"
        )

    def wrapped_text(
        self,
        x: float,
        y: float,
        text: str,
        *,
        width_chars: int,
        size: float = 9.0,
        leading: float = 11.0,
        font: str = "F1",
        max_lines: int | None = None,
        color: tuple[float, float, float] = (0.10, 0.12, 0.16),
    ) -> float:
        lines = textwrap.wrap(
            _ascii_text(text),
            width=width_chars,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        if max_lines is not None and len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1][: max(0, width_chars - 3)].rstrip() + "..."
        for index, line in enumerate(lines):
            self.text(x, y - index * leading, line, size=size, font=font, color=color)
        return y - len(lines) * leading

    def content(self) -> bytes:
        return ("\n".join(self._commands) + "\n").encode("latin-1", errors="replace")


def _json_default(value: Any) -> str:
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


def _ascii_text(value: Any) -> str:
    text = "" if value is None else str(value)
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.encode("ascii", errors="ignore").decode("ascii")


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _write_single_page_pdf(path: Path, page: _PdfPage) -> None:
    content = page.content()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page.width:.2f} "
            f"{page.height:.2f}] /Resources << /Font << /F1 5 0 R "
            "/F2 6 0 R /F3 7 0 R >> >> /Contents 4 0 R >>"
        ).encode("ascii"),
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    ]

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(bytes(output))


def _metadata_from_filename(path: Path) -> tuple[str | None, int | None]:
    match = re.search(r"_(\d{15})_(\d{6})\.sbd$", path.name)
    if match is None:
        return None, None
    return match.group(1), int(match.group(2))


def _message_kind(decoded: dict[str, Any]) -> str:
    message_type = decoded.get("message_type")
    if message_type == "MSG_SYSTEM_STATUS":
        return "Estado del sistema"
    if message_type == "MSG_BOOT":
        return "Arranque"
    if decoded.get("message_type_name") == "MSG_AUDIO" or message_type in (3, 4, 5, 6):
        return "AudioProc"
    return f"Tipo {message_type}"


def _active_flags(decoded: dict[str, Any]) -> list[str]:
    flags = decoded.get("status_flags")
    if not isinstance(flags, dict):
        return []
    return [name for name, active in flags.items() if active]


def _audio_values(decoded: dict[str, Any]) -> list[float]:
    values: list[float] = []
    rows = decoded.get("relative_band_power_db")
    if not isinstance(rows, list):
        return values
    for row in rows:
        row_values = row if isinstance(row, list) else [row]
        for value in row_values:
            if value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric):
                values.append(numeric)
    return values


def _observation(decoded: dict[str, Any]) -> str:
    if decoded.get("message_type") == "MSG_SYSTEM_STATUS":
        battery = decoded.get("battery", {})
        storage = decoded.get("storage", {})
        flags = _active_flags(decoded)
        flag_text = ", ".join(flags) if flags else "sin flags activos"
        return (
            f"Vbat={battery.get('voltage_v')} V; "
            f"SoC={battery.get('soc_percent')}%; "
            f"libre={storage.get('free_gib')} GiB; {flag_text}"
        )
    if decoded.get("message_type") == "MSG_BOOT":
        return f"Uptime al arranque: {decoded.get('uptime_minutes')} min"
    if decoded.get("message_type_name") == "MSG_AUDIO":
        values = _audio_values(decoded)
        if values:
            return (
                f"{decoded.get('band_count')} bandas; "
                f"{decoded.get('channel_count')} canal(es); "
                f"rango {min(values):.1f} a {max(values):.1f} dB"
            )
        return (
            f"{decoded.get('band_count')} bandas; "
            f"{decoded.get('channel_count')} canal(es); "
            f"packing={decoded.get('packing')}"
        )
    return "Decodificado correctamente"


def _decode_payload_file(
    path: Path, *, expected_audio_band_count: int | None
) -> _DecodedPayload:
    imei, momsn = _metadata_from_filename(path)
    payload = path.read_bytes()
    try:
        decoded = decode_message(
            payload, expected_audio_band_count=expected_audio_band_count
        )
    except (ValueError, struct.error) as exc:
        return _DecodedPayload(
            filename=path.name,
            imei=imei,
            momsn=momsn,
            size_bytes=len(payload),
            ok=False,
            kind="No decodificado",
            observation=str(exc),
            decoded=None,
            error=str(exc),
        )

    return _DecodedPayload(
        filename=path.name,
        imei=imei,
        momsn=momsn,
        size_bytes=len(payload),
        ok=True,
        kind=_message_kind(decoded),
        observation=_observation(decoded),
        decoded=decoded,
        error=None,
    )


def _row_to_json(row: _DecodedPayload) -> dict[str, Any]:
    return {
        "filename": row.filename,
        "imei": row.imei,
        "momsn": row.momsn,
        "size_bytes": row.size_bytes,
        "ok": row.ok,
        "kind": row.kind,
        "observation": row.observation,
        "error": row.error,
        "decoded": row.decoded,
    }


def _draw_header(page: _PdfPage, title: str, subtitle: str) -> None:
    page.rect(0, page.height - 86, page.width, 86, fill=(0.08, 0.13, 0.20))
    page.text(PAGE_MARGIN, page.height - 38, title, size=19, font="F2", color=(1, 1, 1))
    page.text(
        PAGE_MARGIN,
        page.height - 60,
        subtitle,
        size=9.5,
        color=(0.82, 0.88, 0.95),
    )


def _draw_card(
    page: _PdfPage,
    x: float,
    y: float,
    width: float,
    height: float,
    label: str,
    value: str,
    *,
    accent: tuple[float, float, float] = (0.10, 0.42, 0.72),
) -> None:
    page.rect(x, y, width, height, fill=(0.96, 0.97, 0.98), stroke=(0.78, 0.82, 0.86))
    page.rect(x, y + height - 4, width, 4, fill=accent)
    page.text(
        x + 10,
        y + height - 18,
        label.upper(),
        size=7.2,
        font="F2",
        color=(0.34, 0.39, 0.46),
    )
    page.wrapped_text(
        x + 10, y + 9, value, width_chars=20, size=12, font="F2", max_lines=1
    )


def _draw_key_values(
    page: _PdfPage,
    x: float,
    y: float,
    rows: list[tuple[str, str]],
    *,
    width: float,
    row_height: float = 21.0,
) -> float:
    page.rect(
        x,
        y - len(rows) * row_height + 4,
        width,
        len(rows) * row_height + 8,
        stroke=(0.82, 0.85, 0.88),
    )
    current_y = y
    for label, value in rows:
        page.text(
            x + 8, current_y, label, size=7.8, font="F2", color=(0.35, 0.39, 0.46)
        )
        page.wrapped_text(
            x + 116, current_y, value, width_chars=56, size=8.2, max_lines=1
        )
        current_y -= row_height
    return current_y


def _message_type_label(row: _DecodedPayload) -> str:
    if row.decoded is None:
        return "No decodificado"
    name = row.decoded.get("message_type_name") or row.decoded.get("message_type")
    byte = row.decoded.get("message_type_byte", row.decoded.get("message_type"))
    if isinstance(byte, int):
        return f"{name} (0x{byte:02X})"
    return str(name)


def _payload_highlights(row: _DecodedPayload) -> list[tuple[str, str]]:
    base = [("Message type", _message_type_label(row))]
    if not row.ok or row.decoded is None:
        return base + [
            ("Resultado", "Error de decodificacion"),
            ("Error", row.error or ""),
        ]

    decoded = row.decoded
    if row.kind == "Estado del sistema":
        battery = decoded.get("battery", {})
        storage = decoded.get("storage", {})
        flags = _active_flags(decoded)
        return base + [
            (
                "Bateria",
                f"{battery.get('voltage_v')} V / {battery.get('soc_percent')}%",
            ),
            ("Almacenamiento", f"{storage.get('free_gib')} GiB libres"),
            ("Uptime", f"{decoded.get('uptime_minutes')} min"),
            ("Flags activos", ", ".join(flags) if flags else "sin flags activos"),
            ("FSM bitmap", f"0x{int(decoded.get('fsm_ok_bitmap', 0)):02X}"),
            ("LL bitmap", f"0x{int(decoded.get('ll_ok_bitmap', 0)):02X}"),
        ]
    if row.kind == "AudioProc":
        values = _audio_values(decoded)
        stats = "sin valores numericos"
        if values:
            stats = (
                f"min {min(values):.1f} dB / prom {sum(values) / len(values):.1f} dB / "
                f"max {max(values):.1f} dB"
            )
        return base + [
            ("Timestamp", str(decoded.get("timestamp"))),
            ("Canales", str(decoded.get("channel_count"))),
            ("Bandas", str(decoded.get("band_count"))),
            ("Packing", str(decoded.get("packing"))),
            ("Potencia relativa", stats),
            ("CRC", f"0x{int(decoded.get('crc16_ccitt_false', 0)):04X}"),
        ]
    if row.kind == "Arranque":
        return base + [("Uptime", f"{decoded.get('uptime_minutes')} min")]
    return base + [("Tipo", row.kind), ("Observacion", row.observation)]


def _draw_table(
    page: _PdfPage,
    x: float,
    y: float,
    headers: list[str],
    rows: list[list[str]],
    *,
    col_widths: list[float],
    row_height: float,
    font_size: float = 6.0,
    header_size: float = 6.2,
    title: str | None = None,
) -> float:
    if title:
        page.text(x, y, title, size=9.5, font="F2")
        y -= 14

    table_width = sum(col_widths)
    table_height = row_height * (len(rows) + 1)
    page.rect(
        x,
        y - table_height + 2,
        table_width,
        table_height + 2,
        stroke=(0.76, 0.80, 0.84),
    )
    page.rect(x, y - row_height + 2, table_width, row_height, fill=(0.90, 0.92, 0.95))

    current_x = x
    for header, width in zip(headers, col_widths):
        page.text(
            current_x + 3, y - row_height + 7, header, size=header_size, font="F2"
        )
        current_x += width

    for row_index, row in enumerate(rows):
        row_y = y - row_height * (row_index + 2) + 7
        if row_index % 2 == 1:
            page.rect(x, row_y - 4, table_width, row_height, fill=(0.97, 0.98, 0.99))
        current_x = x
        for value, width in zip(row, col_widths):
            page.wrapped_text(
                current_x + 3,
                row_y,
                value,
                width_chars=max(4, int(width / (font_size * 0.62))),
                size=font_size,
                leading=font_size + 1,
                max_lines=1,
            )
            current_x += width
    return y - table_height - 8


def _byte_bit_rows(value: int, names: list[str]) -> list[list[str]]:
    raw = int(value) & 0xFF
    rows = []
    for bit in range(7, -1, -1):
        name = names[bit] if bit < len(names) else f"bit_{bit}"
        bit_value = 1 if raw & (1 << bit) else 0
        rows.append([str(bit), f"0x{1 << bit:02X}", name, str(bit_value)])
    return rows


def _draw_status_bit_tables(
    page: _PdfPage, row: _DecodedPayload, x: float, y: float
) -> float:
    decoded = row.decoded or {}
    fsm_value = int(decoded.get("fsm_ok_bitmap", 0))
    ll_value = int(decoded.get("ll_ok_bitmap", 0))
    flags_value = int(decoded.get("status_flags_raw", 0))

    module_names = list(MODULE_ORDER)
    flag_names = list(STATUS_FLAG_ORDER)
    col_widths = [14, 25, 101, 16]
    gap = 12
    width = sum(col_widths)
    next_y = y
    next_y = min(
        next_y,
        _draw_table(
            page,
            x,
            y,
            ["Bit", "Mask", "FSM OK field", "Val"],
            _byte_bit_rows(fsm_value, module_names),
            col_widths=col_widths,
            row_height=12,
            font_size=5.3,
            header_size=5.6,
            title=f"fsm_ok_bitmap = 0x{fsm_value:02X}",
        ),
    )
    next_y = min(
        next_y,
        _draw_table(
            page,
            x + width + gap,
            y,
            ["Bit", "Mask", "LL OK field", "Val"],
            _byte_bit_rows(ll_value, module_names),
            col_widths=col_widths,
            row_height=12,
            font_size=5.3,
            header_size=5.6,
            title=f"ll_ok_bitmap = 0x{ll_value:02X}",
        ),
    )
    next_y = min(
        next_y,
        _draw_table(
            page,
            x + 2 * (width + gap),
            y,
            ["Bit", "Mask", "Status flag", "Val"],
            _byte_bit_rows(flags_value, flag_names),
            col_widths=col_widths,
            row_height=12,
            font_size=5.3,
            header_size=5.6,
            title=f"status_flags = 0x{flags_value:02X}",
        ),
    )
    return next_y


def _audio_band_rows(row: _DecodedPayload) -> list[list[str]]:
    decoded = row.decoded or {}
    relative_power = decoded.get("relative_band_power_db")
    if not isinstance(relative_power, list):
        return []
    rows: list[list[str]] = []
    for index, values in enumerate(relative_power, start=1):
        row_values = values if isinstance(values, list) else [values]
        channel_text = []
        for channel_index, value in enumerate(row_values, start=1):
            if value is None:
                channel_text.append(f"Ch{channel_index}: null")
            else:
                channel_text.append(f"Ch{channel_index}: {float(value):.1f} dB")
        rows.append([str(index), " / ".join(channel_text)])
    return rows


def _draw_audio_bands(
    page: _PdfPage, row: _DecodedPayload, x: float, y: float
) -> float:
    rows = _audio_band_rows(row)
    if not rows:
        return y
    columns = 3
    rows_per_column = math.ceil(len(rows) / columns)
    col_widths = [30, 130]
    gap = 14
    next_y = y
    for column in range(columns):
        start = column * rows_per_column
        chunk = rows[start : start + rows_per_column]
        if not chunk:
            continue
        next_y = min(
            next_y,
            _draw_table(
                page,
                x + column * (sum(col_widths) + gap),
                y,
                ["Banda", "Potencia relativa"],
                chunk,
                col_widths=col_widths,
                row_height=13,
                font_size=6.2,
                title="Bandas AudioProc" if column == 0 else None,
            ),
        )
    return next_y


def _build_payload_pdf(row: _DecodedPayload, pdf_path: Path) -> None:
    page = _PdfPage(width=A4_PORTRAIT[0], height=A4_PORTRAIT[1])
    status = "OK" if row.ok else "ERROR"
    _draw_header(
        page,
        "Reporte tecnico de payload Iridium",
        f"{row.filename} | {status} | {_message_type_label(row)}",
    )

    accent = (0.10, 0.52, 0.32) if row.ok else (0.74, 0.18, 0.18)
    top_y = page.height - 136
    card_width = 122
    _draw_card(
        page, PAGE_MARGIN, top_y, card_width, 48, "Resultado", status, accent=accent
    )
    _draw_card(page, PAGE_MARGIN + 132, top_y, card_width, 48, "Tipo", row.kind)
    _draw_card(
        page,
        PAGE_MARGIN + 264,
        top_y,
        card_width,
        48,
        "MOMSN",
        "" if row.momsn is None else str(row.momsn),
    )
    _draw_card(
        page,
        PAGE_MARGIN + 396,
        top_y,
        card_width,
        48,
        "Bytes",
        f"{row.size_bytes}",
    )

    page.text(
        PAGE_MARGIN,
        top_y - 24,
        "Identificacion y lectura tecnica",
        size=11.0,
        font="F2",
    )
    metadata = [
        ("Archivo", row.filename),
        ("IMEI", row.imei or "no disponible"),
        ("Message type", _message_type_label(row)),
        ("Observacion", row.observation),
    ]
    after_metadata = _draw_key_values(
        page,
        PAGE_MARGIN,
        top_y - 48,
        metadata,
        width=page.width - 2 * PAGE_MARGIN,
        row_height=18,
    )

    page.text(
        PAGE_MARGIN, after_metadata - 4, "Campos decodificados", size=11.0, font="F2"
    )
    after_highlights = _draw_key_values(
        page,
        PAGE_MARGIN,
        after_metadata - 26,
        _payload_highlights(row),
        width=page.width - 2 * PAGE_MARGIN,
        row_height=17,
    )

    content_y = after_highlights - 12
    if row.kind == "Estado del sistema" and row.ok:
        content_y = _draw_status_bit_tables(page, row, PAGE_MARGIN, content_y)
    elif row.kind == "AudioProc" and row.ok:
        content_y = _draw_audio_bands(page, row, PAGE_MARGIN, content_y)

    if content_y < 64:
        page.text(
            PAGE_MARGIN,
            66,
            "Nota: contenido ajustado a una hoja; revisar JSON para valores crudos completos.",
            size=6.8,
            color=(0.74, 0.18, 0.18),
        )

    page.line(PAGE_MARGIN, 52, page.width - PAGE_MARGIN, 52)
    page.text(
        PAGE_MARGIN,
        35,
        "Generado por scripts/decode_iridium_message.py",
        size=7.2,
        color=(0.38, 0.43, 0.49),
    )
    _write_single_page_pdf(pdf_path, page)


def _status_summary(rows: list[_DecodedPayload]) -> tuple[str, str, str]:
    status_rows = [
        row.decoded
        for row in rows
        if row.ok and row.kind == "Estado del sistema" and row.decoded
    ]
    if not status_rows:
        return "sin datos", "sin datos", "sin flags"

    voltages = [
        item.get("battery", {}).get("voltage_v")
        for item in status_rows
        if item.get("battery", {}).get("voltage_v") is not None
    ]
    storage = [
        item.get("storage", {}).get("free_gib")
        for item in status_rows
        if item.get("storage", {}).get("free_gib") is not None
    ]
    flags = Counter(flag for item in status_rows for flag in _active_flags(item))
    battery_text = f"min {min(voltages):.2f} V" if voltages else "sin datos"
    storage_text = f"min {min(storage):.1f} GiB libres" if storage else "sin datos"
    flag_text = ", ".join(f"{name} ({count})" for name, count in flags.most_common(3))
    return battery_text, storage_text, flag_text or "sin flags activos"


def _audio_summary(rows: list[_DecodedPayload]) -> tuple[str, str]:
    audio_rows = [
        row for row in rows if row.ok and row.kind == "AudioProc" and row.decoded
    ]
    if not audio_rows:
        return "0 payloads", "sin datos"
    values = [value for row in audio_rows for value in _audio_values(row.decoded or {})]
    if not values:
        return f"{len(audio_rows)} payloads", "sin valores numericos"
    return (
        f"{len(audio_rows)} payloads",
        f"rango {min(values):.1f} a {max(values):.1f} dB; prom {sum(values) / len(values):.1f} dB",
    )


def _build_run_summary_pdf(rows: list[_DecodedPayload], pdf_path: Path) -> None:
    page = _PdfPage(width=A4_PORTRAIT[0], height=A4_PORTRAIT[1])
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    _draw_header(
        page, "Resumen tecnico de decodificacion Iridium", f"Corrida UTC {generated}"
    )

    total = len(rows)
    ok = sum(1 for row in rows if row.ok)
    errors = total - ok
    by_kind = Counter(row.kind for row in rows)
    by_message_type = Counter(_message_type_label(row) for row in rows)
    momsns = [row.momsn for row in rows if row.momsn is not None]
    momsn_range = f"{min(momsns)} - {max(momsns)}" if momsns else "sin datos"
    battery_text, storage_text, flag_text = _status_summary(rows)
    audio_count, audio_text = _audio_summary(rows)

    top_y = page.height - 136
    card_width = 96
    gap = 10
    cards = [
        ("Archivos", str(total), (0.10, 0.42, 0.72)),
        ("OK", str(ok), (0.10, 0.52, 0.32)),
        ("Errores", str(errors), (0.74, 0.18, 0.18) if errors else (0.10, 0.52, 0.32)),
        ("MOMSN", momsn_range, (0.10, 0.42, 0.72)),
        ("Audio", audio_count, (0.10, 0.42, 0.72)),
    ]
    for index, (label, value, accent) in enumerate(cards):
        _draw_card(
            page,
            PAGE_MARGIN + index * (card_width + gap),
            top_y,
            card_width,
            48,
            label,
            value,
            accent=accent,
        )

    page.text(PAGE_MARGIN, top_y - 26, "Hallazgos principales", size=12, font="F2")
    findings = [
        ("Decodificacion", f"{ok}/{total} payloads decodificados correctamente"),
        (
            "Tipos funcionales",
            ", ".join(f"{kind}: {count}" for kind, count in by_kind.most_common()),
        ),
        (
            "Message types",
            ", ".join(
                f"{kind}: {count}" for kind, count in by_message_type.most_common()
            ),
        ),
        ("Bateria", battery_text),
        ("Almacenamiento", storage_text),
        ("Flags de estado", flag_text),
        ("AudioProc", audio_text),
    ]
    y = _draw_key_values(
        page,
        PAGE_MARGIN,
        top_y - 50,
        findings,
        width=page.width - 2 * PAGE_MARGIN,
        row_height=19,
    )

    page.text(PAGE_MARGIN, y - 2, "Distribucion por tipo", size=12, font="F2")
    current_y = y - 24
    bar_width_max = page.width - 2 * PAGE_MARGIN - 118
    for kind, count in by_kind.most_common(8):
        bar_width = bar_width_max * (count / total) if total else 0
        page.text(PAGE_MARGIN, current_y, kind, size=8.3, font="F2")
        page.rect(
            PAGE_MARGIN + 105, current_y - 4, bar_width_max, 8, fill=(0.88, 0.90, 0.93)
        )
        page.rect(
            PAGE_MARGIN + 105, current_y - 4, bar_width, 8, fill=(0.10, 0.42, 0.72)
        )
        page.text(
            PAGE_MARGIN + 105 + bar_width_max + 8, current_y - 4, str(count), size=7.8
        )
        current_y -= 22

    if errors:
        page.text(
            PAGE_MARGIN,
            92,
            "Payloads con error",
            size=10.5,
            font="F2",
            color=(0.74, 0.18, 0.18),
        )
        error_text = "; ".join(row.filename for row in rows if not row.ok)
        page.wrapped_text(
            PAGE_MARGIN,
            76,
            error_text,
            width_chars=92,
            size=7.6,
            max_lines=2,
        )

    page.line(PAGE_MARGIN, 52, page.width - PAGE_MARGIN, 52)
    page.text(
        PAGE_MARGIN,
        35,
        "Generado por scripts/decode_iridium_message.py",
        size=7.2,
        color=(0.38, 0.43, 0.49),
    )
    _write_single_page_pdf(pdf_path, page)


def _sort_key(path: Path) -> tuple[bool, int, str]:
    _imei, momsn = _metadata_from_filename(path)
    return momsn is None, momsn or 0, path.name


def _process_input_dir(
    input_dir: Path,
    output_dir: Path,
    *,
    expected_audio_band_count: int | None,
) -> int:
    files = sorted(input_dir.glob("*.sbd"), key=_sort_key)
    if not files:
        raise ValueError(f"No .sbd files found in {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        _decode_payload_file(path, expected_audio_band_count=expected_audio_band_count)
        for path in files
    ]

    json_rows = [_row_to_json(row) for row in rows]
    json_path = output_dir / "payloads_decoded.json"
    jsonl_path = output_dir / "payloads_decoded.jsonl"
    summary_pdf_path = output_dir / "payloads_report.pdf"

    json_path.write_text(
        json.dumps(json_rows, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in json_rows:
            handle.write(
                json.dumps(row, ensure_ascii=False, default=_json_default) + "\n"
            )

    for row in rows:
        _build_payload_pdf(row, output_dir / Path(row.filename).with_suffix(".pdf"))
    _build_run_summary_pdf(rows, summary_pdf_path)

    ok = sum(1 for row in rows if row.ok)
    print(f"Analizados: {len(rows)}")
    print(f"Decodificados OK: {ok}")
    print(f"Errores: {len(rows) - ok}")
    print(f"Reportes individuales: {len(rows)}")
    print(f"Resumen PDF: {summary_pdf_path}")
    print(f"JSON: {json_path}")
    print(f"JSONL: {jsonl_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decode Iridium payloads as JSON or executive PDF reports."
    )
    parser.add_argument("--hex", dest="hex_payload", help="Payload as a hex string")
    parser.add_argument(
        "--base64", dest="base64_payload", help="Payload as a base64 string"
    )
    parser.add_argument("--file", help="Path to a binary payload file")
    parser.add_argument(
        "--input-dir",
        help="Directory containing .sbd payloads for batch PDF/JSON report generation",
    )
    parser.add_argument(
        "--output-dir",
        default="scripts/payload_reports",
        help="Directory for batch reports.",
    )
    parser.add_argument(
        "--expected-audio-band-count",
        type=int,
        default=None,
        help="Expected AudioProc band count for validating audio payloads",
    )
    args = parser.parse_args(argv)

    try:
        if args.input_dir is not None:
            selected_single = [
                args.hex_payload is not None,
                args.base64_payload is not None,
                args.file is not None,
            ]
            if any(selected_single):
                raise ValueError(
                    "--input-dir cannot be combined with --hex, --base64, or --file"
                )
            return _process_input_dir(
                Path(args.input_dir),
                Path(args.output_dir),
                expected_audio_band_count=args.expected_audio_band_count,
            )

        payload = _payload_from_args(args)
        decoded = decode_message(
            payload, expected_audio_band_count=args.expected_audio_band_count
        )
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps(decoded, indent=2, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
