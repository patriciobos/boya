import os
import time
import pytest
from serial.tools import list_ports

from modules.ais_LL import AISLowLevel


@pytest.mark.timeout(120)
def test_ais_ll_device_present_and_fix():
    ports = list(list_ports.comports())
    if not ports:
        pytest.skip("No serial ports available to test AISLowLevel")

    preferred = os.getenv('PREFERRED_PORT')
    scan_window = float(os.getenv('SCAN_WINDOW', '2.0'))
    wait_for_fix = float(os.getenv('WAIT_FOR_FIX', '12.0'))

    ll = AISLowLevel(dev=True, preferred_port=preferred, scan_window=scan_window, wait_for_fix=wait_for_fix)
    detected = ll.init(timeout=max(6.0, wait_for_fix))
    assert detected, "No NMEA device detected on serial ports (check PREFERRED_PORT or connect device)"

    opened = ll.open()
    assert opened, "Could not open detected serial port"

    res = ll.test(wait_for_fix=wait_for_fix)
    assert res.get('device_present', False), "Device should be present"
    assert res.get('port_opened', False), "Port should be opened"
    assert res.get('has_fix', False), "GPS should have fix within wait period"

    nav = ll.get_navigation()
    assert nav.get('lat') is not None and nav.get('lon') is not None
    ll.close()
