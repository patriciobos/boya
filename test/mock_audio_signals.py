import os
import numpy as np
import wave
from datetime import datetime

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.log_utils import get_logger

try:
    from scipy.signal import lfilter
except ImportError:
    lfilter = None

# Parámetros configurables
TIPO_RUIDO = "blanco"  # Opciones: "blanco", "rosa", "marron"
FS = 196000            # Frecuencia de muestreo (Hz)
BITS = 24              # Resolución vertical (bits)
DURACION = 5           # Duración del archivo (segundos)
N_CANALES = 1          # Mono=1, Estéreo=2

# Carpeta de salida
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDINGS_DIR = os.path.join(BASE_DIR, "modules", "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# Logger
logger = get_logger("mock_audio_signals")

def generar_ruido(tipo, num_samples):
    if tipo == "blanco":
        return np.random.normal(0, 1, num_samples)
    elif tipo == "rosa":
        if lfilter is None:
            raise ImportError("scipy es requerido para generar ruido rosa.")
        # Filtro de ruido rosa simple (Voss-McCartney)
        b = [0.02109238, 0.07113478, 0.68873558]
        a = [1, -1.73472577, 0.7660066]
        white = np.random.normal(0, 1, num_samples)
        return lfilter(b, a, white)
    elif tipo == "marron":
        white = np.random.normal(0, 1, num_samples)
        return np.cumsum(white)
    else:
        raise ValueError(f"Tipo de ruido no soportado: {tipo}")

def guardar_wav(data, fs, bits, n_canales, path):
    # Normaliza y convierte a formato PCM
    max_val = np.max(np.abs(data))
    if max_val > 0:
        data = data / max_val
    if bits == 16:
        data_pcm = (data * 32767).astype(np.int16)
        sampwidth = 2
    elif bits == 24:
        data_pcm = (data * 2**23).astype(np.int32)
        sampwidth = 3
    elif bits == 32:
        data_pcm = (data * 2**31).astype(np.int32)
        sampwidth = 4
    else:
        raise ValueError("Solo se soportan 16, 24 o 32 bits.")
    if n_canales == 2:
        data_pcm = np.column_stack([data_pcm, data_pcm])
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(n_canales)
        wf.setsampwidth(sampwidth)
        wf.setframerate(fs)
        if bits == 24:
            # Guardar 24 bits como 3 bytes por muestra
            for frame in data_pcm:
                if n_canales == 1:
                    wf.writeframesraw(frame.astype(np.int32).tobytes()[:3])
                else:
                    wf.writeframesraw(b''.join([ch.astype(np.int32).tobytes()[:3] for ch in frame]))
        else:
            wf.writeframes(data_pcm.tobytes())

if __name__ == "__main__":
    logger.info(f"Generando ruido {TIPO_RUIDO} - fs={FS}Hz, bits={BITS}, duración={DURACION}s, canales={N_CANALES}")
    print(f"Generando ruido {TIPO_RUIDO} - fs={FS}Hz, bits={BITS}, duración={DURACION}s, canales={N_CANALES}")
    num_samples = FS * DURACION
    ruido = generar_ruido(TIPO_RUIDO, num_samples)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_mock.wav"
    path = os.path.join(RECORDINGS_DIR, filename)
    guardar_wav(ruido, FS, BITS, N_CANALES, path)
    logger.info(f"Archivo generado: {path}")