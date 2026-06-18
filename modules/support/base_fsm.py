from enum import Enum, auto
from dataclasses import dataclass, field
from multiprocessing import Queue
import time
from typing import Any, Dict, Optional
import json


from modules.support.log_utils import get_logger


# ------------------------------------------------------------------
# States
# ------------------------------------------------------------------

class State(Enum):
    DISABLE = auto()
    INIT = auto()
    TEST = auto()
    IDLE = auto()
    ACQUIRE = auto()
    PROCESS = auto()
    TRANSMIT = auto()
    ERROR = auto()


# ------------------------------------------------------------------
# Messages
# ------------------------------------------------------------------

class MessageID(Enum):
    SIG_INIT = "sig_init"
    SIG_DEINIT = "sig_deinit"
    SIG_TEST = "sig_test"
    SIG_ACQUIRE = "sig_acquire"
    SIG_PROCESS = "sig_process"
    SIG_TRANSMIT = "sig_transmit"
    SIG_QUERY = "sig_query"
    SIG_TIMEOUT = "sig_timeout"

    STATE_CHANGED = "state_changed"
    STATE_RESULT = "state_result"
    ACTION_RESULT = "action_result"

    # legacy / transitional
    STATE_TEST_OK = "state_test_ok"
    RECORDING_DONE = "recording_done"


# ------------------------------------------------------------------
# Result codes
# ------------------------------------------------------------------

class ResultCode(Enum):
    OK = "ok"
    ERROR = "error"


# ------------------------------------------------------------------
# Message container
# ------------------------------------------------------------------

@dataclass
class Message:
    id: MessageID
    params: Dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------
# Base FSM
# ------------------------------------------------------------------

class BaseHandlerFSM:
    def __init__(self, name: str):
        self.name = name
        self.state = State.DISABLE
        self.logger = get_logger(self.name)
        self.running = True

        self._on_entry_flag = True
        self._on_exit_flag = False
        self._last_state = None

        self.queue: Optional[Queue] = None
        self.status_queue: Optional[Queue] = None

    # --------------------------------------------------------------
    # Lifecycle hooks (override in child classes)
    # --------------------------------------------------------------

    def update(self):
        pass

    def handle_message(self, message: Message):
        pass

    def _ignore_scheduler_while_error(self, message: Message) -> bool:
        """Handle allowed ERROR messages and ignore scheduler work during ERROR."""
        if self.state != State.ERROR:
            return False
        params = getattr(message, "params", {}) or {}
        if message.id == MessageID.SIG_INIT:
            self.set_state(State.INIT, self.status_queue)
            return True
        if message.id == MessageID.SIG_DEINIT:
            self.set_state(State.DISABLE, self.status_queue)
            return True
        blocked_messages = {
            MessageID.SIG_ACQUIRE,
            MessageID.SIG_PROCESS,
            MessageID.SIG_TIMEOUT,
            MessageID.SIG_TRANSMIT,
        }
        if params.get("origin") == "Scheduler" or message.id in blocked_messages:
            self.logger.warning(
                "Ignoring operational message while in ERROR: %s | Params: %s",
                message.id.value,
                params,
            )
            return True
        return False

    # --------------------------------------------------------------
    # Runtime loop
    # --------------------------------------------------------------

    def run(self, queue: Queue, status_queue: Optional[Queue] = None):
        self.logger.info("FSM started")
        self.queue = queue
        self.status_queue = status_queue

        try:
            while self.running:
                if not queue.empty():
                    msg = queue.get()
                    self.logger.info(
                        "Received: %s | Params: %s",
                        msg.id.value,
                        msg.params,
                    )
                    if self.state == State.ERROR and msg.id in {
                        MessageID.SIG_ACQUIRE,
                        MessageID.SIG_PROCESS,
                        MessageID.SIG_TIMEOUT,
                        MessageID.SIG_TRANSMIT,
                    }:
                        self.logger.warning(
                            "Run loop ignored operational message while in ERROR: %s | Params: %s",
                            msg.id.value,
                            msg.params,
                        )
                        self.update()
                        continue
                    self.handle_message(msg)
                else:
                    time.sleep(0.1)

                self.update()

        except KeyboardInterrupt:
            self.logger.info("FSM stopped by KeyboardInterrupt")
            self.running = False

    # --------------------------------------------------------------
    # State management
    # --------------------------------------------------------------

    def set_state(self, new_state: State, status_queue: Optional[Queue] = None):
        if self.state != new_state:
            self._on_exit_flag = True

            self.logger.info(
                "State change: %s -> %s",
                self.state.name,
                new_state.name,
            )

            self.state = new_state

            if status_queue:
                status_queue.put((
                    self.name,
                    Message(
                        MessageID.STATE_CHANGED,
                        {"state": self.state.name},
                    ),
                ))


def run_fsm_self_test(
    fsm,
    timeout_s: float = 30.0,
    init_timeout_s: float = 20.0,
    extra_messages: Optional[list[Message]] = None,
) -> tuple[bool, dict]:
    """
    Functional self-test for FSM modules.

    It runs the FSM in-process, sends SIG_INIT, waits for a stable result,
    optionally sends extra messages, and returns a standard report.
    """
    queue = Queue()
    status_queue = Queue()

    fsm.queue = queue
    fsm.status_queue = status_queue

    report = {
        "fsm": fsm.name,
        "success": False,
        "final_state": None,
        "messages": [],
        "errors": [],
    }

    def drain_status():
        while not status_queue.empty():
            name, message = status_queue.get()
            report["messages"].append({
                "name": name,
                "id": message.id.value,
                "params": message.params,
            })

    try:
        queue.put(Message(MessageID.SIG_INIT))

        start = time.time()
        while time.time() - start < init_timeout_s:
            if not queue.empty():
                msg = queue.get()
                fsm.handle_message(msg)

            fsm.update()
            drain_status()

            if fsm.state in (State.IDLE, State.ERROR):
                break

            time.sleep(0.1)

        if extra_messages:
            for msg in extra_messages:
                queue.put(msg)

            start = time.time()
            while time.time() - start < timeout_s:
                if not queue.empty():
                    msg = queue.get()
                    fsm.handle_message(msg)

                fsm.update()
                drain_status()

                if fsm.state in (State.IDLE, State.ERROR):
                    # allow one extra update cycle after returning to IDLE/ERROR
                    fsm.update()
                    drain_status()
                    break

                time.sleep(0.1)

        report["final_state"] = fsm.state.name
        report["success"] = fsm.state == State.IDLE

        return bool(report["success"]), report

    except Exception as exc:
        report["errors"].append(str(exc))
        report["final_state"] = getattr(fsm.state, "name", None)
        return False, report

    finally:
        try:
            if hasattr(fsm, "ll"):
                fsm.ll.deinit()
        except Exception:
            pass
