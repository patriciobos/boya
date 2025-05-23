
import logging
from multiprocessing import Process, Queue
from time import sleep

from modules.behringer_fsm import BehringerHandlerFSM
from modules.base_fsm import Message, MessageID, State

def setup_logger():
    logger = logging.getLogger("Main")
    logger.setLevel(logging.INFO)

    # Consola
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

    # Archivo
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
        "process": process
    }

if __name__ == "__main__":
    logger = setup_logger()
    fsms = {
        "Behringer": launch_fsm(BehringerHandlerFSM, "Behringer")
    }

    logger.info("FSMs lanzados. Enviando SIG_INIT...")
    fsms["Behringer"]["queue"].put(Message(MessageID.SIG_INIT))

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

                            if state_name == State.IDLE.name:
                                fsm["queue"].put(Message(MessageID.SIG_ACQUIRE, {"duration": 3}))
                                sleep(4)



                except Exception:
                    continue

            if not fsms:
                logger.info("Todos los FSM han finalizado.")
                break

            sleep(1)

    except KeyboardInterrupt:
        logger.warning("Interrupción detectada. Terminando FSMs...")
        for fsm in fsms.values():
            fsm["process"].terminate()
            fsm["process"].join()
        logger.info("Finalizado.")
