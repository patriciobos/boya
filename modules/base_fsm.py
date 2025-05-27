from enum import Enum, auto
from dataclasses import dataclass, field
from multiprocessing import Queue
import logging
import time
from typing import Any, Dict, Optional
import threading
from datetime import datetime, timedelta

class State(Enum):
    DISABLE = auto()
    INIT = auto()
    IDLE = auto()
    ACQUIRE = auto()
    PROCESS = auto()
    TEST = auto()
    ERROR = auto()

class MessageID(Enum):
    SIG_INIT = "sig_init"
    SIG_DEINIT = "sig_deinit"
    SIG_ACQUIRE = "sig_acquire"
    SIG_PROCESS = "sig_process"
    SIG_TEST = "sig_test"
    SIG_QUERY = "sig_query"
    SIG_TIMEOUT = "sig_timeout"
    STATE_CHANGED = "state_changed"
    STATE_INIT_OK = "state_init_ok"
    STATE_TEST_OK = "state_test_ok"
    ACTION_RESULT = "action_result"

class ResultCode(Enum):
    OK = "ok"
    ERROR = "error"

@dataclass
class Message:
    id: MessageID
    params: Dict[str, Any] = field(default_factory=dict)

class BaseHandlerFSM:
    def __init__(self, name):
        self.name = name
        self.state = State.DISABLE
        self.logger = self._create_logger()
        self.running = True

        self._on_entry_flag = True
        self._on_exit_flag = False
        self._last_state = None

    def _create_logger(self):
        logger = logging.getLogger(self.name)
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        formatter = logging.Formatter(f"%(asctime)s [{self.name}] %(levelname)s: %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        return logger

    def update(self):
        pass

    def run(self, queue: Queue, status_queue: Optional[Queue] = None):
        self.logger.info("FSM iniciada.")
        self.queue = queue
        self.status_queue = status_queue
        while self.running:
            if not queue.empty():
                msg = queue.get()
                self.logger.info(f"Recibido: {msg.id.value} | Params: {msg.params}")
                self.handle_message(msg)
            else:
                time.sleep(0.1)
            self.update()

    def handle_message(self, message: Message):
        pass

    def set_state(self, new_state: State, status_queue: Optional[Queue] = None):
        if self.state != new_state:
            self._on_exit_flag = True
            self.logger.info(f"Cambio de estado: {self.state.name} → {new_state.name}")
            self.state = new_state
            if status_queue:
                status_queue.put((self.name, Message(MessageID.STATE_CHANGED, {"state": self.state.name})))

class Scheduler:
    def __init__(self, name, queue, get_state_fn, interval_sec=3600):
        self.name = name
        self.queue = queue
        self.get_state = get_state_fn
        self.interval = interval_sec
        self.last_event = datetime.min
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.stop_event = threading.Event()

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()

    def _run(self):
        while not self.stop_event.is_set():
            now = datetime.now()
            if (
                self.get_state() == State.IDLE
                and now - self.last_event >= timedelta(seconds=self.interval)
            ):
                print(f"[{self.name}] Timer cumplido. Enviando SIG_TIMEOUT.")
                self.queue.put(Message(MessageID.SIG_TIMEOUT))
                self.last_event = now
            time.sleep(5)
