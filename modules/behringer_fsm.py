from modules.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode, Scheduler
from modules.behringer_LL import BehringerLowLevel
from typing import Optional

class BehringerHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("Behringer")
        self.audio = BehringerLowLevel()
        self._pending_params = {}
        self.status_queue = None
        self.scheduler = None

    def start_scheduler(self, interval_sec=3600, duration_sec=10):
        self._acquire_duration = duration_sec  # Guardar duración para uso propio
        self.scheduler = Scheduler(
            name=self.name,
            queue=self.queue,
            get_state_fn=lambda: self.state,
            interval_sec=interval_sec
        )
        self.scheduler.start()

    def stop_scheduler(self):
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None

    def log_action_result(self, action: str, result: ResultCode):
        if result == ResultCode.OK:
            self.logger.info(f"{action} → OK")
        else:
            self.logger.error(f"{action} → ERROR")

    def update(self):
        if self._last_state != self.state:
            self._on_entry_flag = True
            self._on_exit_flag = False
            self._last_state = self.state

        if self.state == State.INIT and self._on_entry_flag:
            self.logger.info("Entrando a INIT")
            success = self.audio.init()
            result = ResultCode.OK if success else ResultCode.ERROR
            self.log_action_result("Init", result)
            if self.status_queue:
                self.status_queue.put((self.name, Message(MessageID.STATE_INIT_OK, {"result": result.value})))
            self.set_state(State.TEST if result == ResultCode.OK else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.TEST and self._on_entry_flag:
            self.logger.info("Entrando a TEST")
            success = self.audio.test()
            result = ResultCode.OK if success else ResultCode.ERROR
            self.log_action_result("Test", result)
            if self.status_queue:
                self.status_queue.put((self.name, Message(MessageID.STATE_TEST_OK, {"result": result.value})))
            self.set_state(State.IDLE if result == ResultCode.OK else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.IDLE and self._on_entry_flag:
            self.logger.info("Entrando a IDLE")
            if self.scheduler is None:
                self.start_scheduler(interval_sec=60, duration_sec=10)
            self._on_entry_flag = False

        elif self.state == State.ACQUIRE:
            if self._on_entry_flag:
                self.logger.info("Entrando a ACQUIRE")
                duration = self._pending_params.get("duration", 10)
                success = self.audio.record(duration)
                result = ResultCode.OK if success else ResultCode.ERROR
                self.log_action_result("Record", result)
                if result == ResultCode.ERROR:
                    self.set_state(State.ERROR, self.status_queue)
                self._on_entry_flag = False

            done, success = self.audio.is_recording_done()
            if done:
                result = ResultCode.OK if success else ResultCode.ERROR
                self.log_action_result("Fin grabación", result)
                if self.status_queue:
                    self.status_queue.put((self.name, Message(
                        MessageID.ACTION_RESULT,
                        {
                            "state": self.state.name,
                            "action": "record",
                            "result": result.value,
                            "file": self.audio.output_path
                        }
                    )))
                self.set_state(State.IDLE if result == ResultCode.OK else State.ERROR, self.status_queue)

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.logger.info("Entrando a DISABLE")
            self.stop_scheduler()
            if self.audio.audio_interface is not None:
                self.audio.deinit()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.logger.error("Entrando a ERROR")
            self.stop_scheduler()
            if self.audio.audio_interface is not None:
                self.audio.deinit()
            self._on_entry_flag = False

    def handle_message(self, message: Message):
        if self.state == State.DISABLE:
            if message.id == MessageID.SIG_INIT:
                self.set_state(State.INIT, self.status_queue)
            return

        if self.state == State.IDLE and message.id == MessageID.SIG_TIMEOUT:
            # Usar la duración almacenada
            self._pending_params = {"duration": getattr(self, '_acquire_duration', 10)}
            self.set_state(State.ACQUIRE, self.status_queue)
