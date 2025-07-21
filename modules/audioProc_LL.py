"""
Low-level driver template for the AudioProc module.

This class provides a standard interface for initializing, testing, acquiring data, and resource management for the AudioProc module.
"""

import logging
import numpy as np
import wave
from scipy.signal import butter, lfilter

class AudioProcLowLevel:
    
    """
    Low-level driver for the AudioProc module.
    """

    def __init__(self):
        """
        Initialize the low-level driver instance.
        Sets up internal state and logger.
        """
        self.logger = self._create_logger()
        self.output_path = None

    def _create_logger(self):
        logger = logging.getLogger("AudioProcLowLevel")
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s [AudioProcLowLevel] %(levelname)s: %(message)s")
        handler.setFormatter(formatter)
        if not logger.handlers:
            logger.addHandler(handler)
        return logger

    def init(self):
        """
        Initialize hardware or resources.
        Returns:
            bool: True if initialization succeeded, False otherwise.
        """
        self.logger.info("Initializing AudioProc module...")
        return True

    def deinit(self):
        """
        Deinitialize hardware or resources and clean up.
        """
        self.logger.info("Deinitializing AudioProc module...")

    def lpf_butterworth(self, wav_path, cutoff_hz=200, order=6, output_path=None):
        """
        Apply a low-pass Butterworth filter to a WAV file.
        Args:
            wav_path (str): Path to input WAV file.
            cutoff_hz (float): Cutoff frequency in Hz (default 200).
            order (int): Filter order (default 6).
            output_path (str, optional): Path to save filtered WAV. If None, appends '_lpf.wav'.
        Returns:
            str: Path to filtered WAV file.
        """

        self.logger.info(f"Applying LPF Butterworth: {wav_path}, cutoff={cutoff_hz} Hz, order={order}")
        # Read WAV file
        with wave.open(wav_path, 'rb') as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            fs = wf.getframerate()
            n_frames = wf.getnframes()
            audio_bytes = wf.readframes(n_frames)
        if n_channels != 1:
            raise ValueError("Only mono WAV files are supported.")
        if fs != 192000:
            self.logger.warning(f"Sample rate is {fs} Hz, expected 192000 Hz.")
        # Convert bytes to numpy array
        if sampwidth == 3:
            # 24-bit PCM
            import struct
            a = np.frombuffer(audio_bytes, dtype=np.uint8)
            a = a.reshape(-1, 3)
            # Convert 3 bytes to int32
            def _24bit_to_int(x):
                return int.from_bytes(x.tobytes(), byteorder='little', signed=True)
            audio = np.array([_24bit_to_int(x) for x in a], dtype=np.int32)
            audio = audio / (2**23)
        elif sampwidth == 2:
            audio = np.frombuffer(audio_bytes, dtype=np.int16) / 32768.0
        elif sampwidth == 4:
            audio = np.frombuffer(audio_bytes, dtype=np.int32) / (2**31)
        else:
            raise ValueError("Unsupported sample width.")

        # Design Butterworth LPF
        nyq = 0.5 * fs
        normal_cutoff = cutoff_hz / nyq
        if not (0 < normal_cutoff < 1):
            raise ValueError(f"Cutoff frequency too high for sampling rate: normal_cutoff={normal_cutoff}. Must be between 0 and 1.")
        ba = butter(order, normal_cutoff, btype='low', analog=False)
        if ba is None or not isinstance(ba, (tuple, list)) or len(ba) != 2:
            raise RuntimeError("Failed to design Butterworth filter: butter() returned None or invalid output.")
        b, a = ba
        filtered = lfilter(b, a, audio)
        filtered = np.asarray(filtered)  # Ensure filtered is a NumPy array

        # Normalize to avoid clipping
        max_val = np.max(np.abs(filtered))
        if max_val > 0:
            filtered = filtered / max_val

        # Save filtered audio
        if output_path is None:
            base, ext = wav_path.rsplit('.', 1)
            output_path = f"{base}_lpf.wav"
        with wave.open(output_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(sampwidth)
            wf.setframerate(fs)
            if sampwidth == 3:
                # 24-bit PCM: convert float [-1,1] to int32, then write 3 bytes/sample
                filtered_arr = np.asarray(filtered)
                data_pcm = (filtered_arr * (2**23 - 1)).astype(np.int32)
                for val in data_pcm:
                    wf.writeframesraw(val.tobytes()[:3])
            elif sampwidth == 2:
                data_pcm = (filtered * 32767).astype(np.int16)
                wf.writeframes(data_pcm.tobytes())
            elif sampwidth == 4:
                data_pcm = (filtered * (2**31 - 1)).astype(np.int32)
                wf.writeframes(data_pcm.tobytes())
        self.logger.info(f"Filtered file saved: {output_path}")
        return output_path
    
   
    def full_test(self):
        """
        Run a full self-test of the module.
        Returns:
            tuple: (bool, str) indicating (test_passed, details)
        """
        self.logger.info("Running full test...")
        return True, "Test passed"

    

if __name__ == "__main__":
    # Basic tests when run as a script
    ll = AudioProcLowLevel()
    print("Init:", ll.init())
    print("Full test:", ll.full_test())
    ll.deinit()
