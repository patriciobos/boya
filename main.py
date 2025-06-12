import logging
from multiprocessing import Process, Queue
from time import sleep

from modules.behringer_fsm import BehringerHandlerFSM
from modules.windsonic_fsm import WindsonicHandlerFSM
from modules.iridium_fsm import IridiumHandlerFSM
from modules.base_fsm import Message, MessageID, State

def setup_logger():
    logger = logging.getLogger("Main")
    logger.setLevel(logging.INFO)

    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

    fh = logging.FileHandler("main.log")
    fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger

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
    logger = setup_logger()
    fsms = {
        "Behringer": launch_fsm(BehringerHandlerFSM, "Behringer"),
        #"Windsonic": launch_fsm(WindsonicHandlerFSM, "Windsonic"),
        "Iridium": launch_fsm(IridiumHandlerFSM, "Iridium"),
    }

    logger.info("FSMs lanzados. Enviando SIG_INIT...")
    for fsm in fsms.values():
        fsm["queue"].put(Message(MessageID.SIG_INIT))

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

                        elif message.id == MessageID.ACTION_RESULT:
                            state = message.params["state"]
                            action = message.params["action"]
                            result = message.params["result"]
                            logger.info(f"[{name}] Acción '{action}' en estado '{state}' → {result.upper()}")

                            if "file" in message.params:
                                logger.info(f"[{name}] Archivo generado: {message.params['file']}")

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
