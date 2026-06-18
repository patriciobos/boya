import time
from datetime import datetime, timedelta
from multiprocessing import Queue
from queue import Empty

from main import ErrorRecoveryManager, centralScheduler
from modules.support.base_fsm import Message, MessageID
from modules.support.system_config import UTC_MINUS_3, now_utc_minus_3


def test_central_scheduler_sends_timeout_to_scheduled_fsm():
    behringer_queue = Queue()
    iridium_queue = Queue()
    fsms = {
        "Behringer": {"queue": behringer_queue},
        "Iridium": {"queue": iridium_queue},
    }

    scheduler = centralScheduler(fsms)
    scheduler.schedules["Behringer"] = 1
    scheduler.schedules["Iridium"] = 2
    now = now_utc_minus_3()
    scheduler.next_run["Behringer"] = now
    scheduler.next_run["Iridium"] = now

    scheduler.start()
    try:
        message = behringer_queue.get(timeout=3)
        assert message.id == MessageID.SIG_TIMEOUT
        assert message.params["origin"] == "Scheduler"
        assert message.params["scheduled_for"] == now.isoformat()

        message = iridium_queue.get(timeout=3)
        assert message.id == MessageID.SIG_TRANSMIT
        assert message.params["mode"] == scheduler._iridium_mode_for_run(now)
        assert message.params["origin"] == "Scheduler"
    finally:
        scheduler.stop()


def test_central_scheduler_does_not_retry_behringer_immediately_on_failure():
    behringer_queue = Queue()
    fsms = {"Behringer": {"queue": behringer_queue}}

    scheduler = centralScheduler(fsms)
    scheduler.schedules["Behringer"] = 600
    scheduler.next_run["Behringer"] = now_utc_minus_3() + timedelta(seconds=3600)
    scheduler.record_action_result(
        "Behringer",
        Message(MessageID.ACTION_RESULT, {"action": "acquire", "result": "error"}),
    )

    scheduler.start()
    try:
        try:
            message = behringer_queue.get(timeout=0.5)
        except Empty:
            message = None
        assert message is None
    finally:
        scheduler.stop()


def test_central_scheduler_aligns_regular_slots_from_midnight():
    queue = Queue()
    fsms = {"Behringer": {"queue": queue}}

    scheduler = centralScheduler(fsms)
    interval = 14400  # 4 hours
    now = datetime(2026, 6, 9, 1, 30, tzinfo=UTC_MINUS_3)
    next_run = scheduler._aligned_next_run(now, interval)

    assert next_run.hour == 4
    assert next_run.minute == 0
    assert next_run.second == 0

    now = datetime(2026, 6, 9, 16, 0, tzinfo=UTC_MINUS_3)
    next_run = scheduler._aligned_next_run(now, interval)
    assert next_run.hour == 16
    assert next_run.minute == 0
    assert next_run.second == 0

    now = datetime(2026, 6, 9, 20, 1, tzinfo=UTC_MINUS_3)
    next_run = scheduler._aligned_next_run(now, interval)
    assert next_run.hour == 0
    assert next_run.minute == 0
    assert next_run.second == 0
    assert next_run.day == 10


def test_central_scheduler_aligns_sensor_and_iridium_slots():
    scheduler = centralScheduler({"AHT10": {"queue": Queue()}, "Iridium": {"queue": Queue()}})

    next_sensor = scheduler._aligned_next_run(datetime(2026, 6, 9, 10, 4, 30, tzinfo=UTC_MINUS_3), 600)
    assert next_sensor.hour == 10
    assert next_sensor.minute == 10
    assert next_sensor.second == 0

    next_iridium = scheduler._aligned_next_run(datetime(2026, 6, 9, 10, 4, 30, tzinfo=UTC_MINUS_3), 3600)
    assert next_iridium.hour == 11
    assert next_iridium.minute == 0
    assert next_iridium.second == 0


def test_central_scheduler_iridium_four_hour_cycle():
    scheduler = centralScheduler({"Iridium": {"queue": Queue()}})

    assert scheduler._iridium_mode_for_run(datetime(2026, 6, 9, 1, 0, tzinfo=UTC_MINUS_3)) == "alive"
    assert scheduler._iridium_mode_for_run(datetime(2026, 6, 9, 2, 0, tzinfo=UTC_MINUS_3)) == "alive"
    assert scheduler._iridium_mode_for_run(datetime(2026, 6, 9, 3, 0, tzinfo=UTC_MINUS_3)) == "alive"
    assert scheduler._iridium_mode_for_run(datetime(2026, 6, 9, 4, 0, tzinfo=UTC_MINUS_3)) == "audio"
    assert scheduler._iridium_mode_for_run(datetime(2026, 6, 9, 5, 0, tzinfo=UTC_MINUS_3)) == "alive"
    assert scheduler._iridium_mode_for_run(datetime(2026, 6, 9, 8, 0, tzinfo=UTC_MINUS_3)) == "audio"


def test_central_scheduler_advances_without_drift():
    scheduler = centralScheduler({"AHT10": {"queue": Queue()}})
    previous = datetime(2026, 6, 9, 10, 0, tzinfo=UTC_MINUS_3)
    now = datetime(2026, 6, 9, 10, 25, 3, tzinfo=UTC_MINUS_3)

    next_run = scheduler._advance_next_run(previous, now, 600)

    assert next_run == datetime(2026, 6, 9, 10, 30, tzinfo=UTC_MINUS_3)


class CapturingLogger:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, message, *args):
        self.warnings.append(message % args)

    def error(self, message, *args):
        self.errors.append(message % args)


class CapturingStatusReport:
    def __init__(self):
        self.updates = []

    def update(self, *args):
        self.updates.append(args)


def test_error_recovery_manager_sends_sig_init_and_marks_irrecoverable():
    queue = Queue()
    logger = CapturingLogger()
    status_report = CapturingStatusReport()
    recovery = ErrorRecoveryManager({"AHT10": {"queue": queue}}, max_attempts=3)

    for expected_attempt in (1, 2, 3):
        recovery.handle_state("AHT10", "ERROR", logger, status_report)
        message = queue.get(timeout=1)
        assert message.id == MessageID.SIG_INIT
        assert message.params == {"origin": "main", "recovery_attempt": expected_attempt}

    recovery.handle_state("AHT10", "ERROR", logger, status_report)

    assert "AHT10" in recovery.irrecoverable
    assert logger.errors[-1] == "[AHT10] Module failed irrecoverably after 3 recovery attempts"
    assert status_report.updates[-1][0:4] == ("AHT10", "ERROR", "recover", "error")
    assert status_report.updates[-1][4]["error"] == "Module failed irrecoverably"
