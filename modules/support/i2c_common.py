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