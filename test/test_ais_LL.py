import os
import time
import pytest
from serial.tools import list_ports

from modules.ais_LL import AISLowLevel


@pytest.mark.hardware
@pytest.mark.timeout(120)
def test_ais_ll_device_present_and_fix():
    if os.getenv("RUN_HARDWARE_TESTS", "0").strip().lower() not in ("1", "true", "yes", "on"):
        pytest.skip("hardware test disabled; set RUN_HARDWARE_TESTS=1 to run")
    ports = list(list_ports.comports())
    if not ports:
        pytest.skip("No serial ports available to test AISLowLevel")

    preferred = os.getenv('PREFERRED_PORT')
    scan_window = float(os.getenv('SCAN_WINDOW', '2.0'))
    wait_for_fix = float(os.getenv('WAIT_FOR_FIX', '12.0'))

    ll = AISLowLevel(preferred_port=preferred, scan_window=scan_window, wait_for_fix=wait_for_fix)
    detected = ll.init(scan_window=max(6.0, wait_for_fix))
    assert detected, "No NMEA device detected on serial ports (check PREFERRED_PORT or connect device)"

    opened = ll.open()
    assert opened, "Could not open detected serial port"

    assert ll.test(), "Device should be present"

    nav = ll.get_navigation()
    if os.getenv("REQUIRE_GPS_FIX", "0").strip().lower() in ("1", "true", "yes", "on"):
        assert nav.get('lat') is not None and nav.get('lon') is not None
    ll.close()
