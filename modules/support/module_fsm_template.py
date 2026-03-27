"""
Template FSM module for new hardware/software handlers.

This class provides a finite state machine (FSM) structure for managing the lifecycle and actions of a hardware or software module.
It communicates upstream via a message queue and interacts with its low-level driver via direct method calls.
"""

from modules.support.base_fsm import BaseHandlerFSM, State, Message, MessageID, ResultCode, Scheduler
from modules.support.module_LL_template import ModuleLowLevel  # Replace with actual LL module name

class ModuleHandlerFSM(BaseHandlerFSM):
    """
    FSM handler for a generic module.
    """

    def __init__(self):
        super().__init__("ModuleName")
        self.ll = ModuleLowLevel()
        self._pending_params = {}
        self.status_queue = None
        self.scheduler = None

    def start_scheduler(self, interval_sec=3600, duration_sec=10):
        """
        Start the periodic scheduler for automatic acquisition or actions.

        Args:
            interval_sec (int): Interval between actions in seconds.
            duration_sec (int): Duration of each acquisition/action.
        """
        self._acquire_duration = duration_sec
        self.scheduler = Scheduler(
            name=self.name,
            queue=self.queue,
            get_state_fn=lambda: self.state,
            interval_sec=interval_sec
        )
        self.scheduler.start()

    def stop_scheduler(self):
        """
        Stop the periodic scheduler if running.
        """
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None

    def log_action_result(self, action: str, result: ResultCode):
        """
        Log the result of an action.

        Args:
            action (str): Action name.
            result (ResultCode): Result code.
        """
        if result == ResultCode.OK:
            self.logger.info(f"{action} → OK")
        else:
            self.logger.error(f"{action} → ERROR")

    def handle_message(self, message: Message):
        """
        Handle incoming messages for FSM control.

        Args:
            message (Message): The message to handle.
        """
        if self.state == State.DISABLE:
            if message.id == MessageID.SIG_INIT:
                self.set_state(State.INIT, self.status_queue)
            return

        if self.state == State.IDLE and message.id == MessageID.SIG_TIMEOUT:
            self._pending_params = {"duration": getattr(self, '_acquire_duration', 10)}
            self.set_state(State.ACQUIRE, self.status_queue)

    def update(self):
        """
        Main FSM update loop. Handles state transitions and actions.
        """
        if self._last_state != self.state:
            self._on_entry_flag = True
            self._on_exit_flag = False
            self._last_state = self.state

        #############
        # state INIT
        #############
        if self.state == State.INIT and self._on_entry_flag:
            self.logger.info("Entering INIT")
            success = self.ll.init()
            result = ResultCode.OK if success else ResultCode.ERROR
            self.log_action_result("Init", result)
            if self.status_queue:
                self.status_queue.put((self.name, Message(MessageID.STATE_RESULT, {"result": result.value})))
            self.set_state(State.TEST if result == ResultCode.OK else State.ERROR, self.status_queue)
            self._on_entry_flag = False

        #############
        # state TEST
        #############
        elif self.state == State.TEST and self._on_entry_flag:
            self.logger.info("Entering TEST")
            test_ok, details = self.ll.full_test()
            if test_ok:
                self.logger.info("[TEST] full_test OK")
            else:
                self.logger.error("[TEST] full_test ERROR")
            
            # upstream message with test result
            if self.status_queue:
                self.status_queue.put((self.name, Message(
                    MessageID.ACTION_RESULT,
                    {
                        "state": self.state.name,
                        "action": "test",
                        "result": ResultCode.OK.value if test_ok else ResultCode.ERROR.value
                    }
                )))
            self.set_state(State.IDLE if test_ok else State.ERROR, self.status_queue)
            self._on_entry_flag = False
        
        #############
        # state IDLE
        #############
        elif self.state == State.IDLE and self._on_entry_flag:
            self.logger.info("Entering IDLE")
            if self.scheduler is None:
                self.start_scheduler(interval_sec=60, duration_sec=10)
            self._on_entry_flag = False

        ################
        # state DISABLE
        ################
        elif self.state == State.DISABLE and self._on_entry_flag:
            self.logger.info("Entering DISABLE")
            self.stop_scheduler()
            self.ll.deinit()
            self._on_entry_flag = False

        ##############
        # state ERROR
        ##############
        elif self.state == State.ERROR and self._on_entry_flag:
            self.logger.error("Entering ERROR")
            self.stop_scheduler()
            self.ll.deinit()
            self.set_state(State.INIT, self.status_queue)
            self._on_entry_flag = False

    