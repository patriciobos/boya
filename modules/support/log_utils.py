import logging
import os
from typing import Optional

def get_logger(name: str, log_file: Optional[str] = None, level=logging.INFO) -> logging.Logger:
    """
    Crea un logger uniforme para todos los módulos, con formato y handlers estándar.
    - name: nombre del logger (usualmente el nombre del módulo o clase)
    - log_file: nombre del archivo de log (solo nombre, sin ruta) o ruta completa. Si es None, se usa logs/<name>.log
    - level: nivel de logging (por defecto INFO)
    """
    # Centraliza la ubicación de logs en 'logs/'
    logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'logs'))
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir, exist_ok=True)
    if log_file is None or not os.path.dirname(log_file):
        log_file = os.path.join(logs_dir, f"{name}.log")
    else:
        # Si se pasa una ruta absoluta o relativa, la respeta
        log_file = os.path.abspath(log_file)
        log_dir = os.path.dirname(log_file)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    formatter = logging.Formatter('%(asctime)s [%(name)s] %(levelname)s: %(message)s')

    # Handler de archivo
    if not any(isinstance(h, logging.FileHandler) and getattr(h, 'baseFilename', None) == os.path.abspath(log_file) for h in logger.handlers):
        fh = logging.FileHandler(log_file)
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    # Handler de consola
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    return logger
