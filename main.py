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
from modules.support.ll_factory import is_mock_enabled
from modules.support.log_utils import get_logger
from modules.support.router import Router
from modules.support.status_report import StatusReport
from modules.windsonic_fsm import WindsonicHandlerFSM
from modules.xtra2210_fsm import XTRA2210HandlerFSM

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

if __name__ == "__main__":
    logger = get_logger("main")
    if is_mock_enabled():
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
            fsm["handler"].stop_scheduler()
            fsm["process"].terminate()
            fsm["process"].join()
        logger.info("Todos los FSMs han sido detenidos correctamente.")
