from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional


def discover_i2c_buses(preferred_bus: Optional[int] = None) -> List[int]:
    """Return ordered list of candidate I2C bus numbers.

    Order:
    - preferred_bus first, if given
    - then all discovered /dev/i2c-* buses in ascending order, deduplicated
    """
    candidates: List[int] = []
    if preferred_bus is not None:
        candidates.append(int(preferred_bus))

    for p in sorted(Path("/dev").glob("i2c-*")):
        try:
            n = int(p.name.split("-")[1])
        except Exception:
            continue
        if n not in candidates:
            candidates.append(n)

    return candidates


def create_driver_logger(
    logger_name: str,
    tag: str,
    logfile_name: str,
):
    """Create/configure a driver logger similar to current AHT10 style.

    - INFO level
    - stream handler
    - file handler under ../logs if possible
    - avoid duplicate handlers
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter(f"%(asctime)s [{tag}] %(levelname)s: %(message)s")

    if not logger.handlers:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)

        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_dir = os.path.join(base_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            fh = logging.FileHandler(
                os.path.join(log_dir, logfile_name),
                mode="a",
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except Exception:
            pass

    return logger