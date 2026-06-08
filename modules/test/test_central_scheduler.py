import time
from datetime import datetime, timedelta
from multiprocessing import Queue
from queue import Empty

from main import CentralScheduler
from modules.support.base_fsm import Message, MessageID


def test_central_scheduler_sends_timeout_to_scheduled_fsm():
    behringer_queue = Queue()
    iridium_queue = Queue()
    fsms = {
        "Behringer": {"queue": behringer_queue},
        "Iridium": {"queue": iridium_queue},
    }

    scheduler = CentralScheduler(fsms)
    scheduler.schedules["Behringer"] = 1
    scheduler.schedules["Iridium"] = 2
    now = datetime.utcnow()
    scheduler.next_run["Behringer"] = now
    scheduler.next_run["Iridium"] = now

    scheduler.start()
    try:
        message = behringer_queue.get(timeout=3)
        assert message.id == MessageID.SIG_TIMEOUT
        assert message.params == {}

        message = iridium_queue.get(timeout=3)
        assert message.id == MessageID.SIG_TRANSMIT
        assert message.params["text"] == "alive"
        assert message.params["mode"] == "text"
    finally:
        scheduler.stop()


def test_central_scheduler_retries_behringer_on_failure():
    behringer_queue = Queue()
    fsms = {"Behringer": {"queue": behringer_queue}}

    scheduler = CentralScheduler(fsms)
    scheduler.schedules["Behringer"] = 600
    scheduler.next_run["Behringer"] = datetime.utcnow() + timedelta(seconds=3600)
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
