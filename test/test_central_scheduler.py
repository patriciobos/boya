import time
from datetime import datetime, timedelta, timezone
from multiprocessing import Queue
from queue import Empty

from main import centralScheduler
from modules.support.base_fsm import Message, MessageID


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
    now = datetime.now(timezone.utc)
    scheduler.next_run["Behringer"] = now
    scheduler.next_run["Iridium"] = now

    scheduler.start()
    try:
        message = behringer_queue.get(timeout=3)
        assert message.id == MessageID.SIG_TIMEOUT
        assert message.params == {}

        message = iridium_queue.get(timeout=3)
        assert message.id == MessageID.SIG_TRANSMIT
        assert message.params["mode"] == "alive"
        assert message.params["origin"] == "Scheduler"
    finally:
        scheduler.stop()


def test_central_scheduler_retries_behringer_on_failure():
    behringer_queue = Queue()
    fsms = {"Behringer": {"queue": behringer_queue}}

    scheduler = centralScheduler(fsms)
    scheduler.schedules["Behringer"] = 600
    scheduler.next_run["Behringer"] = datetime.now(timezone.utc) + timedelta(seconds=3600)
    scheduler.record_action_result(
        "Behringer",
        Message(MessageID.ACTION_RESULT, {"action": "acquire", "result": "error"}),
    )

    scheduler.start()
    try:
        message = behringer_queue.get(timeout=3)
        assert message.id == MessageID.SIG_ACQUIRE
    finally:
        scheduler.stop()


def test_central_scheduler_aligns_behringer_to_midnight():
    queue = Queue()
    fsms = {"Behringer": {"queue": queue}}

    scheduler = centralScheduler(fsms)
    interval = 14400  # 4 hours
    now = datetime(2026, 6, 9, 1, 30, tzinfo=timezone.utc)
    next_run = scheduler._aligned_next_run(now, interval)

    assert next_run.hour == 4
    assert next_run.minute == 0
    assert next_run.second == 0

    now = datetime(2026, 6, 9, 16, 0, tzinfo=timezone.utc)
    next_run = scheduler._aligned_next_run(now, interval)
    assert next_run.hour == 16
    assert next_run.minute == 0
    assert next_run.second == 0

    now = datetime(2026, 6, 9, 20, 1, tzinfo=timezone.utc)
    next_run = scheduler._aligned_next_run(now, interval)
    assert next_run.hour == 0
    assert next_run.minute == 0
    assert next_run.second == 0
    assert next_run.day == 10
