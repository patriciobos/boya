"""
Low-level driver template for the AudioProc module.

This class provides a standard interface for initializing, testing, acquiring data, and resource management for the AudioProc module.
"""

import os
import sys
import json
import math
import re
import numpy as np
import wave
import pandas as pd
from scipy.signal import butter, lfilter, welch
from scipy import signal
from scipy.interpolate import interp1d
from scipy.stats import chi2
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.support.log_utils import get_logger
from modules.support.system_config import now_utc_minus_3


def _trapz(y, x):
    y_arr = np.asarray(y, dtype=float)
    x_arr = np.asarray(x, dtype=float)
    if y_arr.shape != x_arr.shape:
        raise ValueError("y and x must have the same shape")
    if y_arr.ndim != 1:
        y_arr = y_arr.ravel()
        x_arr = x_arr.ravel()
    if x_arr.size < 2:
        return 0.0
    dx = np.diff(x_arr)
    return float(np.sum((y_arr[:-1] + y_arr[1:]) * dx * 0.5))

# Define the path to the test WAV file (project-relative, cross-platform)
# Keep the same filename, but construct it relative to repository root so it works
# on Windows and Linux. If the file isn't present, TEST_WAV_PATH will be None.
BASE_DIR = Path(__file__).resolve().parents[1]
_candidate = BASE_DIR / 'test' / 'recordings' / 'test_recordings' / '20180824_8105_20m_daspre_cap_2.wav'

TEST_WAV_PATH = str(_candidate) if _candidate.exists() else None

class AudioProcLowLevel:
    
    """
    Low-level driver for the AudioProc module.
    """

    def __init__(self):
        """
        Initialize the low-level driver instance.
        Sets up internal state and logger.
        """
        self.logger = get_logger("audioProc_LL")
        self.output_path = None
        self.test_wav_path = None
        self.write_csv_output = False

        self.is_initialized: bool = False
        self.is_open: bool = False
        self.last_error: Optional[str] = None
        self.bus: Optional[Any] = None
        self.bus_num: Optional[int] = None
        self.address: Optional[int] = None
        self.bus_candidates: list[int] = []
        self.bus_forced: bool = False

    def _set_error(self, msg: str) -> None:
        self.last_error = msg

    def _clear_error(self) -> None:
        self.last_error = None

    def _build_full_test_report(self) -> dict:
        return {
            "initialized": self.is_initialized,
            "opened": self.is_open,
            "device_present": False,
            "errors": [],
            "details": {},
        }

    def init(self) -> bool:
        """
        Prepare internal configuration only. Does not perform audio processing.
        """
        self.logger.info("Initializing AudioProc module...")
        self._clear_error()

        try:
            self.close()
            self.output_path = None
            self.test_wav_path = self.test_wav_path or TEST_WAV_PATH
            self.write_csv_output = False
            self.is_initialized = True
            self.is_open = False
            self.last_error = None
            self.address = None
            self.bus_num = None
            self.bus_candidates = []
            self.bus_forced = False
            self.logger.info("AudioProc module initialized: test_wav_path=%s", self.test_wav_path)
            return True
        except Exception as exc:
            self.is_initialized = False
            self._set_error(f"Initialization failed: {exc}")
            self.logger.exception("Initialization failed: %s", exc)
            return False

    def open(self) -> bool:
        """Open the AudioProc module for processing."""
        self.logger.info("Opening AudioProc module")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        if self.is_open:
            self.logger.info("AudioProc module already open")
            return True

        self.is_open = True
        self.logger.info("AudioProc module opened")
        return True

    def close(self) -> bool:
        """Close the AudioProc module."""
        self.logger.info("Closing AudioProc module")
        self._clear_error()

        self.is_open = False
        return True

    def probe(self) -> bool:
        """Perform a lightweight presence and environment check."""
        self.logger.info("Probing AudioProc module")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        if self.test_wav_path is None:
            self._set_error("No test WAV path configured")
            self.logger.error(self.last_error)
            return False

        if not os.path.exists(self.test_wav_path):
            self._set_error(f"Test WAV file not found: {self.test_wav_path}")
            self.logger.error(self.last_error)
            return False

        return True

    def test(self) -> bool:
        """Run a quick smoke test without executing the full processing pipeline."""
        self.logger.info("Running smoke test")
        self._clear_error()
        was_open = self.is_open
        temporarily_opened = False

        try:
            if not was_open:
                if not self.open():
                    return False
                temporarily_opened = True

            return self.probe()
        except Exception as exc:
            self._set_error(f"Test failed: {exc}")
            self.logger.exception("Test failed: %s", exc)
            return False
        finally:
            if temporarily_opened:
                self.close()

    def deinit(self) -> bool:
        """
        Deinitialize hardware or resources and clean up.
        """
        self.logger.info("Deinitializing AudioProc module...")
        self._clear_error()

        try:
            self.close()
            self.output_path = None
            self.test_wav_path = None
            self.write_csv_output = False
            self.is_initialized = False
            self.is_open = False
            self.last_error = None
            self.bus = None
            self.bus_num = None
            self.address = None
            self.bus_candidates = []
            self.bus_forced = False
            return True
        except Exception as exc:
            self._set_error(f"Deinitialization failed: {exc}")
            self.logger.exception("Deinitialization failed: %s", exc)
            return False

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
    
    
    def BL_calculator(self, wav_path):
        """
        Calculates the power contained in each third-octave band from 1 Hz to 100000 Hz.
        Loads bands from third_octave_bands.csv and uses Welch estimator for PSD.
        
        Args:
            wav_path (str): Path to input WAV file.
            
        Returns:
            numpy.ndarray: Array of powers in third-octave bands expressed in dB.
                          The conversion uses: power_dB = 10 * log10(power)
                          If mono: 1D array of powers in dB.
                          If stereo: 2D array of shape (N_bands, 2) with powers in dB.
        """
        self.logger.info(f"Calculating third-octave bands: {wav_path}")
        
        # Read WAV file
        with wave.open(wav_path, 'rb') as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            fs = wf.getframerate()
            n_frames = wf.getnframes()
            audio_bytes = wf.readframes(n_frames)
        
        # Convert bytes to numpy array (taken from lpf_butterworth)
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
        
        # Reshape to handle multiple channels
        if n_channels == 2:
            audio = audio.reshape(-1, 2)
        elif n_channels == 1:
            audio = audio.reshape(-1, 1)
        else:
            raise ValueError(f"Unsupported number of channels: {n_channels}")
        
        # Load configuration from JSON (config.json moved to repository root)
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(base_dir, "config.json")
        with open(config_path, 'r') as f:
            config = json.load(f)
        nperseg = config['nperseg']
        noverlap = config['noverlap']
        min_length_factor = config.get('min_length_factor', 4)
        
        # Get signal length L (number of points per channel)
        if n_channels == 1:
            L = len(audio.flatten())
        else:
            L = len(audio[:, 0])  # Length of first channel (all channels have same length)
        
        # Adjust nperseg if L < min_length_factor * nperseg
        if L < min_length_factor * nperseg:
            target_nperseg = L / min_length_factor
            # Find the largest power of 2 less than or equal to target_nperseg
            if target_nperseg >= 1:
                nperseg = int(2 ** np.floor(np.log2(target_nperseg)))
            else:
                # If target is less than 1, use minimum value of 1
                nperseg = 1
            noverlap = nperseg // 2
            self.logger.info(f"Adjusted nperseg to {nperseg} and noverlap to {noverlap} "
                           f"(L={L}, min_length_factor={min_length_factor})")
        
        # Load frequency responses of the 3 stages
        support_dir = os.path.join(base_dir, "support")
        
        # Load DAQ response
        daq_file = os.path.join(support_dir, f"{config['daq']}.csv")
        daq_df = pd.read_csv(daq_file)
        daq_resp_func = interp1d(daq_df['freq'], daq_df['resp'], 
                                kind='linear', bounds_error=False, fill_value='extrapolate')
        
        # Load preamplifier response
        preamp_file = os.path.join(support_dir, f"{config['preamp']}.csv")
        preamp_df = pd.read_csv(preamp_file)
        preamp_resp_func = interp1d(preamp_df['freq'], preamp_df['resp'],
                                    kind='linear', bounds_error=False, fill_value='extrapolate')
        
        # Load hydrophone responses (one per channel)
        hid_ch1_file = os.path.join(support_dir, f"{config['hid_ch1']}.csv")
        hid_ch1_df = pd.read_csv(hid_ch1_file)
        hid_ch1_resp_func = interp1d(hid_ch1_df['freq'], hid_ch1_df['resp'],
                                     kind='linear', bounds_error=False, fill_value='extrapolate')
        
        if n_channels == 2:
            hid_ch2_file = os.path.join(support_dir, f"{config['hid_ch2']}.csv")
            hid_ch2_df = pd.read_csv(hid_ch2_file)
            hid_ch2_resp_func = interp1d(hid_ch2_df['freq'], hid_ch2_df['resp'],
                                         kind='linear', bounds_error=False, fill_value='extrapolate')
        
        # Load third-octave bands from CSV
        csv_path = os.path.join(base_dir, "support", "third_octave_bands.csv")
        bands_df = pd.read_csv(csv_path)
        
        # Filter bands in the range from 1 Hz to 100000 Hz
        f_min = 1.0
        f_max = 100000.0
        bands_df = bands_df[(bands_df['fl'] >= f_min) & (bands_df['fh'] <= f_max)].copy()
        
        # Filter bands that are within Nyquist range
        nyquist = fs / 2
        bands_df = bands_df[bands_df['fh'] <= nyquist * 0.99]
        
        # Extract band limits
        f_lower = bands_df['fl'].values
        f_upper = bands_df['fh'].values
        
        # Initialize power array
        n_bands_valid = len(f_lower)
        if n_channels == 1:
            powers = np.zeros(n_bands_valid)
        else:  # n_channels == 2
            powers = np.zeros((n_bands_valid, 2))
        
        # Calculate PSD using Welch with parameters from config.json
        for ch in range(n_channels):
            if n_channels == 1:
                signal_data = audio.flatten()
            else:
                signal_data = audio[:, ch]
            
            
            freqs, psd = welch(signal_data, fs=fs, nperseg=nperseg, noverlap=noverlap, 
                             window=config['window'], scaling='density')
            
            # Apply frequency response correction
            # Interpolate responses to PSD frequencies
            daq_resp = daq_resp_func(freqs)
            preamp_resp = preamp_resp_func(freqs)
            
            # Select hydrophone response according to channel
            if n_channels == 1 or ch == 0:
                hid_resp = hid_ch1_resp_func(freqs)
            else:  # n_channels == 2 and ch == 1
                hid_resp = hid_ch2_resp_func(freqs)
            
            # Square each response (magnitude -> power)
            daq_resp_power = daq_resp ** 2
            preamp_resp_power = preamp_resp ** 2
            hid_resp_power = hid_resp ** 2
            
            # Multiply the 3 power responses to get total response
            total_resp_power = daq_resp_power * preamp_resp_power * hid_resp_power
            
            # Divide PSD by total response (correction)
            # Avoid division by zero
            total_resp_power = np.maximum(total_resp_power, 1e-20)
            psd_corrected = psd / total_resp_power
            
            # Calculate minimum frequency resolution
            df_min = fs / nperseg
            
            # Integrate corrected PSD in each third-octave band
            for i in range(n_bands_valid):
                # Create mask for current band
                band_mask = (freqs >= f_lower[i]) & (freqs <= f_upper[i])
                
                if np.any(band_mask):
                    # Integrate corrected PSD in band using trapezoidal rule
                    band_freqs = freqs[band_mask]
                    band_psd = psd_corrected[band_mask]
                    if len(band_psd) == 1:
                        # In case there is only one PSD point inside band,
                        # rectangular area using df_min as width is considered.
                        power_in_band = float(band_psd[0]) * df_min
                    else:
                        power_in_band = _trapz(band_psd, band_freqs)
                    
                    if n_channels == 1:
                        powers[i] = power_in_band
                    else:
                        powers[i, ch] = power_in_band
                else:
                    # If there are no frequencies in the band, power is zero
                    if n_channels == 1:
                        powers[i] = np.nan                    
                    else:
                        powers[i, ch] = np.nan
        
        # Convert powers to dB: power_dB = 10 * log10(power)
        # Avoid log(0) or negative values using a small epsilon
        # Preserve NaN values
        nan_mask = np.isnan(powers)
        powers_safe = np.where(nan_mask, 1e-20, np.maximum(powers, 1e-20))
        powers_dB = 10 * np.log10(powers_safe)
        # Restore NaN values
        powers_dB[nan_mask] = np.nan
        
        self.logger.info(f"Calculation completed. {n_bands_valid} bands processed using Welch estimator for PSD.")
        return powers_dB
    
    def rel_band_power_calculator(self, powers):
        """
        Calculates the difference in dB between measured powers and minimum reference powers.
        
        Args:
            powers (numpy.ndarray): Array of powers in dB (as returned by BL_calculator).
                                   If mono: 1D array of powers in dB.
                                   If stereo: 2D array of shape (N_bands, 2) with powers in dB.
            
        Returns:
            numpy.ndarray: Array of differences in dB (powers - reference_minimum).
                          If mono: 1D array of differences in dB.
                          If stereo: 2D array of shape (N_bands, 2) with differences in dB.
        """
        self.logger.info("Calculating relative power with respect to minimum reference (noise_ref from config)...")

        # Determine base and support directories
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        support_dir = os.path.join(base_dir, "support")

        # Load config to get noise_ref filename and sampling frequency
        config_path = os.path.join(base_dir, "config.json")
        try:
            with open(config_path, 'r') as cf:
                config = json.load(cf)
        except Exception as e:
            raise RuntimeError(f"Failed to load config.json at '{config_path}': {e}")

        noise_ref_name = config.get('noise_ref')
        if not noise_ref_name:
            raise ValueError("'noise_ref' not specified in config.json")

        fs = config.get('fs[Hz]')
        if fs is None:
            raise ValueError("Sampling rate 'fs[Hz]' not found in config.json")

        # Locate noise_ref file (prefer absolute path if given, else in support dir)
        if os.path.isabs(noise_ref_name):
            noise_ref_path = noise_ref_name
        else:
            noise_ref_path = os.path.join(support_dir, noise_ref_name)

        if not os.path.exists(noise_ref_path):
            raise FileNotFoundError(f"noise_ref file not found: {noise_ref_path}")

        # Load PSD reference file. Expect columns: freq, psd (power spectral density)
        try:
            ref_df = pd.read_csv(noise_ref_path)
        except Exception as e:
            raise RuntimeError(f"Failed to read noise_ref CSV '{noise_ref_path}': {e}")

        # Try to find columns
        if {'freq', 'psd'}.issubset(ref_df.columns):
            ref_freq = ref_df['freq'].values
            ref_psd = ref_df['psd'].values
        else:
            # Fallback: use first two columns
            if ref_df.shape[1] < 2:
                raise ValueError(f"noise_ref file '{noise_ref_path}' must have at least two columns (freq, psd)")
            ref_freq = ref_df.iloc[:, 0].values
            ref_psd = ref_df.iloc[:, 1].values

        # Create interpolator for PSD (power per Hz)
        try:
            psd_interp = interp1d(ref_freq, ref_psd, kind='linear', bounds_error=False, fill_value='extrapolate')
        except Exception as e:
            raise RuntimeError(f"Failed to create PSD interpolator: {e}")

        # Load third-octave bands
        bands_path = os.path.join(support_dir, "third_octave_bands.csv")
        bands_df = pd.read_csv(bands_path)

        # Filter bands in the same way BL_calculator does
        f_min = 1.0
        f_max = 100000.0
        bands_df = bands_df[(bands_df['fl'] >= f_min) & (bands_df['fh'] <= f_max)].copy()
        nyquist = float(fs) / 2.0
        bands_df = bands_df[bands_df['fh'] <= nyquist * 0.99]

        f_lower = bands_df['fl'].values
        f_upper = bands_df['fh'].values

        n_bands = len(f_lower)

        # Integrate PSD in each band to obtain power per band
        ref_powers = np.zeros(n_bands)
        for i in range(n_bands):
            fl = f_lower[i]
            fh = f_upper[i]
            if fh <= fl:
                ref_powers[i] = np.nan
                continue
            # Sample frequencies within band for integration
            num_samples = 256
            freqs_band = np.linspace(fl, fh, num_samples)
            psd_vals = psd_interp(freqs_band)
            # Ensure non-negative PSD
            psd_vals = np.maximum(psd_vals, 0.0)
            power_in_band = _trapz(psd_vals, freqs_band)
            ref_powers[i] = power_in_band

        # Convert reference powers to dB (10*log10)
        # Avoid log of zero
        ref_powers_safe = np.where(np.isfinite(ref_powers), np.maximum(ref_powers, 1e-20), ref_powers)
        reference_minimum_dB = 10.0 * np.log10(ref_powers_safe)
        # Preserve NaNs
        reference_minimum_dB[~np.isfinite(ref_powers)] = np.nan

        # Now compare shapes and compute rel powers: powers (dB) - reference_minimum_dB
        # Determine number of bands in 'powers'
        n_bands_meas = powers.shape[0] if powers.ndim > 1 else len(powers)
        if n_bands_meas != len(reference_minimum_dB):
            raise ValueError(f"Number of bands in measured powers ({n_bands_meas}) does not match number of reference bands ({len(reference_minimum_dB)})")

        # Subtract reference from measured powers (both in dB)
        if (powers.ndim == 2) and (powers.shape[1] == 2):
            # Broadcast reference to both channels
            powers[:, 0] = powers[:, 0] - reference_minimum_dB
            powers[:, 1] = powers[:, 1] - reference_minimum_dB
        else:
            powers = powers - reference_minimum_dB

        self.logger.info("Relative power calculation completed using noise_ref PSD.")
        return powers
    
    def _json_safe_matrix(self, values):
        arr = np.asarray(values, dtype=float)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        matrix = []
        for row in arr:
            matrix.append([
                None if not math.isfinite(float(value)) else round(float(value), 3)
                for value in row
            ])
        return matrix

    def _output_timestamp_from_wav(self, wav_path):
        wav_basename = os.path.splitext(os.path.basename(wav_path))[0]
        match = re.search(r"(\d{8})_(\d{6})", wav_basename)
        if not match:
            now = now_utc_minus_3()
            return now.strftime("%Y%m%d_%H%M%S"), now.replace(microsecond=0).isoformat()

        date_part, time_part = match.groups()
        dt_local = datetime.strptime(f"{date_part}_{time_part}", "%Y%m%d_%H%M%S").replace(tzinfo=now_utc_minus_3().tzinfo)
        return f"{date_part}_{time_part}", dt_local.replace(microsecond=0).isoformat()

    def generate_json_output(self, rel_powers, wav_path):
        self.logger.info(f"Generating JSON output file for: {wav_path}")

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output_dir = os.path.join(base_dir, "data", "audio_proc")
        os.makedirs(output_dir, exist_ok=True)
        timestamp_token, timestamp = self._output_timestamp_from_wav(wav_path)
        output_path = os.path.join(output_dir, f"audioProc_{timestamp_token}.json")

        payload = {
            "timestamp": timestamp,
            "relative_band_power_db": self._json_safe_matrix(rel_powers),
        }
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
            handle.write("\n")
        self.logger.info(f"JSON file generated: {output_path}")
        return output_path

    def generate_output(self, rel_powers, wav_path):
        """
        Writes rel_powers data to a binary file.
        If write_csv_output is True, also generates a CSV file with relative powers
        (without rounding or limiting to uint8).
        
        Args:
            rel_powers (numpy.ndarray): Array of relative powers in dB.
                                       If mono: 1D array of N_bands elements.
                                       If stereo: 2D array of shape (N_bands, 2).
            wav_path (str): Path to original WAV file (used to generate output filename).
            
        Returns:
            str: Path to generated binary file.
        """
        self.logger.info(f"Generating binary output file for: {wav_path}")
        
        # Get base name of WAV file (without extension)
        wav_basename = os.path.splitext(os.path.basename(wav_path))[0]
        
        # Determine output directory ("data" folder at repository root)
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output_dir = os.path.join(base_dir, "data")
        os.makedirs(output_dir, exist_ok=True)
        
        # Generate output filename
        output_path = os.path.join(output_dir, f"{wav_basename}.dat")
        
        # Convert rel_powers to 1D array if it's 2D
        if rel_powers.ndim == 2:
            # Stereo: flatten to 1D (2*N_bands bytes)
            rel_powers_flat = rel_powers.flatten()
        else:
            # Mono: already 1D (N_bands bytes)
            rel_powers_flat = rel_powers
        
        # Convert to uint8: round, clip to range 0-255
        # Replace NaN values with 255 before conversion
        rel_powers_flat = np.where(np.isnan(rel_powers_flat), 255.0, rel_powers_flat)
        # First round to integer
        rel_powers_int = np.round(rel_powers_flat).astype(np.int32)
        # Clip to range 0-255
        rel_powers_uint8 = np.clip(rel_powers_int, 0, 255).astype(np.uint8)
        
        # Write binary file
        with open(output_path, 'wb') as f:
            f.write(rel_powers_uint8.tobytes())
        
        self.logger.info(f"Binary file generated: {output_path} ({len(rel_powers_uint8)} bytes)")
        
        # If write_csv_output is enabled, also generate CSV file
        if self.write_csv_output:
            csv_path = os.path.join(output_dir, f"{wav_basename}.csv")
            
            # Create DataFrame with relative powers (without rounding or limiting)
            if rel_powers.ndim == 1:
                # Mono: one column
                df = pd.DataFrame({'rel_power_dB': rel_powers})
            else:
                # Stereo: two columns (one per channel)
                df = pd.DataFrame({
                    'rel_power_dB_ch1': rel_powers[:, 0],
                    'rel_power_dB_ch2': rel_powers[:, 1]
                })
            
            # Write CSV
            df.to_csv(csv_path, index=False)
            self.logger.info(f"CSV file generated: {csv_path}")
        
        return output_path
    
    def process(self, wav_path):
        """
        Main method that integrates the complete audio processing:
        1. Calculates powers in third-octave bands (BL_calculator)
        2. Calculates relative powers with respect to reference (rel_band_power_calculator)
        3. Generates binary output file (generate_output)
        
        Args:
            wav_path (str): Path to WAV file to process.
            
        Returns:
            str: Path to generated binary file in "data" folder.
                 None if there is an error during processing.
        """
        self.logger.info(f"Starting complete processing of file: {wav_path}")
        
        try:
            # Step 1: Calculate powers in third-octave bands
            self.logger.info("Step 1: Calculating powers in third-octave bands...")
            powers = self.BL_calculator(wav_path)
            
            # Step 2: Calculate relative powers with respect to reference
            self.logger.info("Step 2: Calculating relative powers with respect to reference...")
            rel_powers = self.rel_band_power_calculator(powers)
            
            # Step 3: Generate JSON output file
            self.logger.info("Step 3: Generating JSON output file...")
            output_path = self.generate_json_output(rel_powers, wav_path)
            
            self.logger.info(f"Processing completed successfully. Generated file: {output_path}")
            return output_path
            
        except Exception as e:
            self.logger.exception(f"Error during processing of file {wav_path}: {e}")
            return None
    
   
    def full_test(self) -> tuple[bool, dict]:
        """
        Run a full self-test of the module.
        Executes the process method with a hardcoded WAV file to verify
        that the entire processing pipeline works correctly.
        """
        self.logger.info("Running full test...")
        self._clear_error()
        report = self._build_full_test_report()
        report["details"]["test_wav_path"] = self.test_wav_path

        if self.test_wav_path is None:
            error_msg = "test_wav_path is not defined. Must set it before running full_test."
            self.logger.error(error_msg)
            report["errors"].append(error_msg)
            self._set_error(error_msg)
            return False, report

        wav_path = self.test_wav_path

        if not os.path.exists(wav_path):
            error_msg = f"Test WAV file not found: {wav_path}"
            self.logger.error(error_msg)
            report["errors"].append(error_msg)
            self._set_error(error_msg)
            return False, report

        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            test_recordings_dir = os.path.join(base_dir, "test", "recordings", "test_recordings")
            test_data_dir = os.path.join(base_dir, "data", "test_data")
            os.makedirs(test_recordings_dir, exist_ok=True)
            os.makedirs(test_data_dir, exist_ok=True)

            abs_wav = os.path.abspath(wav_path)
            abs_tr = os.path.abspath(test_recordings_dir)
            is_in_test_recordings = False
            try:
                is_in_test_recordings = os.path.commonpath([abs_wav, abs_tr]) == abs_tr
            except Exception:
                is_in_test_recordings = False

            if is_in_test_recordings:
                ref_csv = os.path.join(test_data_dir, os.path.splitext(os.path.basename(wav_path))[0] + ".csv")
                if os.path.exists(ref_csv):
                    self.logger.info(f"Found external reference CSV for comparison: {ref_csv}")
                    measured = self.BL_calculator(wav_path)

                    try:
                        external = np.loadtxt(ref_csv, delimiter=',')
                    except Exception as e:
                        msg = f"Failed to load reference CSV '{ref_csv}': {e}"
                        self.logger.error(msg)
                        report["errors"].append(msg)
                        self._set_error(msg)
                        return False, report

                    if measured.ndim == 2 and external.ndim == 1:
                        if external.shape[0] == measured.shape[0]:
                            external = np.tile(external.reshape(-1, 1), (1, measured.shape[1]))
                        else:
                            msg = "Reference CSV shape does not match measured bands."
                            self.logger.error(msg)
                            report["errors"].append(msg)
                            self._set_error(msg)
                            return False, report
                    elif measured.ndim == 1 and external.ndim == 2:
                        if external.shape[1] == 1:
                            external = external.flatten()
                        else:
                            msg = "Reference CSV shape does not match measured bands."
                            self.logger.error(msg)
                            report["errors"].append(msg)
                            self._set_error(msg)
                            return False, report

                    if measured.shape != external.shape:
                        msg = f"Measured bands shape {measured.shape} does not match reference shape {external.shape}."
                        self.logger.error(msg)
                        report["errors"].append(msg)
                        self._set_error(msg)
                        return False, report

                    measured_arr = np.array(measured)
                    external_arr = np.array(external)
                    finite_mask = np.isfinite(measured_arr) & np.isfinite(external_arr)
                    if not np.any(finite_mask):
                        msg = "No comparable band values (all NaN or non-finite) between measured and reference."
                        self.logger.error(msg)
                        report["errors"].append(msg)
                        self._set_error(msg)
                        return False, report

                    diffs = np.abs(measured_arr - external_arr)
                    diffs_masked = np.where(finite_mask, diffs, 0.0)

                    failing = np.argwhere(diffs_masked >= 1.0)
                    if failing.size == 0:
                        msg = "External comparison: all bands within 1 dB."
                        self.logger.info(msg)
                        report["details"]["comparison"] = msg
                    else:
                        details = []
                        for idx in failing:
                            if measured_arr.ndim == 1:
                                i = int(idx[0])
                                details.append(
                                    f"band {i}: measured={measured_arr[i]:.2f} dB, ref={external_arr[i]:.2f} dB, diff={diffs[i]:.2f} dB"
                                )
                            else:
                                i, ch = int(idx[0]), int(idx[1])
                                details.append(
                                    f"band {i} ch{ch+1}: measured={measured_arr[i,ch]:.2f} dB, ref={external_arr[i,ch]:.2f} dB, diff={diffs[i,ch]:.2f} dB"
                                )
                        err_msg = "External comparison failed for bands: " + "; ".join(details)
                        self.logger.error(err_msg)
                        report["errors"].append(err_msg)
                        self._set_error(err_msg)
                        return False, report

        except Exception as e:
            self.logger.exception(f"Error during external comparison setup: {e}")
            report["details"]["comparison_setup_error"] = str(e)

        try:
            self.write_csv_output = True
            self.logger.info(f"Executing process with file: {wav_path}")
            output_path = self.process(wav_path)

            if output_path is None:
                error_msg = "The process method returned None (error during processing)"
                self.logger.error(error_msg)
                report["errors"].append(error_msg)
                self._set_error(error_msg)
                return False, report

            if not os.path.exists(output_path):
                error_msg = f"Binary output file was not generated: {output_path}"
                self.logger.error(error_msg)
                report["errors"].append(error_msg)
                self._set_error(error_msg)
                return False, report

            success_msg = f"Test completed successfully. Generated file: {output_path}"
            self.logger.info(success_msg)
            report["device_present"] = True
            report["details"]["output_path"] = output_path
            report["details"]["message"] = success_msg
            self.write_csv_output = False
            return True, report

        except Exception as e:
            error_msg = f"Error during full_test: {e}"
            self.logger.exception(error_msg)
            report["errors"].append(error_msg)
            self._set_error(error_msg)
            return False, report

    

if __name__ == "__main__":
    # Basic tests when run as a standalone script
    ll = AudioProcLowLevel()
    print("Initializing AudioProcLowLevel...")
    init_result = ll.init()
    print(f"Init: {init_result}")
    
    if init_result:
        # Set the wav_path for the test (adjust as needed)
        # Example: ll.test_wav_path = "path/to/file.wav"
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ll.test_wav_path = TEST_WAV_PATH
        
        print("Running full_test...")
        test_passed, details = ll.full_test()
        if test_passed:
            print(f"Full test: OK - {details}")
        else:
            print(f"Full test: ERROR - {details}")
    else:
        print("Could not initialize. Aborting full_test.")
    
    ll.deinit()
    print("Process completed.")
