import importlib
import os

import pytest

import modules.support.ll_factory as ll_factory

MODULE_NAMES = [
    "AHT10",
    "AIS",
    "AudioProc",
    "Behringer",
    "Iridium",
    "MPU6050",
    "Windsonic",
    "XTRA2210",
]


@pytest.fixture(autouse=True)
def enable_ll_mocks(monkeypatch):
    monkeypatch.setenv("USE_LL_MOCKS", "1")
    importlib.reload(ll_factory)
    yield
    # reload after test to avoid side effects for other tests
    monkeypatch.delenv("USE_LL_MOCKS", raising=False)
    importlib.reload(ll_factory)


@pytest.mark.parametrize("module_name", MODULE_NAMES)
def test_ll_module_full_test_runs(module_name):
    ll_class = ll_factory.get_low_level_class(module_name)
    ll = ll_class()
    assert ll.init() is True
    ok, details = ll.full_test()
    assert ok is True
    assert isinstance(details, dict)
    assert "errors" in details or module_name == "AudioProc"
    assert ll.deinit() is True
