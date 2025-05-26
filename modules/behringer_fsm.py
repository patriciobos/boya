from modules.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode
from modules.behringer_LL import BehringerLowLevel
from typing import Optional

class BehringerHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("Behringer")
        self.audio = BehringerLowLevel()
        self._pending_params = {}

    def notify_action_result(self, action: str, result: ResultCode, extra: Optional[dict] = None):
        msg = {
            "state": self.state.name,
            "action": action,
            "result": result.value
        }
        if extra:
            msg.update(extra)
        if self.status_queue:
            self.status_queue.put((self.name, Message(MessageID.ACTION_RESULT, msg)))

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

        if self.state == State.DISABLE:
            if self._on_entry_flag:
                self.logger.info("Entrando a DISABLE")
                if self.audio.audio_interface is not None:
                    self.logger.info("Ejecutando deinit() automático en DISABLE.")
                    success = self.audio.deinit()
                    result = ResultCode.OK if success else ResultCode.ERROR
                    self.log_action_result("Auto-Deinit", result)
                self._on_entry_flag = False
            if self._on_exit_flag:
                self.logger.info("Saliendo de DISABLE")
                self._on_exit_flag = False

        elif self.state == State.INIT:
            if self._on_entry_flag:
                self.logger.info("Entrando a INIT")
                success = self.audio.init()
                result = ResultCode.OK if success else ResultCode.ERROR
                self.log_action_result("Init", result)
                self.notify_action_result("init", result)
                if self.status_queue:
                    self.status_queue.put((self.name, Message(MessageID.STATE_INIT_OK, {"result": result.value})))
                self.set_state(State.TEST if result == ResultCode.OK else State.ERROR, self.status_queue)
                self._on_entry_flag = False
            if self._on_exit_flag:
                self.logger.info("Saliendo de INIT")
                self._on_exit_flag = False

        elif self.state == State.TEST:
            if self._on_entry_flag:
                self.logger.info("Entrando a TEST")
                success = self.audio.test()
                result = ResultCode.OK if success else ResultCode.ERROR
                self.log_action_result("Test", result)
                self.notify_action_result("test", result)
                if self.status_queue:
                    self.status_queue.put((self.name, Message(MessageID.STATE_TEST_OK, {"result": result.value})))
                self.set_state(State.IDLE if result == ResultCode.OK else State.ERROR, self.status_queue)
                self._on_entry_flag = False
            if self._on_exit_flag:
                self.logger.info("Saliendo de TEST")
                self._on_exit_flag = False

        elif self.state == State.ACQUIRE:
            if self._on_entry_flag:
                self.logger.info("Entrando a ACQUIRE")
                duration = self._pending_params.get("duration", 5)
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
                self.notify_action_result("record", result, {"file": self.audio.output_path})
                self.set_state(State.IDLE if result == ResultCode.OK else State.ERROR, self.status_queue)

            if self._on_exit_flag:
                self.logger.info("Saliendo de ACQUIRE")
                self._on_exit_flag = False

        elif self.state == State.IDLE:
            if self._on_entry_flag:
                self.logger.info("Entrando a IDLE")
                self._on_entry_flag = False
            if self._on_exit_flag:
                self.logger.info("Saliendo de IDLE")
                self._on_exit_flag = False

        elif self.state == State.ERROR:
            if self._on_entry_flag:
                self.logger.error("Entrando a ERROR")
                self._on_entry_flag = False
            if self._on_exit_flag:
                self.logger.info("Saliendo de ERROR")
                self._on_exit_flag = False

    def handle_message(self, message: Message):
        if self.state == State.DISABLE:
            if message.id == MessageID.SIG_INIT:
                self.set_state(State.INIT, self.status_queue)
            else:
                self.logger.warning(f"[DISABLE] Mensaje descartado: {message.id.value}")
            return

        if self.state == State.IDLE and message.id == MessageID.SIG_ACQUIRE:
            self._pending_params = message.params
            self.set_state(State.ACQUIRE, self.status_queue)

        elif message.id == MessageID.SIG_DEINIT:
            self.logger.info("Finalizando BehringerLowLevel desde FSM...")
            self.audio.stop_recording()
            success = self.audio.deinit()
            result = ResultCode.OK if success else ResultCode.ERROR
            self.log_action_result("Deinit", result)
            self.notify_action_result("deinit", result)
            self.set_state(State.DISABLE, self.status_queue)

        elif message.id == MessageID.SIG_QUERY:
            self.logger.info(f"Consulta de estado actual: {self.state.name}")

        else:
            self.logger.warning(f"Transición no válida: {self.state} con {message.id}")
