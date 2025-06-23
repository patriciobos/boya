import logging
import os

def get_logger(name: str, log_file: str, level=logging.INFO) -> logging.Logger:
    """
    Crea un logger uniforme para todos los módulos, con formato y handlers estándar.
    - name: nombre del logger (usualmente el nombre del módulo o clase)
    - log_file: ruta al archivo de log
    - level: nivel de logging (por defecto INFO)
    """
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
