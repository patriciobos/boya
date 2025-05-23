"""Módulo de bajo nivel para controlar la interfaz de audio Behringer UMC204HD con PyAudio."""

import os
import wave
import logging
import threading
import time
import queue
from datetime import datetime

import pyaudio

# Suppress warnings and prevent JACK server from starting
os.environ["PYTHONWARNINGS"] = "ignore"
os.environ["JACK_NO_START_SERVER"] = "1"

class BehringerLowLevel:
    """Controlador de bajo nivel para la interfaz de audio USB Behringer."""

    def __init__(self):
        self.audio_interface = None
        self.device_index = None
        self.stream = None
        self.is_recording_event = threading.Event()
        self.recording_thread = None
        self.last_record_ok = False
        self.start_time = None
        self.duration = None
        self.frames_queue = queue.Queue()

        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.log_file = os.path.join(base_dir, "behringer_LL.log")
        self.output_path = None

        self.logger = logging.getLogger("behringer_logger")
        self.logger.setLevel(logging.INFO)

        file_handler = logging.FileHandler(self.log_file)
        console_handler = logging.StreamHandler()

        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def init(self) -> bool:
        if self.audio_interface is not None:
            self.logger.info("Behringer ya está inicializado. Omite init().")
            return True

        self.logger.info("Buscando dispositivo Behringer USB...")
        p = None
        try:
            p = pyaudio.PyAudio()
            num_devices = p.get_device_count()

            for i in range(num_devices):
                try:
                    device_info = p.get_device_info_by_index(i)
                    device_name = device_info.get("name", "")
                    max_input_channels = int(device_info.get("maxInputChannels", 0))

                    if ("Behringer" in str(device_name) or "USB" in str(device_name)) and max_input_channels > 0:
                        self.device_index = i
                        self.audio_interface = p
                        self.logger.info("Dispositivo encontrado: %s (Index: %d)", device_name, i)
                        return True
                except OSError as e:
                    self.logger.warning("Índice inválido %d: %s", i, e)

            self.logger.warning("No se encontró dispositivo Behringer USB.")
            return False
        except Exception as e:
            self.logger.exception("Error durante init(): %s", e)
            return False
        finally:
            if p is not None and self.audio_interface is None:
                p.terminate()

    def open(self) -> bool:
        if self.audio_interface is None or self.device_index is None:
            self.logger.warning("No se puede abrir el stream: dispositivo no inicializado.")
            return False
        try:
            self.stream = self.audio_interface.open(
                format=pyaudio.paInt24,
                channels=2,
                rate=192000,
                input=True,
                frames_per_buffer=8192,
                input_device_index=self.device_index,
                stream_callback=self._callback,
            )
            self.logger.info("Stream de audio abierto (Index %d).", self.device_index)
            return True
        except Exception as e:
            self.logger.exception("Error abriendo el stream: %s", e)
            self.stream = None
            return False

    def _callback(self, in_data, _frame_count, _time_info, status):
        if status:
            self.logger.warning("Estado del stream: %s", status)
        if not self.is_recording_event.is_set():
            return (b"", pyaudio.paComplete)
        self.frames_queue.put(in_data)
        return (in_data, pyaudio.paContinue)

    def record(self, duration: int) -> bool:
        if self.audio_interface is None or self.device_index is None:
            self.logger.warning("No se puede grabar: dispositivo no inicializado.")
            return False

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        recordings_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
        os.makedirs(recordings_dir, exist_ok=True)
        self.output_path = os.path.join(recordings_dir, f"recording_{timestamp}.wav")

        self.logger.info("Iniciando grabación: %s por %d segundos.", self.output_path, duration)

        self.is_recording_event.set()
        self.start_time = time.time()
        self.duration = duration
        self.frames_queue.queue.clear()

        if not self.open():
            return False

        self.recording_thread = threading.Thread(target=self._write_audio)
        self.recording_thread.start()
        return True

    def _write_audio(self):
        if self.audio_interface is None:
            self.logger.error("Interfaz no inicializada. Abortando _write_audio().")
            self.last_record_ok = False
            self.stop_recording()
            return
        try:
            if self.output_path is None:
                self.logger.error("output_path no está definido. Abortando _write_audio().")
                self.last_record_ok = False
                self.stop_recording()
                return

            with wave.open(self.output_path, "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(self.audio_interface.get_sample_size(pyaudio.paInt24))
                wf.setframerate(192000)

                start = time.time()
                if self.duration is None:
                    self.logger.error("Duración de grabación no establecida. Abortando _write_audio().")
                    self.last_record_ok = False
                    self.stop_recording()
                    return
                while time.time() - start < self.duration:
                    try:
                        frame = self.frames_queue.get(timeout=0.5)
                        wf.writeframes(frame)
                    except queue.Empty:
                        continue

            self.logger.info("Grabación guardada en: %s", self.output_path)
            self.last_record_ok = True
        except Exception as e:
            self.logger.exception("Error durante la escritura de audio: %s", e)
            self.last_record_ok = False
        finally:
            self.close()
            self.stop_recording()  # 🔧 aseguramos que el evento y el thread se limpien



    def stop_recording(self):
        self.is_recording_event.clear()

        # No intentar hacer join desde el mismo thread
        if (
            self.recording_thread is not None
            and self.recording_thread.is_alive()
            and threading.current_thread() != self.recording_thread
        ):
            self.recording_thread.join()

        self.logger.info("Grabación finalizada.")
        self.recording_thread = None


    def test(self) -> bool:
        self.logger.info("Ejecutando test de dispositivo...")
        return self.audio_interface is not None

    def deinit(self) -> bool:
        if self.audio_interface is None:
            self.logger.info("No hay recursos que liberar.")
            return True

        try:
            self.audio_interface.terminate()
            self.logger.info("Interfaz de audio Behringer liberada.")
            return True
        except Exception as e:
            self.logger.warning("Error al liberar recursos: %s", e)
            return False
        finally:
            self.audio_interface = None
            self.device_index = None

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
                self.logger.info("Stream de audio cerrado.")
            except Exception as e:
                self.logger.warning("Error cerrando el stream: %s", e)
            finally:
                self.stream = None
        else:
            self.logger.warning("No hay stream activo para cerrar.")


    def is_recording_done(self) -> tuple[bool, bool]:
        """Devuelve (terminó la grabación, grabación exitosa o no)."""
        finished = not self.is_recording_event.is_set() and self.recording_thread is None
        return finished, self.last_record_ok
