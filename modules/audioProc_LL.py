"""
Low-level driver for the AudioProc module.

This module wraps the existing DSP pipeline in the common low-level lifecycle:
- init() -> bool
- open() -> bool
- close() -> bool
- test() -> bool
- full_test() -> tuple[bool, dict]
- deinit() -> bool

The DSP logic is intentionally preserved:
- lpf_butterworth()
- BL_calculator()
- rel_band_power_calculator()
- generate_output()
- process()
"""

from __future__ import annotations

import json
import os
import sys
import wave
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from scipy.interpolate import interp1d
from scipy.signal import butter, lfilter, welch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.support.log_utils import get_logger


class AudioProcError(Exception):
    """Base exception for AudioProc low-level errors."""


class ConfigurationError(AudioProcError):
    """Raised when required configuration files or keys are missing."""


class ProcessingError(AudioProcError):
    """Raised when the processing pipeline fails."""


class AudioProcLowLevel:
    """
    Low-level driver for the AudioProc processing pipeline.
    """

    DEFAULT_LOGGER_NAME = "audioProc_LL"

    def __init__(
        self,
        logger_name: str = DEFAULT_LOGGER_NAME,
        test_wav_path: Optional[str] = None,
        output_dir: Optional[str] = None,
        config_path: Optional[str] = None,
        write_csv_output: bool = False,
    ) -> None:
        self.logger = get_logger(logger_name)

        # standard lifecycle state
        self.is_initialized: bool = False
        self.is_open: bool = False
        self.last_error: Optional[str] = None

        # standard transport state; logical-only for AudioProc
        self.bus = None
        self.bus_num = None
        self.address = None
        self.bus_candidates: list[Any] = []
        self.bus_forced: bool = False

        # paths / processing state
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.modules_dir = os.path.dirname(os.path.abspath(__file__))
        self.support_dir = os.path.join(self.base_dir, "support")
        self.config_path: str = config_path or os.path.join(self.base_dir, "config.json")
        self.output_dir: str = output_dir or os.path.join(self.base_dir, "data")
        self.test_wav_path: Optional[str] = test_wav_path
        self.output_path: Optional[str] = None
        self.write_csv_output: bool = bool(write_csv_output)

        self.config: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

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

    def _log_full_test_result(self, success: bool, report: dict) -> None:
        self.logger.info(
            "Full diagnostic test completed: success=%s pipeline_available=%s processing=%s",
            success,
            report.get("device_present"),
            report.get("details", {}).get("processing"),
        )

    def _load_config(self) -> Dict[str, Any]:
        if not os.path.exists(self.config_path):
            raise ConfigurationError(f"config.json not found: {self.config_path}")

        with open(self.config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        required_keys = [
            "nperseg",
            "noverlap",
            "window",
            "daq",
            "preamp",
            "hid_ch1",
            "hid_ch2",
            "noise_ref",
            "fs[Hz]",
        ]
        missing = [key for key in required_keys if key not in config]
        if missing:
            raise ConfigurationError(f"Missing required config keys: {missing}")

        self.config = config
        return config

    def _required_support_files(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        cfg = config or self.config or self._load_config()

        files = {
            "third_octave_bands": os.path.join(self.support_dir, "third_octave_bands.csv"),
            "daq": os.path.join(self.support_dir, f"{cfg['daq']}.csv"),
            "preamp": os.path.join(self.support_dir, f"{cfg['preamp']}.csv"),
            "hid_ch1": os.path.join(self.support_dir, f"{cfg['hid_ch1']}.csv"),
            "hid_ch2": os.path.join(self.support_dir, f"{cfg['hid_ch2']}.csv"),
        }

        noise_ref_name = cfg.get("noise_ref")
        if os.path.isabs(str(noise_ref_name)):
            files["noise_ref"] = str(noise_ref_name)
        else:
            files["noise_ref"] = os.path.join(self.support_dir, str(noise_ref_name))

        return files

    def _check_environment(self) -> tuple[bool, list[str], dict]:
        errors: list[str] = []
        details: dict[str, Any] = {
            "config_path": self.config_path,
            "support_dir": self.support_dir,
            "output_dir": self.output_dir,
            "files": {},
        }

        try:
            config = self._load_config()
            details["config"] = {
                "fs_hz": config.get("fs[Hz]"),
                "nperseg": config.get("nperseg"),
                "noverlap": config.get("noverlap"),
                "window": config.get("window"),
                "noise_ref": config.get("noise_ref"),
            }
        except Exception as exc:
            errors.append(str(exc))
            return False, errors, details

        for name, path in self._required_support_files(config).items():
            exists = os.path.exists(path)
            details["files"][name] = {"path": path, "exists": exists}
            if not exists:
                errors.append(f"Required support file missing: {name} -> {path}")

        try:
            os.makedirs(self.output_dir, exist_ok=True)
            testfile = os.path.join(self.output_dir, "audioProc_test_perm.tmp")
            with open(testfile, "w", encoding="utf-8") as f:
                f.write("test")
            os.remove(testfile)
            details["output_write_ok"] = True
        except Exception as exc:
            details["output_write_ok"] = False
            errors.append(f"Output directory is not writable: {exc}")

        return len(errors) == 0, errors, details

    def _wav_details(self, wav_path: str) -> dict:
        with wave.open(wav_path, "rb") as wf:
            return {
                "path": wav_path,
                "exists": True,
                "channels": wf.getnchannels(),
                "sample_width_bytes": wf.getsampwidth(),
                "sample_rate_hz": wf.getframerate(),
                "frames": wf.getnframes(),
                "duration_s": wf.getnframes() / float(wf.getframerate()) if wf.getframerate() else None,
            }

    def _external_reference_comparison(self, wav_path: str) -> dict:
        """
        Optional regression check.
        If a matching CSV exists in data/test_data, compare BL_calculator() output
        against it with a 1 dB tolerance.
        """
        result = {
            "enabled": False,
            "reference_csv": None,
            "compared": False,
            "ok": None,
            "max_diff_db": None,
            "error": None,
        }

        try:
            test_recordings_dir = os.path.join(self.modules_dir, "recordings", "test_recordings")
            test_data_dir = os.path.join(self.base_dir, "data", "test_data")
            os.makedirs(test_recordings_dir, exist_ok=True)
            os.makedirs(test_data_dir, exist_ok=True)

            abs_wav = os.path.abspath(wav_path)
            abs_tr = os.path.abspath(test_recordings_dir)
            try:
                is_in_test_recordings = os.path.commonpath([abs_wav, abs_tr]) == abs_tr
            except Exception:
                is_in_test_recordings = False

            if not is_in_test_recordings:
                return result

            ref_csv = os.path.join(
                test_data_dir,
                os.path.splitext(os.path.basename(wav_path))[0] + ".csv",
            )
            result["reference_csv"] = ref_csv

            if not os.path.exists(ref_csv):
                return result

            result["enabled"] = True
            measured = self.BL_calculator(wav_path)

            try:
                external = np.loadtxt(ref_csv, delimiter=",")
            except Exception as exc:
                result["error"] = f"Failed to load reference CSV: {exc}"
                result["ok"] = False
                return result

            if measured.ndim == 2 and external.ndim == 1:
                if external.shape[0] == measured.shape[0]:
                    external = np.tile(external.reshape(-1, 1), (1, measured.shape[1]))
                else:
                    result["error"] = "Reference CSV shape does not match measured bands"
                    result["ok"] = False
                    return result
            elif measured.ndim == 1 and external.ndim == 2:
                if external.shape[1] == 1:
                    external = external.flatten()
                else:
                    result["error"] = "Reference CSV shape does not match measured bands"
                    result["ok"] = False
                    return result

            if measured.shape != external.shape:
                result["error"] = f"Measured shape {measured.shape} does not match reference shape {external.shape}"
                result["ok"] = False
                return result

            measured_arr = np.array(measured)
            external_arr = np.array(external)
            finite_mask = np.isfinite(measured_arr) & np.isfinite(external_arr)

            if not np.any(finite_mask):
                result["error"] = "No comparable finite band values"
                result["ok"] = False
                return result

            diffs = np.abs(measured_arr - external_arr)
            finite_diffs = diffs[finite_mask]
            max_diff = float(np.max(finite_diffs)) if finite_diffs.size else None

            result["compared"] = True
            result["max_diff_db"] = max_diff
            result["ok"] = bool(max_diff is not None and max_diff < 1.0)
            if not result["ok"]:
                result["error"] = f"External comparison failed: max diff {max_diff:.3f} dB"

            return result

        except Exception as exc:
            result["error"] = str(exc)
            result["ok"] = False
            return result

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def init(
        self,
        test_wav_path: Optional[str] = None,
        output_dir: Optional[str] = None,
        config_path: Optional[str] = None,
        write_csv_output: Optional[bool] = None,
    ) -> bool:
        """
        Prepare configuration and internal state only.
        Does not execute the DSP pipeline.
        """
        self.logger.info("Initializing module")
        self._clear_error()

        try:
            self.close()

            if test_wav_path is not None:
                self.test_wav_path = str(test_wav_path)
            if output_dir is not None:
                self.output_dir = str(output_dir)
            if config_path is not None:
                self.config_path = str(config_path)
            if write_csv_output is not None:
                self.write_csv_output = bool(write_csv_output)

            os.makedirs(self.output_dir, exist_ok=True)

            self.bus = None
            self.bus_num = None
            self.address = None
            self.bus_candidates = []
            self.bus_forced = False
            self.output_path = None
            self.is_open = False
            self.is_initialized = True

            self.logger.info(
                "Module initialized: config_path=%s output_dir=%s test_wav_path=%s write_csv_output=%s",
                self.config_path,
                self.output_dir,
                self.test_wav_path,
                self.write_csv_output,
            )
            return True

        except Exception as exc:
            self.is_initialized = False
            self._set_error(f"Initialization failed: {exc}")
            self.logger.exception("Initialization failed: %s", exc)
            return False

    def open(self) -> bool:
        """
        Open the logical processing environment.
        This validates config and support files but does not process audio.
        """
        self.logger.info("Opening processing environment")
        self._clear_error()

        if not self.is_initialized:
            self._set_error("Module is not initialized")
            self.logger.error(self.last_error)
            return False

        if self.is_open:
            self.logger.info("Processing environment already open")
            return True

        ok, errors, details = self._check_environment()
        if not ok:
            self._set_error("; ".join(errors))
            self.logger.error("Processing environment validation failed: %s", self.last_error)
            return False

        self.bus = details
        self.is_open = True
        self.logger.info("Processing environment opened")
        return True

    def close(self) -> bool:
        """
        Close the logical processing environment.
        Idempotent.
        """
        self.logger.info("Closing processing environment")
        self._clear_error()

        try:
            self.bus = None
            self.is_open = False
            return True
        except Exception as exc:
            self._set_error(f"Close failed: {exc}")
            self.logger.exception("Close failed: %s", exc)
            return False

    def deinit(self) -> bool:
        """
        Total cleanup. Leaves the module in a neutral state.
        """
        self.logger.info("Deinitializing module")
        self._clear_error()

        try:
            ok = self.close()
            self.is_initialized = False
            self.output_path = None
            self.config = {}
            self.bus_candidates = []
            self.bus_forced = False
            return bool(ok)
        except Exception as exc:
            self._set_error(f"Deinitialization failed: {exc}")
            self.logger.exception("Deinitialization failed: %s", exc)
            return False

    def probe(self) -> bool:
        """
        Smoke-level check for the processing environment.
        """
        self.logger.info("Probing AudioProc processing environment")
        self._clear_error()

        try:
            ok, errors, _details = self._check_environment()
            if not ok:
                self._set_error("; ".join(errors))
            self.logger.info("Probe result: %s", ok)
            return bool(ok)
        except Exception as exc:
            self._set_error(f"Probe failed: {exc}")
            self.logger.warning("Probe failed: %s", exc)
            return False

    def test(self) -> bool:
        """
        Fast smoke test.
        May open temporarily and restores original state.
        """
        self.logger.info("Running smoke test")
        self._clear_error()

        was_open = self.is_open
        temporarily_opened = False

        try:
            if not was_open:
                if not self.open():
                    return False
                temporarily_opened = True

            result = self.probe()
            self.logger.info("Smoke test completed: success=%s", result)
            return bool(result)

        except Exception as exc:
            self._set_error(f"Test failed: {exc}")
            self.logger.warning("Test failed: %s", exc)
            return False

        finally:
            if temporarily_opened:
                self.close()

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
                        # rectangular area using df_min as width is 
                        # considered.
                        power_in_band = band_psd * df_min
                    else:
                        power_in_band = np.trapz(band_psd, band_freqs)
                    
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
            power_in_band = np.trapz(psd_vals, freqs_band)
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
            
            # Step 3: Generate binary output file
            self.logger.info("Step 3: Generating binary output file...")
            output_path = self.generate_output(rel_powers, wav_path)
            
            self.logger.info(f"Processing completed successfully. Generated file: {output_path}")
            return output_path
            
        except Exception as e:
            self.logger.exception(f"Error during processing of file {wav_path}: {e}")
            return None


    def full_test(self) -> tuple[bool, dict]:
        """
        Full diagnostic test.
        Executes the full processing pipeline on test_wav_path.
        Never propagates uncaught exceptions.
        """
        self.logger.info("Running full diagnostic test")
        self._clear_error()

        report = self._build_full_test_report()
        was_open = self.is_open
        temporarily_opened = False
        original_write_csv = self.write_csv_output

        try:
            report["initialized"] = self.is_initialized
            if not self.is_initialized:
                msg = "Module is not initialized"
                report["errors"].append(msg)
                self._set_error(msg)
                self._log_full_test_result(False, report)
                return False, report

            if not was_open:
                if self.open():
                    temporarily_opened = True
                    report["opened"] = True
                else:
                    report["opened"] = False
                    if self.last_error:
                        report["errors"].append(self.last_error)
                    self._log_full_test_result(False, report)
                    return False, report
            else:
                report["opened"] = True

            env_ok, env_errors, env_details = self._check_environment()
            if not env_ok:
                report["errors"].extend(env_errors)

            test_input: Dict[str, Any] = {
                "path": self.test_wav_path,
                "exists": bool(self.test_wav_path and os.path.exists(self.test_wav_path)),
            }

            if not self.test_wav_path:
                report["errors"].append("test_wav_path is not defined")
            elif not os.path.exists(self.test_wav_path):
                report["errors"].append(f"Test WAV file not found: {self.test_wav_path}")
            else:
                try:
                    test_input.update(self._wav_details(self.test_wav_path))
                except Exception as exc:
                    report["errors"].append(f"Test WAV inspection failed: {exc}")

            comparison = {}
            processing = {
                "ran": False,
                "ok": False,
                "output_path": None,
                "output_exists": False,
                "csv_output_enabled": True,
            }

            if env_ok and test_input.get("exists"):
                comparison = self._external_reference_comparison(self.test_wav_path)
                if comparison.get("enabled") and comparison.get("ok") is False:
                    report["errors"].append(comparison.get("error") or "External reference comparison failed")

                try:
                    self.write_csv_output = True
                    processing["ran"] = True
                    output_path = self.process(self.test_wav_path)
                    processing["output_path"] = output_path
                    processing["output_exists"] = bool(output_path and os.path.exists(output_path))
                    processing["ok"] = bool(output_path and os.path.exists(output_path))

                    if not processing["ok"]:
                        report["errors"].append("Processing did not generate a binary output file")

                except Exception as exc:
                    processing["ok"] = False
                    report["errors"].append(f"Processing failed: {exc}")

            report["details"] = {
                "paths": {
                    "base_dir": self.base_dir,
                    "modules_dir": self.modules_dir,
                    "support_dir": self.support_dir,
                    "config_path": self.config_path,
                    "output_dir": self.output_dir,
                },
                "environment": env_details,
                "test_input": test_input,
                "reference_comparison": comparison,
                "processing": processing,
            }

            success = bool(env_ok and test_input.get("exists") and processing.get("ok"))
            report["device_present"] = success

            self._log_full_test_result(success, report)
            return success, report

        except Exception as exc:
            report["errors"].append(f"Unexpected full_test failure: {exc}")
            self._set_error(f"Full test failed: {exc}")
            self.logger.exception("Full test failed: %s", exc)
            self._log_full_test_result(False, report)
            return False, report

        finally:
            self.write_csv_output = original_write_csv
            if temporarily_opened:
                self.close()


def main(argv=None) -> bool:
    """Run AudioProc self-test and return True on success."""
    import json as _json

    default_test_wav = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "recordings",
        "test_recordings",
        "20180824_8105_20m_daspre_cap_2.wav",
    )

    ll = AudioProcLowLevel()
    ll.logger.info("Script start: AudioProcLowLevel init/full_test")

    init_kwargs = {}
    if os.path.exists(default_test_wav):
        init_kwargs["test_wav_path"] = default_test_wav

    if not ll.init(**init_kwargs):
        report = {
            "initialized": False,
            "opened": False,
            "device_present": False,
            "errors": [ll.last_error] if ll.last_error else [],
            "details": {},
        }
        ll.logger.error("Initialization report=%s", _json.dumps(report, default=str))
        print(_json.dumps(report, indent=2, default=str))
        return False

    ok, report = ll.full_test()
    print(_json.dumps(report, indent=2, default=str))
    ll.deinit()
    return bool(ok)


if __name__ == "__main__":
    ok = main(sys.argv[1:])
    raise SystemExit(0 if ok else 1)