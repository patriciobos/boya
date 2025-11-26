"""
Low-level driver template for the AudioProc module.

This class provides a standard interface for initializing, testing, acquiring data, and resource management for the AudioProc module.
"""

import logging
import os
import json
import numpy as np
import wave
import pandas as pd
from scipy.signal import butter, lfilter, welch
from scipy import signal
from scipy.interpolate import interp1d
from scipy.stats import chi2

# Define the path to the test WAV file
TEST_WAV_PATH = 'C:/repo/boya/modules/recordings/20180824_8105_20m_daspre_cap_2.wav'

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
        self.test_wav_path = None
        self.write_csv_output = False

    def _create_logger(self):
        logger = logging.getLogger("AudioProcLowLevel")
        logger.setLevel(logging.INFO)

        # Stream (console) handler
        stream_handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s [AudioProcLowLevel] %(levelname)s: %(message)s")
        stream_handler.setFormatter(formatter)

        # File handler: append logs into logs/AudioProcLowLevel.log at repo root
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        log_dir = os.path.join(base_dir, "logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            # If directory creation fails, keep going and let FileHandler raise if necessary
            pass
        log_path = os.path.join(log_dir, "AudioProcLowLevel.log")
        file_handler = logging.FileHandler(log_path, mode='a', encoding='utf-8')
        file_handler.setFormatter(formatter)

        # Add handlers only once to avoid duplicate logs on re-instantiation
        if not logger.handlers:
            logger.addHandler(stream_handler)
            logger.addHandler(file_handler)

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
        self.logger.info("Calculating relative power with respect to minimum reference...")
        
        # Load minimum reference powers from TXT
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        reference_path = os.path.join(base_dir, "support", "reference_minimum_BL.txt")
        
        # Read reference values (one per line, in dB)
        reference_minimum = np.loadtxt(reference_path)
        
        # Verify that dimensions match
        n_bands = powers.shape[0] if powers.ndim > 1 else len(powers)
        if n_bands != len(reference_minimum):
            raise ValueError(f"Number of bands in powers ({n_bands}) does not match "
                           f"the number of reference values ({len(reference_minimum)})")
        
        # Calculate difference: powers - reference_minimum (both in dB)
        # numpy broadcasting automatically handles mono and stereo cases
        
        if (len(powers.shape)==2) and (powers.shape[1] == 2):
            powers[:,0] = powers[:,0] - reference_minimum            
            powers[:,1] = powers[:,1] - reference_minimum
        else:
            powers = powers - reference_minimum
        
        self.logger.info("Relative power calculation completed.")
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
    
   
    def full_test(self):
        """
        Run a full self-test of the module.
        Executes the process method with a hardcoded WAV file to verify
        that the entire processing pipeline works correctly.
        
        Returns:
            tuple: (test_passed: bool, details: str) indicating if the test passed and details.
        """
        self.logger.info("Running full test...")
        
        # Verify that test_wav_path is defined
        if self.test_wav_path is None:
            error_msg = "test_wav_path is not defined. Must set it before running full_test."
            self.logger.error(error_msg)
            return False, error_msg
        
        wav_path = self.test_wav_path
        
        # Verify that the file exists
        if not os.path.exists(wav_path):
            error_msg = f"Test WAV file not found: {wav_path}"
            self.logger.error(error_msg)
            return False, error_msg
        
        try:
            # Enable CSV writing for the test
            self.write_csv_output = True
            
            # Execute the process method
            self.logger.info(f"Executing process with file: {wav_path}")
            output_path = self.process(wav_path)
            
            if output_path is None:
                error_msg = "The process method returned None (error during processing)"
                self.logger.error(error_msg)
                return False, error_msg
            
            # Verify that the binary file was generated
            if not os.path.exists(output_path):
                error_msg = f"Binary output file was not generated: {output_path}"
                self.logger.error(error_msg)
                return False, error_msg
            
            success_msg = f"Test completed successfully. Generated file: {output_path}"
            self.logger.info(success_msg)
            self.write_csv_output = False
            return True, success_msg
            
        except Exception as e:
            error_msg = f"Error during full_test: {e}"
            self.logger.exception(error_msg)
            return False, error_msg

    

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
