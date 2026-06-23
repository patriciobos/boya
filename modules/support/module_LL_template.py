from __future__ import annotations

from typing import Any, Optional

from support.log_utils import get_logger


class ModuleError(Exception):
    """Base exception for this low-level module."""


class NotFound(ModuleError):
    """Raised when a required dependency or device is not available."""


class TransportError(ModuleError):
    """Raised on low-level transport/resource errors."""


class ProtocolError(ModuleError):
    """Raised when the device response does not match expectations."""


class MyModuleLowLevel:
    """
    Canonical low-level module template.

    Public lifecycle contract:
    - init() -> bool
    - open() -> bool
    - close() -> bool
    - test() -> bool
    - full_test() -> tuple[bool, dict]
    - deinit() -> bool

    Optional:
    - probe() -> bool
    """

    DEFAULT_ADDRESS = 0x00
    DEFAULT_BUS = 1

    def __init__(
        self,
        logger_name: str = "my_module_LL",
    ) -> None:
        # logging
        self.logger = get_logger(logger_name)

        # standard lifecycle state
        self.is_initialized: bool = False
        self.is_open: bool = False
        self.last_error: Optional[str] = None

        # standard transport state
        self.bus: Optional[Any] = None  # or self.conn / self.transport
        self.bus_num: Optional[int] = None
        self.address: Optional[int] = None
        self.bus_candidates: list[int] = []
        self.bus_forced: bool = False

        # module-specific state
        self._config: dict[str, Any] = {}

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _set_error(self, message: str) -> None:
        self.last_error = message

    def _clear_error(self) -> None:
        self.last_error = None

    def _build_full_test_report(self) -> dict:
        return {
            "initialized": self.is_initialized,
            "opened": self.is_open,
            "device_present": False,
            "errors": [],
            "details": {},
        }

    def _require_dependencies(self) -> None:
        """
        Validate optional imports / runtime dependencies.

        Raise:
            NotFound: if a required dependency is not available.
        """
        # Example:
        # if SMBus is None:
        #     raise NotFound("smbus2 is required")
        return

    def _resolve_candidates(self, preferred: Optional[int]) -> list[int]:
        """
        Resolve transport candidates when transport is not forced.

        For explicit transport:
        - return only that one
        For implicit transport:
        - return ordered candidates
        """
        if preferred is not None:
            return [int(preferred)]

        # Replace with real discovery logic if needed.
        return [self.DEFAULT_BUS]

    def _open_transport(self, candidate: int) -> Any:
        """
        Open the physical transport/resource for a given candidate.

        Must return the live transport object.
        Must raise exceptions on failure.
        """
        raise NotImplementedError

    def _close_transport(self) -> None:
        """
        Close the physical transport/resource if present.

        Must not raise if already closed / missing.
        """
        # Example:
        # if self.bus is not None:
        #     self.bus.close()
        # self.bus = None
        raise NotImplementedError

    def _probe_device(self) -> bool:
        """
        Minimal presence/communication check.

        Internal version may raise exceptions.
        Public test()/full_test() must capture them.
        """
        raise NotImplementedError

    def _collect_diagnostics(self) -> dict[str, Any]:
        """
        Full diagnostic collection.

        May include:
        - identification
        - configuration
        - real sensor readings
        - transport info
        """
        raise NotImplementedError

    # -------------------------------------------------------------------------
    # Public lifecycle
    # -------------------------------------------------------------------------

    def init(
        self,
        bus: Optional[int] = None,
        address: Optional[int] = None,
    ) -> bool:
        """
        Prepare configuration and internal state only.

        Rules:
        - validates parameters
        - does not access hardware
        - does not open resources
        """
        self.logger.info("Initializing module")
        self._clear_error()

        try:
            self._require_dependencies()

            # Reset runtime state without touching hardware
            self.close()

            self.address = int(address) if address is not None else self.DEFAULT_ADDRESS
            self.bus_forced = bus is not None
            self.bus_num = int(bus) if bus is not None else None
            self.bus_candidates = self._resolve_candidates(bus)

            self.is_initialized = True

            self.logger.info(
                "Module initialized: address=%s bus_num=%s bus_forced=%s candidates=%s",
                self.address,
                self.bus_num,
                self.bus_forced,
                self.bus_candidates,
            )
            return True

        except Exception as exc:
            self.is_initialized = False
            self._set_error(f"Initialization failed: {exc}")
            self.logger.exception("Initialization failed: %s", exc)
            return False

    def open(self) -> bool:
        """
        Open the physical resource and leave the module ready to operate.

        Rules:
        - if bus/transport is forced, use only that one
        - do not scan fallbacks if forced
        - idempotent if already open
        """
        self.logger.info("Opening module transport")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        if self.is_open and self.bus is not None:
            self.logger.info("Transport already open")
            return True

        candidates = (
            [self.bus_num]
            if self.bus_forced and self.bus_num is not None
            else list(self.bus_candidates)
        )
        last_exc: Optional[Exception] = None

        for candidate in candidates:
            try:
                self.logger.info("Trying transport candidate: %s", candidate)
                self.bus = self._open_transport(candidate)
                self.bus_num = candidate
                self.is_open = True
                self.logger.info("Transport opened on candidate: %s", candidate)
                return True

            except Exception as exc:
                last_exc = exc
                self.logger.warning("Failed to open candidate %s: %s", candidate, exc)

        self.bus = None
        self.is_open = False
        message = f"Open failed: {last_exc}" if last_exc else "Open failed"
        self._set_error(message)
        self.logger.error(message)
        return False

    def close(self) -> bool:
        """
        Close the operational resource.

        Rules:
        - idempotent
        - must not fail if already closed
        """
        self.logger.info("Closing module transport")
        self._clear_error()

        try:
            if self.bus is not None:
                self._close_transport()

            self.bus = None
            self.is_open = False

            self.logger.info("Transport closed")
            return True

        except Exception as exc:
            self.bus = None
            self.is_open = False
            self._set_error(f"Close failed: {exc}")
            self.logger.exception("Close failed: %s", exc)
            return False

    def probe(self) -> bool:
        """
        Optional public minimal presence check.

        Can be called by test() / full_test().
        """
        self.logger.info("Probing device")
        self._clear_error()

        try:
            result = bool(self._probe_device())
            self.logger.info("Probe result: %s", result)
            return result

        except Exception as exc:
            self._set_error(f"Probe failed: {exc}")
            self.logger.warning("Probe failed: %s", exc)
            return False

    def test(self) -> bool:
        """
        Fast and robust smoke test.

        Rules:
        - may open temporarily if needed
        - must restore the original state
        - should be hardware-compatible and quick
        """
        self.logger.info("Running smoke test")
        self._clear_error()

        was_open = self.is_open
        temporarily_opened = False

        try:
            if not was_open:
                if not self.open():
                    return False
                temporarily_opened = True

            if hasattr(self, "probe"):
                return bool(self.probe())

            return bool(self._probe_device())

        except Exception as exc:
            self._set_error(f"Test failed: {exc}")
            self.logger.warning("Test failed: %s", exc)
            return False

        finally:
            if temporarily_opened:
                self.close()

    def full_test(self) -> tuple[bool, dict]:
        """
        Full diagnostic test.

        Rules:
        - never propagates uncaught exceptions
        - may include identification, configuration and real readings
        - can scan fallback candidates only when transport is not forced
        """
        self.logger.info("Running full diagnostic test")
        self._clear_error()

        report = self._build_full_test_report()
        original_is_open = self.is_open
        original_bus_num = self.bus_num
        original_bus = self.bus
        temporarily_opened = False

        try:
            report["initialized"] = self.is_initialized

            if not self.is_initialized:
                msg = "Module is not initialized"
                report["errors"].append(msg)
                self._set_error(msg)
                return False, report

            if not self.is_open:
                if self.open():
                    temporarily_opened = True
                    report["opened"] = True
                else:
                    report["opened"] = False

                    # fallback scan allowed only when transport is not forced
                    if not self.bus_forced:
                        for candidate in self.bus_candidates:
                            if candidate == original_bus_num:
                                continue
                            try:
                                self.logger.info(
                                    "Fallback diagnostic open on candidate: %s",
                                    candidate,
                                )
                                self.bus = self._open_transport(candidate)
                                self.bus_num = candidate
                                self.is_open = True
                                temporarily_opened = True
                                report["opened"] = True
                                break
                            except Exception as exc:
                                report["errors"].append(
                                    f"Fallback open failed on candidate {candidate}: {exc}"
                                )

            if not self.is_open or self.bus is None:
                report["device_present"] = False
                if self.last_error:
                    report["errors"].append(self.last_error)
                return False, report

            try:
                report["device_present"] = bool(self._probe_device())
            except Exception as exc:
                report["device_present"] = False
                report["errors"].append(f"Probe failed: {exc}")

            try:
                report["details"] = self._collect_diagnostics()
            except Exception as exc:
                report["errors"].append(f"Diagnostics failed: {exc}")

            success = (
                report["initialized"]
                and report["opened"]
                and report["device_present"]
                and not report["errors"]
            )
            return success, report

        except Exception as exc:
            report["errors"].append(f"Unexpected full_test failure: {exc}")
            self._set_error(f"Full test failed: {exc}")
            self.logger.exception("Full test failed: %s", exc)
            return False, report

        finally:
            if temporarily_opened and not original_is_open:
                self.close()
            elif original_is_open:
                self.bus = original_bus
                self.bus_num = original_bus_num
                self.is_open = True

    def deinit(self) -> bool:
        """
        Total cleanup.

        Rules:
        - calls close() internally
        - leaves the module in a neutral state
        """
        self.logger.info("Deinitializing module")
        self._clear_error()

        try:
            self.close()

            self.bus = None
            self.bus_num = None
            self.address = None
            self.bus_candidates = []
            self.bus_forced = False

            self.is_initialized = False
            self.is_open = False

            self._config.clear()

            self.logger.info("Module deinitialized")
            return True

        except Exception as exc:
            self._set_error(f"Deinitialization failed: {exc}")
            self.logger.exception("Deinitialization failed: %s", exc)
            return False
