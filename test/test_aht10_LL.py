from modules.aht10_LL import AHT10LowLevel


def test_parse_warns_but_returns_out_of_range_payload_that_decodes_to_minus_50(caplog):
    ll = AHT10LowLevel()

    temperature_c, humidity_rh = ll.parse(b"\x00\x00\x00\x00\x00\x08")

    assert round(temperature_c, 2) == -50.0
    assert humidity_rh == 0.0
    assert "out of expected range" in caplog.text
