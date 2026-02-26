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

# Configurable parameters
NOISE_TYPE = "pink"   # Options: "white", "pink", "brown"
FS = 192000           # Sampling frequency (Hz)
BITS = 24             # Bit depth (bits)
DURATION = 70         # File duration (seconds)
N_CHANNELS = 1       # Mono=1, Stereo=2

# Output folder
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDINGS_DIR = os.path.join(BASE_DIR, "modules", "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)

# Logger
logger = get_logger("mock_audio_signals")

def generate_noise(noise_type, num_samples):
    if noise_type == "white":
        return np.random.normal(0, 1, num_samples)
    elif noise_type == "pink":
        if lfilter is None:
            raise ImportError("scipy is required to generate pink noise.")
        # Simple pink noise filter (Voss-McCartney)
        b = [0.02109238, 0.07113478, 0.68873558]
        a = [1, -1.73472577, 0.7660066]
        white = np.random.normal(0, 1, num_samples)
        return lfilter(b, a, white)
    elif noise_type == "brown":
        white = np.random.normal(0, 1, num_samples)
        return np.cumsum(white)
    else:
        raise ValueError(f"Unsupported noise type: {noise_type}")

def save_wav(data, fs, bits, n_channels, path):
    """
    Normalize and save the audio data as a WAV file.

    Args:
        data (np.ndarray): Audio data.
        fs (int): Sampling frequency.
        bits (int): Bit depth.
        n_channels (int): Number of channels.
        path (str): Output file path.
    """
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
        raise ValueError("Only 16, 24, or 32 bits are supported.")
    if n_channels == 2:
        data_pcm = np.column_stack([data_pcm, data_pcm])
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(fs)
        if bits == 24:
            # Save 24 bits as 3 bytes per sample
            for frame in data_pcm:
                if n_channels == 1:
                    wf.writeframesraw(frame.astype(np.int32).tobytes()[:3])
                else:
                    wf.writeframesraw(b''.join([ch.astype(np.int32).tobytes()[:3] for ch in frame]))
        else:
            wf.writeframes(data_pcm.tobytes())

if __name__ == "__main__":
    logger.info(f"Generating {NOISE_TYPE} noise - fs={FS}Hz, bits={BITS}, duration={DURATION}s, channels={N_CHANNELS}")
    print(f"Generating {NOISE_TYPE} noise - fs={FS}Hz, bits={BITS}, duration={DURATION}s, channels={N_CHANNELS}")
    num_samples = FS * DURATION
    noise = generate_noise(NOISE_TYPE, num_samples)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_mock.wav"
    path = os.path.join(RECORDINGS_DIR, filename)
    save_wav(noise, FS, BITS, N_CHANNELS, path)
    logger.info(f"File generated: {path}")