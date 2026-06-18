import threading
import time
from multiprocessing import Queue

from modules.support.base_fsm import BaseHandlerFSM, Message, MessageID


class RecordingFSM(BaseHandlerFSM):
    def __init__(self) -> None:
        super().__init__("Recording")
        self.messages: list[Message] = []
        self.update_count = 0

    def handle_message(self, message: Message) -> None:
        self.messages.append(message)
        self.running = False

    def update(self) -> None:
        self.update_count += 1


def test_run_receives_message_from_multiprocessing_queue() -> None:
    message_queue = Queue()
    fsm = RecordingFSM()
    thread = threading.Thread(target=fsm.run, args=(message_queue,))

    thread.start()
    message_queue.put(Message(MessageID.SIG_INIT, {"origin": "test"}))
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert fsm.messages == [Message(MessageID.SIG_INIT, {"origin": "test"})]


def test_run_handles_empty_queue_and_stops_cleanly() -> None:
    message_queue = Queue()
    fsm = RecordingFSM()
    thread = threading.Thread(target=fsm.run, args=(message_queue,))

    thread.start()
    time.sleep(0.15)
    fsm.running = False
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert fsm.messages == []
    assert fsm.update_count >= 1
