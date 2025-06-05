from modules.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode, Scheduler
from modules.windsonic_LL import WindsonicLowLevel

class WindsonicHandlerFSM(BaseHandlerFSM):
    def __init__(self):
        super().__init__("Windsonic")
        self.device = WindsonicLowLevel()
        self._pending_params = {}
        self.scheduler = None

    def start_scheduler(self, interval_sec=3600, num_samples=5):
        self._acquire_count = num_samples
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

    def update(self):
        if self._last_state != self.state:
            self._on_entry_flag = True
            self._on_exit_flag = False
            self._last_state = self.state

        if self.state == State.INIT and self._on_entry_flag:
            self.logger.info("Entrando a INIT")
            success = self.device.init()
            result = ResultCode.OK if success else ResultCode.ERROR
            if success:
                # Ejecutar full_test tras init, pero sin afectar el estado
                test_ok, detalles = self.device.full_test()
                self.logger.info(f"[full_test] Resultado global: {test_ok}")
            if self.status_queue:
                self.status_queue.put((self.name, Message(MessageID.STATE_INIT_OK, {"result": result.value})))
            self.set_state(State.TEST if success else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.TEST and self._on_entry_flag:
            self.logger.info("Entrando a TEST")
            success = self.device.test()
            if self.status_queue:
                self.status_queue.put((self.name, Message(
                    MessageID.ACTION_RESULT,
                    {
                        "state": self.state.name,
                        "action": "test",
                        "result": ResultCode.OK.value if success else ResultCode.ERROR.value
                    }
                )))
            self.set_state(State.IDLE if success else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        elif self.state == State.IDLE and self._on_entry_flag:
            self.logger.info("Entrando a IDLE")
            if self.scheduler is None:
                self.start_scheduler(interval_sec=60, num_samples=5)
            self._on_entry_flag = False

        elif self.state == State.ACQUIRE:
            if self._on_entry_flag:
                self.logger.info("Entrando a ACQUIRE")
                # Usar SIEMPRE los valores configurados en el low-level
                success = self.device.acquire()
                result = ResultCode.OK if success else ResultCode.ERROR
                if result == ResultCode.ERROR:
                    self.set_state(State.ERROR, self.status_queue)
                self._on_entry_flag = False

            done, success = self.device.is_acquisition_done()
            if done:
                result = ResultCode.OK if success else ResultCode.ERROR
                if self.status_queue:
                    self.status_queue.put((self.name, Message(
                        MessageID.ACTION_RESULT,
                        {
                            "state": self.state.name,
                            "action": "acquire",
                            "result": result.value
                        }
                    )))
                self.set_state(State.IDLE if result == ResultCode.OK else State.ERROR, self.status_queue)

        elif self.state == State.DISABLE and self._on_entry_flag:
            self.logger.info("Entrando a DISABLE")
            self.stop_scheduler()
            self.device.deinit()
            self._on_entry_flag = False

        elif self.state == State.ERROR and self._on_entry_flag:
            self.logger.error("Entrando a ERROR")
            self.stop_scheduler()
            self.device.deinit()
            self._on_entry_flag = False

    def handle_message(self, message: Message):
        if self.state == State.DISABLE:
            if message.id == MessageID.SIG_INIT:
                self.set_state(State.INIT, self.status_queue)
            return

        if message.id == MessageID.SIG_DEINIT:
            self.set_state(State.DISABLE, self.status_queue)

        elif message.id == MessageID.SIG_TEST:
            self.set_state(State.TEST, self.status_queue)

        elif message.id == MessageID.SIG_ACQUIRE:
            self._pending_params = {"num": message.params.get("num", 5)}
            self.set_state(State.ACQUIRE, self.status_queue)

        elif message.id == MessageID.SIG_TIMEOUT:
            self._pending_params = {"num": getattr(self, '_acquire_count', 5)}
            self.set_state(State.ACQUIRE, self.status_queue)

        elif message.id == MessageID.SIG_QUERY:
            if self.status_queue:
                self.status_queue.put((self.name, Message(
                    MessageID.ACTION_RESULT,
                    {
                        "state": self.state.name,
                        "action": "query",
                        "result": ResultCode.OK.value
                    }
                )))

    def set_config(self, samples=None, spacing=None):
        """Configura los parámetros de Windsonic y los aplica al low-level."""
        if samples is not None or spacing is not None:
            self.device.config(
                samples=samples if samples is not None else self.device.samples,
                spacing=spacing if spacing is not None else self.device.spacing
            )
            self.logger.info(f"Configuración Windsonic actualizada: muestras={self.device.samples}, spacing={self.device.spacing}")
