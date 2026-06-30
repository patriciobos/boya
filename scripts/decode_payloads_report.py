#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
from datetime import datetime, timezone

from reportlab.lib import colors
import textwrap
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    Preformatted,
)

from modules.support.iridium_protocol import decode_message


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def metadata_from_filename(path: Path) -> dict:
    name = path.name
    match = re.search(r"_(\d{15})_(\d{6})\.sbd$", name)
    return {
        "filename": name,
        "imei": match.group(1) if match else None,
        "momsn": int(match.group(2)) if match else None,
    }


def classify(decoded: dict, payload_size: int) -> str:
    msg = decoded.get("message_type")
    msg_name = decoded.get("message_type_name")

    if msg == "MSG_SYSTEM_STATUS":
        return "Estado del sistema"

    if msg == "MSG_BOOT":
        return "Arranque"

    if msg_name == "MSG_AUDIO" or msg in (3, 4, 5, 6):
        return "AudioProc"

    return f"Tipo {msg}"


def short_observation(decoded: dict, ok: bool, error: str | None) -> str:
    if not ok:
        return error or "Error de decodificación"

    if decoded.get("message_type") == "MSG_SYSTEM_STATUS":
        battery = decoded.get("battery", {})
        storage = decoded.get("storage", {})
        flags = decoded.get("status_flags_raw")
        voltage = battery.get("voltage_v")
        soc = battery.get("soc_percent")
        free = storage.get("free_gib")
        return (
            f"Vbat={voltage} V; SoC={soc} %; "
            f"libre={free} GiB; flags=0x{int(flags):02X}"
            if flags is not None
            else "Estado del sistema"
        )

    if decoded.get("message_type_name") == "MSG_AUDIO":
        ts = decoded.get("timestamp")
        ch = decoded.get("channel_count")
        bands = decoded.get("band_count")
        packing = decoded.get("packing")
        crc = decoded.get("crc16_ccitt_false")
        return (
            f"timestamp={ts}; canales={ch}; bandas={bands}; "
            f"packing={packing}; CRC=0x{int(crc):04X}"
            if crc is not None
            else f"timestamp={ts}; canales={ch}; bandas={bands}; packing={packing}"
        )

    return "Decodificado correctamente"


def decode_one(path: Path, expected_audio_band_count: int | None) -> dict:
    meta = metadata_from_filename(path)
    payload = path.read_bytes()

    row = {
        **meta,
        "size_bytes": len(payload),
        "ok": False,
        "kind": None,
        "decoded": None,
        "error": None,
        "observation": None,
    }

    try:
        decoded = decode_message(
            payload,
            expected_audio_band_count=expected_audio_band_count,
        )
        row["ok"] = True
        row["kind"] = classify(decoded, len(payload))
        row["decoded"] = decoded
        row["observation"] = short_observation(decoded, True, None)
    except Exception as exc:
        row["error"] = str(exc)
        row["kind"] = "No decodificado"
        row["observation"] = short_observation({}, False, str(exc))

    return row


def _cell(text, style):
    text = "" if text is None else str(text)
    return Paragraph(xml_escape(text), style)


def _wrapped_filename(name: str, style, chunk: int = 34):
    name = "" if name is None else str(name)
    pieces = [name[i:i + chunk] for i in range(0, len(name), chunk)]
    html = "<br/>".join(xml_escape(piece) for piece in pieces)
    return Paragraph(html, style)


def _wrapped_json_text(obj: dict, width: int = 125) -> str:
    raw = json.dumps(obj, indent=2, ensure_ascii=False, default=json_default)
    out_lines = []

    for line in raw.splitlines():
        if len(line) <= width:
            out_lines.append(line)
            continue

        indent = len(line) - len(line.lstrip(" "))
        prefix = " " * indent
        wrapped = textwrap.wrap(
            line.strip(),
            width=max(40, width - indent),
            break_long_words=True,
            break_on_hyphens=False,
        )
        out_lines.extend(prefix + part for part in wrapped)

    return "\n".join(out_lines)


def _audio_proc_frequency_labels(band_count: int) -> list[str]:
    bands_path = Path(__file__).resolve().parents[1] / "support" / "third_octave_bands.csv"
    labels: list[str] = []

    try:
        with bands_path.open("r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if len(labels) >= band_count:
                    break
                fc = row.get("fc")
                fl = row.get("fl")
                fh = row.get("fh")
                if fc is None or fl is None or fh is None:
                    continue
                try:
                    labels.append(
                        f"{float(fc):g} Hz ({float(fl):g}-{float(fh):g} Hz)"
                    )
                except ValueError:
                    labels.append(f"{fc} Hz")
    except FileNotFoundError:
        pass

    if len(labels) < band_count:
        labels.extend(f"Band {i + 1}" for i in range(len(labels), band_count))

    return labels


def _build_audio_proc_table(decoded: dict, style) -> Table | None:
    relative_power = decoded.get("relative_band_power_db")
    if not isinstance(relative_power, list) or not relative_power:
        return None

    band_count = len(relative_power)
    frequency_labels = _audio_proc_frequency_labels(band_count)
    first_row = relative_power[0]
    channel_count = 1
    if isinstance(first_row, list):
        channel_count = len(first_row)

    cells_per_row = 4
    rows = []
    row_count = math.ceil(band_count / cells_per_row)

    for row_index in range(row_count):
        row_cells = []
        for col_index in range(cells_per_row):
            band_index = row_index + col_index * row_count
            if band_index >= band_count:
                row_cells.append("")
                continue

            band_label = frequency_labels[band_index]
            value = relative_power[band_index]
            if isinstance(value, list):
                if channel_count == 1:
                    value_text = f"{float(value[0]):.1f} dB"
                else:
                    value_text = "<br/>".join(
                        xml_escape(f"Ch{ch + 1}: {float(val):.1f} dB")
                        for ch, val in enumerate(value)
                    )
            else:
                value_text = f"{float(value):.1f} dB"

            cell_html = (
                f"<b>Band {band_index + 1}</b><br/>"
                f"{xml_escape(band_label)}<br/>"
                f"{value_text}"
            )
            row_cells.append(Paragraph(cell_html, style))

        rows.append(row_cells)

    table = Table(rows, colWidths=[65 * mm] * cells_per_row, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


def build_pdf(rows: list[dict], pdf_path: Path) -> None:
    styles = getSampleStyleSheet()

    title_style = styles["Title"]
    normal_style = styles["Normal"]

    table_header_style = ParagraphStyle(
        "TableHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7,
        leading=8,
        wordWrap="CJK",
    )

    table_cell_style = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontSize=6.5,
        leading=7.5,
        wordWrap="CJK",
    )

    table_cell_right_style = ParagraphStyle(
        "TableCellRight",
        parent=table_cell_style,
        alignment=2,
    )

    code_style = ParagraphStyle(
        "SmallCode",
        parent=styles["Code"],
        fontName="Courier",
        fontSize=5.6,
        leading=6.4,
    )

    page_size = landscape(A4)

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=page_size,
        rightMargin=9 * mm,
        leftMargin=9 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )

    story = []

    story.append(Paragraph("Reporte de payloads Iridium SBD", title_style))
    story.append(Spacer(1, 5 * mm))

    total = len(rows)
    ok = sum(1 for r in rows if r["ok"])
    errors = total - ok

    by_kind = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1

    story.append(Paragraph(f"Total de archivos analizados: {total}", normal_style))
    story.append(Paragraph(f"Decodificados correctamente: {ok}", normal_style))
    story.append(Paragraph(f"Con error de decodificación: {errors}", normal_style))
    story.append(Spacer(1, 4 * mm))

    summary_data = [
        [_cell("Tipo", table_header_style), _cell("Cantidad", table_header_style)]
    ]

    for kind, count in sorted(by_kind.items()):
        summary_data.append([
            _cell(kind, table_cell_style),
            _cell(str(count), table_cell_right_style),
        ])

    summary_table = Table(
        summary_data,
        colWidths=[90 * mm, 28 * mm],
        hAlign="LEFT",
        repeatRows=1,
    )

    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))

    story.append(summary_table)
    story.append(Spacer(1, 6 * mm))

    table_data = [[
        _cell("MOMSN", table_header_style),
        _cell("Archivo", table_header_style),
        _cell("Bytes", table_header_style),
        _cell("Tipo", table_header_style),
        _cell("Resultado", table_header_style),
    ]]

    for r in rows:
        table_data.append([
            _cell("" if r["momsn"] is None else str(r["momsn"]), table_cell_style),
            _wrapped_filename(r["filename"], table_cell_style, chunk=32),
            _cell(str(r["size_bytes"]), table_cell_right_style),
            _cell(r["kind"], table_cell_style),
            _cell(r["observation"], table_cell_style),
        ])

    main_table = Table(
        table_data,
        repeatRows=1,
        splitByRow=1,
        hAlign="LEFT",
        colWidths=[
            16 * mm,   # MOMSN
            74 * mm,   # Archivo
            15 * mm,   # Bytes
            31 * mm,   # Tipo
            139 * mm,  # Resultado
        ],
    )

    main_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.5),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))

    story.append(main_table)

    story.append(PageBreak())
    story.append(Paragraph("Detalle JSON por payload", styles["Heading1"]))

    for r in rows:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(xml_escape(r["filename"]), styles["Heading3"]))

        if r["kind"] == "AudioProc" and isinstance(r.get("decoded"), dict):
            audio_table = _build_audio_proc_table(r["decoded"], table_cell_style)
            if audio_table is not None:
                story.append(Paragraph("AudioProc: banda vs potencia relativa", styles["Heading4"]))
                story.append(audio_table)
                story.append(Spacer(1, 4 * mm))

        detail = {
            "filename": r["filename"],
            "imei": r["imei"],
            "momsn": r["momsn"],
            "size_bytes": r["size_bytes"],
            "ok": r["ok"],
            "kind": r["kind"],
            "error": r["error"],
            "decoded": r["decoded"],
        }

        story.append(Preformatted(
            _wrapped_json_text(detail, width=125),
            code_style,
        ))

    doc.build(story)


def build_pdf_for_payload(row: dict, pdf_path: Path) -> None:
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    normal_style = styles["Normal"]
    code_style = ParagraphStyle(
        "SmallCode",
        parent=styles["Code"],
        fontName="Courier",
        fontSize=6.5,
        leading=7.5,
    )

    page_size = landscape(A4)

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=page_size,
        rightMargin=9 * mm,
        leftMargin=9 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )

    story = []
    story.append(Paragraph(f"Reporte payload Iridium SBD", title_style))
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(f"Archivo: {xml_escape(str(row.get('filename', '')))}", normal_style))
    story.append(Paragraph(f"MOMSN: {row.get('momsn')}", normal_style))
    story.append(Paragraph(f"Tamaño: {row.get('size_bytes')} bytes", normal_style))
    story.append(Paragraph(f"Tipo: {xml_escape(str(row.get('kind', '')))}", normal_style))
    story.append(Paragraph(f"Decodificado correctamente: {'Sí' if row.get('ok') else 'No'}", normal_style))
    if row.get("error"):
        story.append(Paragraph(f"Error: {xml_escape(str(row['error']))}", normal_style))

    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph("Detalle JSON", styles["Heading2"]))

    detail = {
        "filename": row.get("filename"),
        "imei": row.get("imei"),
        "momsn": row.get("momsn"),
        "size_bytes": row.get("size_bytes"),
        "ok": row.get("ok"),
        "kind": row.get("kind"),
        "error": row.get("error"),
        "decoded": row.get("decoded"),
    }

    story.append(Preformatted(
        _wrapped_json_text(detail, width=125),
        code_style,
    ))

    doc.build(story)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decode all Iridium SBD payloads and generate JSONL/PDF reports."
    )
    parser.add_argument(
        "--input-dir",
        default="scripts/payloads",
        help="Directory containing .sbd files.",
    )
    parser.add_argument(
        "--output-dir",
        default="scripts/payload_reports",
        help="Directory for reports.",
    )
    parser.add_argument(
        "--expected-audio-band-count",
        type=int,
        default=49,
        help="Expected AudioProc band count.",
    )
    parser.add_argument(
        "--write-individual-pdfs",
        action="store_true",
        help="Generate one PDF report per payload in the output directory.",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(input_dir.glob("*.sbd"))
    if not files:
        raise SystemExit(f"No .sbd files found in {input_dir}")

    rows = [
        decode_one(path, args.expected_audio_band_count)
        for path in files
    ]

    jsonl_path = output_dir / "payloads_decoded.jsonl"
    json_path = output_dir / "payloads_decoded.json"
    pdf_path = output_dir / "payloads_report.pdf"

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=json_default) + "\n")

    json_path.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False, default=json_default),
        encoding="utf-8",
    )

    build_pdf(rows, pdf_path)

    if args.write_individual_pdfs:
        for row in rows:
            filename = row.get("filename") or "payload"
            pdf_name = Path(filename).with_suffix(".pdf")
            build_pdf_for_payload(row, output_dir / pdf_name)

        print(f"PDFs individuales: {len(rows)}")

    ok = sum(1 for row in rows if row["ok"])
    print(f"Analizados: {len(rows)}")
    print(f"Decodificados OK: {ok}")
    print(f"Errores: {len(rows) - ok}")
    print(f"JSONL: {jsonl_path}")
    print(f"JSON: {json_path}")
    print(f"PDF: {pdf_path}")


if __name__ == "__main__":
    raise SystemExit(main())
