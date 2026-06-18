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
DEFAULT_CALIBRATION_PATH = PROJECT_ROOT / "support" / "mpu6050_calibration.json"
if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))

from mpu6050_LL import (
    MPU6050LowLevel,
    MPU6050Error,
    NotFound,
    CalibrationError,
)

def save_calibration(
    path: Path,
    bus: int | None,
    address: int,
    accel_offsets_g: tuple[float, float, float],
    gyro_offsets_dps: tuple[float, float, float],
    expected_gravity_g: tuple[float, float, float],
) -> None:
    data = {
        "sensor": "MPU6050",
        "bus": bus,
        "address": address,
        "accel_offsets_g": list(accel_offsets_g),
        "gyro_offsets_dps": list(gyro_offsets_dps),
        "expected_gravity_g": list(expected_gravity_g),
        "created_unix": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MPU6050 calibration script")
    parser.add_argument("--bus", "-b", type=int, default=None, help="I2C bus override")
    parser.add_argument(
        "--address",
        "-a",
        type=lambda x: int(x, 0),
        default=0x68,
        help="I2C address (default: 0x68, alternate: 0x69)",
    )
    parser.add_argument("--gyro-samples", type=int, default=500)
    parser.add_argument("--accel-samples", type=int, default=500)
    parser.add_argument("--delay", type=float, default=0.01)
    parser.add_argument("--expected-x", type=float, default=0.0)
    parser.add_argument("--expected-y", type=float, default=0.0)
    parser.add_argument("--expected-z", type=float, default=-1.0)
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=DEFAULT_CALIBRATION_PATH,
        help="Output JSON file",
    )
    args = parser.parse_args(argv)

    expected = (args.expected_x, args.expected_y, args.expected_z)

    drv = MPU6050LowLevel()
    try:
        print("Inicializando MPU6050...")
        drv.init(bus=args.bus, address=args.address)
        if not drv.probe():
            print(f"No se detectó MPU6050 en bus={drv.bus_num} address=0x{drv.address:02x}")
            return 2

        print(f"Detectado en bus={drv.bus_num} address=0x{drv.address:02x}")
        print(f"WHO_AM_I = 0x{drv.whoami():02x}")

        print("Reseteando y configurando defaults...")
        drv.reset()
        drv.configure_default()

        print("Dejá el sensor totalmente quieto en la postura de referencia.")
        print(
            f"Vector de gravedad esperado: "
            f"({expected[0]:.3f}, {expected[1]:.3f}, {expected[2]:.3f}) g"
        )
        print("Esperando 2 segundos antes de empezar...")
        time.sleep(2.0)

        print("Calibrando giroscopio...")
        gyro_offsets = drv.calibrate_gyro(
            samples=args.gyro_samples,
            delay=args.delay,
        )
        print(
            "gyro_offsets_dps = "
            f"({gyro_offsets[0]:.6f}, {gyro_offsets[1]:.6f}, {gyro_offsets[2]:.6f})"
        )

        print("Calibrando acelerómetro...")
        accel_offsets = drv.calibrate_accel(
            samples=args.accel_samples,
            delay=args.delay,
            expected=expected,
        )
        print(
            "accel_offsets_g  = "
            f"({accel_offsets[0]:.6f}, {accel_offsets[1]:.6f}, {accel_offsets[2]:.6f})"
        )

        corrected = drv.read_all_corrected()
        roll_deg, pitch_deg = drv.read_tilt_deg_avg(samples=10, delay=0.02)

        print("Lectura corregida actual:")
        print(
            "  accel_g = "
            f"({corrected['ax_g']:.6f}, {corrected['ay_g']:.6f}, {corrected['az_g']:.6f})"
        )
        print(
            "  gyro_dps = "
            f"({corrected['gx_dps']:.6f}, {corrected['gy_dps']:.6f}, {corrected['gz_dps']:.6f})"
        )
        print(f"  temp_c = {corrected['temp_c']:.2f}")
        print(f"  tilt_deg = (roll={roll_deg:.3f}, pitch={pitch_deg:.3f})")

        save_calibration(
            path=args.output,
            bus=drv.bus_num,
            address=drv.address,
            accel_offsets_g=accel_offsets,
            gyro_offsets_dps=gyro_offsets,
            expected_gravity_g=expected,
        )
        print(f"Calibración guardada en: {args.output}")
        return 0

    except NotFound as e:
        print(f"Dependencia faltante: {e}", file=sys.stderr)
        return 3
    except (MPU6050Error, CalibrationError) as e:
        print(f"Error de calibración/MPU6050: {e}", file=sys.stderr)
        return 4
    except Exception as e:
        print(f"Error inesperado: {e}", file=sys.stderr)
        return 5
    finally:
        try:
            drv.deinit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
