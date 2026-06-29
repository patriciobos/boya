#!/usr/bin/env python3
import argparse
import json
import sys

from modules.iridium_LL import IridiumLowLevel


def main():
    parser = argparse.ArgumentParser(
        description="Envía manualmente un mensaje de prueba por Iridium SBD."
    )
    parser.add_argument(
        "mensaje",
        nargs="?",
        default="TEST",
        help="Mensaje a enviar. Máximo 120 bytes en modo texto.",
    )
    parser.add_argument(
        "--port",
        default="/dev/ttyS2",
        help="Puerto serie del módem. Ejemplo: /dev/ttyS2",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=19200,
        help="Baudrate del módem Iridium.",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="Cantidad de intentos SBDIX.",
    )

    args = parser.parse_args()

    modem = IridiumLowLevel(
        preferred_port=args.port,
        baudrate=args.baudrate,
        timeout=1.0,
        show_ports=True,
    )

    if not modem.init():
        print(f"ERROR init: {modem.last_error}", file=sys.stderr)
        return 1

    try:
        ok, report = modem.send_sbd_text(
            args.mensaje,
            clear_after_success=True,
            max_attempts=args.attempts,
            retry_delay_s=10.0,
            session_timeout=90.0,
        )

        print("OK:", ok)
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))

        return 0 if ok else 2

    finally:
        modem.deinit()


if __name__ == "__main__":
    raise SystemExit(main())
