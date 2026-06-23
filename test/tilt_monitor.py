#!/usr/bin/env python3
from __future__ import annotations

import sys
import argparse
import json
import time
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
MODULES_DIR = THIS_FILE.parent.parent
PROJECT_ROOT = MODULES_DIR.parent
SUPPORT_DIR = PROJECT_ROOT / "support"

if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))

from mpu6050_LL import MPU6050LowLevel, MPU6050Error, NotFound


def load_calibration(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MPU6050 one-line tilt monitor")
    parser.add_argument("--bus", "-b", type=int, default=None, help="I2C bus override")
    parser.add_argument(
        "--address",
        "-a",
        type=lambda x: int(x, 0),
        default=0x68,
        help="I2C address (default: 0x68, alternate: 0x69)",
    )
    parser.add_argument(
        "--calibration",
        "-c",
        type=Path,
        default=SUPPORT_DIR / "mpu6050_calibration.json",
        help="Calibration JSON file",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=10,
        help="Number of accel samples to average per tilt reading",
    )
    parser.add_argument(
        "--sample-delay",
        type=float,
        default=0.02,
        help="Delay between samples inside each averaged tilt reading, in seconds",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.20,
        help="Delay between screen updates, in seconds",
    )
    parser.add_argument(
        "--ema-alpha",
        type=float,
        default=0.20,
        help="Exponential moving average alpha in [0,1]. Smaller = smoother.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Also include raw accel and gyro readings",
    )
    args = parser.parse_args(argv)

    if not (0.0 <= args.ema_alpha <= 1.0):
        print("Error: --ema-alpha must be between 0 and 1", file=sys.stderr)
        return 2

    drv = MPU6050LowLevel()
    last_len = 0

    f_roll = None
    f_pitch = None
    f_ax = None
    f_ay = None
    f_az = None

    try:
        print("Inicializando MPU6050...")
        drv.init(bus=args.bus, address=args.address)

        if not drv.probe():
            print(
                f"No se detectó MPU6050 en bus={drv.bus_num} address=0x{drv.address:02x}"
            )
            return 2

        print(f"Detectado en bus={drv.bus_num} address=0x{drv.address:02x}")
        print(f"WHO_AM_I = 0x{drv.whoami():02x}")

        print("Reseteando y configurando defaults...")
        drv.reset()
        drv.configure_default()

        if args.calibration.exists():
            cal = load_calibration(args.calibration)
            drv.set_accel_offsets(*cal["accel_offsets_g"])
            drv.set_gyro_offsets(*cal["gyro_offsets_dps"])
            print(f"Calibración cargada desde: {args.calibration}")
        else:
            print(f"No se encontró calibración en: {args.calibration}")
            print("Se usarán offsets nulos.")

        print("Mové el sensor. Ctrl+C para salir.")

        while True:
            roll_deg, pitch_deg = drv.read_tilt_deg_avg(
                samples=args.samples,
                delay=args.sample_delay,
            )
            corrected = drv.read_all_corrected()

            alpha = args.ema_alpha

            if f_roll is None:
                f_roll = roll_deg
                f_pitch = pitch_deg
                f_ax = corrected["ax_g"]
                f_ay = corrected["ay_g"]
                f_az = corrected["az_g"]
            else:
                f_roll = alpha * roll_deg + (1.0 - alpha) * f_roll
                f_pitch = alpha * pitch_deg + (1.0 - alpha) * f_pitch
                f_ax = alpha * corrected["ax_g"] + (1.0 - alpha) * f_ax
                f_ay = alpha * corrected["ay_g"] + (1.0 - alpha) * f_ay
                f_az = alpha * corrected["az_g"] + (1.0 - alpha) * f_az

            line = (
                f"Roll ={f_roll:7.2f}°  "
                f"Pitch ={f_pitch:7.2f}°  "
                f"A(xyz) =({f_ax: .3f},{f_ay: .3f},{f_az: .3f})"
            )

            if args.raw:
                raw = drv.read_all_raw()
                line += (
                    f"  raw_a=({raw['ax']:6d},{raw['ay']:6d},{raw['az']:6d})"
                    f"  raw_g=({raw['gx']:6d},{raw['gy']:6d},{raw['gz']:6d})"
                )

            padded = line
            if len(padded) < last_len:
                padded += " " * (last_len - len(padded))

            sys.stdout.write("\r" + padded)
            sys.stdout.flush()
            last_len = len(line)

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nSaliendo...")
        return 0
    except NotFound as e:
        print(f"\nDependencia faltante: {e}", file=sys.stderr)
        return 3
    except MPU6050Error as e:
        print(f"\nError MPU6050: {e}", file=sys.stderr)
        return 4
    except Exception as e:
        print(f"\nError inesperado: {e}", file=sys.stderr)
        return 5
    finally:
        try:
            drv.deinit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
