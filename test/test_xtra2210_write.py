import pytest

from modules.xtra2210_LL import XTRA2210LowLevel


def test_xtra2210_builds_read_holding_registers_request():
    ll = XTRA2210LowLevel(slave_id=1)
    request = ll.build_read_holding_registers_request(0x9000, 2)

    assert request[:6] == bytes.fromhex("010390000002")
    assert int.from_bytes(request[-2:], "little") == ll.modbus_crc(request[:-2])


def test_xtra2210_builds_and_parses_write_single_register():
    ll = XTRA2210LowLevel(slave_id=1)
    request = ll.build_write_single_register_request(0x9000, 0x0001)

    assert request[:7] == bytes.fromhex("01109000000102")
    assert request[7:9] == bytes.fromhex("0001")
    assert int.from_bytes(request[-2:], "little") == ll.modbus_crc(request[:-2])

    response_payload = bytes.fromhex("011090000001")
    response = response_payload + ll.modbus_crc(response_payload).to_bytes(2, "little")
    ok, parsed = ll.parse_write_single_register_response(response, 0x9000, 0x0001)
    assert ok is True
    assert parsed == {"register": 0x9000, "count": 1, "value": 0x0001}


def test_xtra2210_write_single_register_dry_run_does_not_require_open_port():
    ll = XTRA2210LowLevel(slave_id=1)
    result = ll.write_single_holding_register(0x9000, 0x0001, dry_run=True)

    assert result["register_hex"] == "0x9000"
    assert result["value_hex"] == "0x0001"
    assert result["dry_run"] is True
    assert result["written"] is False
    assert result["request_hex"].startswith("011090000001020001")


def test_xtra2210_refuses_real_write_without_explicit_confirm():
    ll = XTRA2210LowLevel(slave_id=1)

    with pytest.raises(ValueError, match="confirm=True"):
        ll.write_single_holding_register(0x9000, 0x0001, dry_run=False)


def test_xtra2210_refuses_real_write_outside_allowlist_even_with_confirm():
    ll = XTRA2210LowLevel(slave_id=1)

    with pytest.raises(ValueError, match="non-allowlisted"):
        ll.write_single_holding_register(0x9999, 0x0001, dry_run=False, confirm=True)


def test_xtra2210_encodes_recommended_battery_parameters():
    ll = XTRA2210LowLevel(slave_id=1)

    assert ll.encode_battery_parameter("battery_type", "user") == 0
    assert ll.encode_battery_parameter("boost_voltage_v", 14.4) == 1440
    assert ll.encode_battery_parameter("float_voltage_v", 13.7) == 1370
    assert ll.encode_battery_parameter("equalization_time_min", 0) == 0


def test_xtra2210_builds_recommended_battery_write_plan_as_dry_run():
    ll = XTRA2210LowLevel(slave_id=1)
    plan = ll.build_battery_parameter_write_plan()

    by_param = {item["parameter"]: item for item in plan}
    assert set(by_param) == set(ll.RECOMMENDED_BATTERY_PARAMETERS)
    assert by_param["battery_type"]["register_hex"] == "0x9000"
    assert by_param["battery_type"]["value"] == 0
    assert by_param["boost_voltage_v"]["register_hex"] == "0x9007"
    assert by_param["boost_voltage_v"]["value"] == 1440
    assert by_param["low_voltage_disconnect_v"]["register_hex"] == "0x900D"
    assert by_param["low_voltage_disconnect_v"]["value"] == 1110
    assert all(item["dry_run"] is True for item in plan)
    assert all(item["written"] is False for item in plan)


def test_xtra2210_loads_battery_parameter_config(tmp_path):
    config_path = tmp_path / "xtra2210_battery_params_test.json"
    config_path.write_text(
        '{"registers":{"0x9008":{"name":"Float Voltage","voltage":13.7,"modbus_value":1370},'
        '"0x9002":{"name":"Temperature Compensation Coefficient","value":-3}},'
        '"timers":{"equalization_time_minutes":0,"boost_time_minutes":120}}',
        encoding="utf-8",
    )

    path, desired = XTRA2210LowLevel.load_battery_parameter_config(config_path)

    assert path == config_path
    assert desired[0x9008]["value"] == 1370
    assert desired[0x9002]["value"] == 300
    assert desired[0x906B]["value"] == 0
    assert desired[0x906C]["value"] == 120


def test_xtra2210_sync_writes_only_changed_registers(tmp_path):
    config_path = tmp_path / "xtra2210_battery_params_test.json"
    config_path.write_text(
        '{"registers":{"0x9008":{"name":"Float Voltage","modbus_value":1370},'
        '"0x9009":{"name":"Boost Reconnect Voltage","modbus_value":1320}},'
        '"timers":{"equalization_time_minutes":0}}',
        encoding="utf-8",
    )
    ll = XTRA2210LowLevel(slave_id=1)
    current = {0x9008: 1380, 0x9009: 1320, 0x906B: 120}
    writes = []

    def fake_read(register, count=1):
        return [current[register + offset] for offset in range(count)]

    def fake_write_block(start_reg, values, dry_run=True, confirm=False):
        writes.append((start_reg, values, dry_run, confirm))
        for offset, value in enumerate(values):
            current[start_reg + offset] = value
        return {"start_register": start_reg, "values": values, "dry_run": dry_run, "written": not dry_run}

    ll.read_holding_registers_raw = fake_read
    ll.write_holding_registers = fake_write_block

    report = ll.sync_battery_parameters_from_config(config_path)

    assert report["checked"] == 3
    assert report["matched"] == 1
    assert [(item["register"], item["before"], item["after"]) for item in report["changed"]] == [
        (0x9008, 1380, 1370),
        (0x906B, 120, 0),
    ]
    assert writes == [(0x9008, [1370], False, True), (0x906B, [0], False, True)]
