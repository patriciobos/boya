from multiprocessing import Process, Queue
from time import sleep
import os

from modules.aht10_fsm import AHT10HandlerFSM
from modules.ais_fsm import AISHandlerFSM
from modules.audioProc_fsm import AudioProcHandlerFSM
from modules.behringer_fsm import BehringerHandlerFSM
from modules.iridium_fsm import IridiumHandlerFSM
from modules.mpu6050_fsm import MPU6050HandlerFSM
from modules.support.base_fsm import Message, MessageID, State
from modules.support.ll_factory import get_mocked_module_names, is_mock_enabled, validate_mock_configuration
from modules.support.log_utils import get_logger
from modules.support.router import Router
from modules.support.status_report import StatusReport
from modules.windsonic_fsm import WindsonicHandlerFSM
from modules.xtra2210_fsm import XTRA2210HandlerFSM
from modules.support.system_config import get_schedule, now_utc_minus_3
import threading
import time
import math
from datetime import datetime, timedelta

def launch_fsm(handler_class, name):
    queue = Queue()
    status_queue = Queue()
    handler = handler_class()
    process = Process(target=handler.run, args=(queue, status_queue), daemon=True)
    process.start()
    return {
        "name": name,
        "queue": queue,
        "status_queue": status_queue,
        "process": process,
        "handler": handler
    }


class centralScheduler:
    """Central scheduler that sends SIG_TIMEOUT/SIG_TRANSMIT to FSM queues.

    Responsibilities:
    - Send periodic SIG_TIMEOUT to FSMs on regular wall-clock slots (UTC-3).
    - Send SIG_TIMEOUT to Behringer every 14400s (4h) and retry up to 2 times on failure.
    - Send hourly Iridium SIG_TRANSMIT on regular hours: alive, alive, alive, audio.
    """

    def __init__(self, fsms: dict[str, dict]):
        self.fsms = fsms
        self._stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

        # load schedules from system config
        self.schedules = {}
        for name in fsms.keys():
            val = get_schedule(name)
            self.schedules[name] = int(val) if val is not None else None

        # default overrides per user request
        defaults = {
            "AHT10": 600,
            "AIS": 600,
            "MPU6050": 600,
            "Windsonic": 600,
            "XTRA2210": 600,
            "Behringer": 14400,
            "Iridium": 3600,
            "AudioProc": None,
        }
        for k, v in defaults.items():
            if k in self.schedules and (self.schedules[k] is None):
                self.schedules[k] = v

        now = now_utc_minus_3()
        self.next_run: dict[str, datetime] = {}
        for name, interval in self.schedules.items():
            if interval is None:
                continue
            self.next_run[name] = self._aligned_next_run(now, interval)

        # Behringer retry bookkeeping
        self.behringer_retries: int = 0
        self.behringer_last_attempt: datetime | None = None
        self.max_behringer_retries = 2

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop_event.set()
        if self.thread.is_alive():
            self.thread.join()

    def record_action_result(self, origin: str, message: Message):
        # Used to track Behringer failures for retries
        try:
            if origin == "Behringer" and message.id == MessageID.ACTION_RESULT:
                result = message.params.get("result")
                action = message.params.get("action")
                if action == "acquire":
                    if result == "error":
                        # schedule immediate retry if under limit
                        self.behringer_retries += 1
                    else:
                        # reset on success
                        self.behringer_retries = 0
        except Exception:
            pass

    def _aligned_next_run(self, now: datetime, interval_seconds: int) -> datetime:
        """Return the next run aligned to regular slots from midnight UTC-3."""
        midnight = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
        seconds_since_midnight = (now - midnight).total_seconds()
        next_seconds = math.ceil(seconds_since_midnight / interval_seconds) * interval_seconds
        next_run = midnight + timedelta(seconds=next_seconds)
        if next_run < now:
            next_run += timedelta(seconds=interval_seconds)
        return next_run

    def _advance_next_run(self, previous_run: datetime, now: datetime, interval_seconds: int) -> datetime:
        if previous_run > now:
            return previous_run
        missed_slots = int((now - previous_run).total_seconds() // interval_seconds) + 1
        return previous_run + timedelta(seconds=missed_slots * interval_seconds)

    def _iridium_mode_for_run(self, run_at: datetime) -> str:
        # Four-hour cycle anchored at midnight UTC-3: 01/02/03 alive, 04 audio.
        return "audio" if run_at.hour % 4 == 0 else "alive"

    def _run(self):
        while not self._stop_event.is_set():
            now = now_utc_minus_3()
            for name, interval in list(self.schedules.items()):
                if interval is None:
                    continue
                nr = self.next_run.get(name)
                if nr is None:
                    self.next_run[name] = self._aligned_next_run(now, interval)
                    continue
                if now >= nr:
                    # send scheduling message
                    entry = self.fsms.get(name)
                    if entry:
                        q = entry.get("queue")
                        if q:
                            if name == "Iridium":
                                mode = self._iridium_mode_for_run(nr)
                                q.put(Message(MessageID.SIG_TRANSMIT, {"mode": mode, "origin": "Scheduler", "scheduled_for": nr.isoformat()}))
                            else:
                                q.put(Message(MessageID.SIG_TIMEOUT, {"origin": "Scheduler", "scheduled_for": nr.isoformat()}))
                    # increment next_run from the regular slot, not from current time, to avoid drift
                    self.next_run[name] = self._advance_next_run(nr, now, interval)

            # Behringer retries: if retries >0 and <= max, trigger an immediate SIG_ACQUIRE
            if self.behringer_retries and self.behringer_retries <= self.max_behringer_retries:
                entry = self.fsms.get("Behringer")
                if entry and entry.get("queue"):
                    # send immediate retry
                    entry.get("queue").put(Message(MessageID.SIG_ACQUIRE))
                    self.behringer_retries = 0  # counted as dispatched; further failures will increment again

            time.sleep(1)

if __name__ == "__main__":
    logger = get_logger("main")
    mock_config = validate_mock_configuration()
    mock_modules = get_mocked_module_names()
    if mock_modules:
        logger.warning(
            "Mock mode enabled for modules=%s config_modules=%s env_modules=%s all_mock=%s",
            mock_modules,
            mock_config["config_modules"],
            mock_config["env_modules"],
            mock_config["all_mock"],
        )
    if is_mock_enabled() and mock_config["all_mock"]:
        logger.warning("USE_LL_MOCKS is enabled: all LL modules will use mocks")
    fsms = {
        "Behringer": launch_fsm(BehringerHandlerFSM, "Behringer"),
        "AudioProc": launch_fsm(AudioProcHandlerFSM, "AudioProc"),
        "Windsonic": launch_fsm(WindsonicHandlerFSM, "Windsonic"),
        "Iridium": launch_fsm(IridiumHandlerFSM, "Iridium"),
        "AHT10": launch_fsm(AHT10HandlerFSM, "AHT10"),
        "AIS": launch_fsm(AISHandlerFSM, "AIS"),
        "MPU6050": launch_fsm(MPU6050HandlerFSM, "MPU6050"),
        "XTRA2210": launch_fsm(XTRA2210HandlerFSM, "XTRA2210"),
    }

    router = Router()
    for name, fsm in fsms.items():
        router.register(name, fsm["queue"])

    logger.info("FSMs lanzados. Enviando SIG_INIT...")
    for fsm in fsms.values():
        fsm["queue"].put(Message(MessageID.SIG_INIT))

    # Start central scheduler
    central_scheduler = centralScheduler(fsms)
    central_scheduler.start()

    status_report = StatusReport()
    try:
        while True:
            for fsm_id, fsm in list(fsms.items()):
                try:
                    msg = fsm["status_queue"].get_nowait()
                    if isinstance(msg, tuple) and isinstance(msg[1], Message):
                        name, message = msg
                        if message.id == MessageID.STATE_CHANGED:
                            state_name = message.params["state"]
                            logger.info(f"[{name}] Nuevo estado: {state_name}")
                            status_report.update(name, state_name, None, None, {})
                        elif message.id == MessageID.ACTION_RESULT:
                            state = message.params["state"]
                            action = message.params["action"]
                            result = message.params["result"]
                            logger.info(f"[{name}] Acción '{action}' en estado '{state}' → {result.upper()}")
                            if "file" in message.params:
                                logger.info(f"[{name}] Archivo generado: {message.params['file']}")
                            status_report.update(name, state, action, result, message.params)
                            # Let central scheduler know about action results (for retries, etc.)
                            try:
                                central_scheduler.record_action_result(name, message)
                            except Exception:
                                pass

                        router.route(name, message)
                        status_report.write()

                except Exception:
                    continue

            if not fsms:
                logger.info("Todos los FSM han finalizado.")
                break

            sleep(1)

    except KeyboardInterrupt:
        logger.warning("Interrupción detectada. Finalizando FSMs...")
        for fsm in fsms.values():
            fsm["process"].terminate()
            fsm["process"].join()
        logger.info("Todos los FSMs han sido detenidos correctamente.")
