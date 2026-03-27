from __future__ import annotations

import math
import time
from typing import Optional, Tuple

from mpu6050_LL import MPU6050LowLevel


class MPU6050ComplementaryFilter:
    """Simple roll/pitch estimator using accel + gyro fusion."""

    def __init__(
        self,
        driver: MPU6050LowLevel,
        alpha: float = 0.98,
    ):
        if not (0.0 <= alpha <= 1.0):
            raise ValueError("alpha must be in [0, 1]")

        self.drv = driver
        self.alpha = float(alpha)

        self.roll_deg: Optional[float] = None
        self.pitch_deg: Optional[float] = None
        self.last_t: Optional[float] = None

    def reset(self) -> None:
        self.roll_deg = None
        self.pitch_deg = None
        self.last_t = None

    def initialize_from_accel(self) -> Tuple[float, float]:
        """Initialize filter state from accelerometer tilt."""
        roll_acc, pitch_acc = self.drv.read_tilt_deg_corrected()
        self.roll_deg = roll_acc
        self.pitch_deg = pitch_acc
        self.last_t = time.monotonic()
        return self.roll_deg, self.pitch_deg

    def update(self) -> Tuple[float, float]:
        """Run one complementary-filter update and return (roll_deg, pitch_deg)."""
        now = time.monotonic()

        if self.roll_deg is None or self.pitch_deg is None or self.last_t is None:
            return self.initialize_from_accel()

        dt = now - self.last_t
        self.last_t = now

        # corrected gyro in deg/s
        gx_dps, gy_dps, gz_dps = self.drv.read_gyro_dps_corrected()

        # accel-based tilt in deg
        roll_acc, pitch_acc = self.drv.read_tilt_deg_corrected()

        # Integrate gyro.
        # This mapping is the usual first approximation for roll/pitch:
        # gx -> roll rate
        # gy -> pitch rate
        roll_gyro = self.roll_deg + gx_dps * dt
        pitch_gyro = self.pitch_deg + gy_dps * dt

        # Complementary fusion
        a = self.alpha
        self.roll_deg = a * roll_gyro + (1.0 - a) * roll_acc
        self.pitch_deg = a * pitch_gyro + (1.0 - a) * pitch_acc

        return self.roll_deg, self.pitch_deg