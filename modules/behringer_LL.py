"""Módulo de bajo nivel para controlar la interfaz de audio Behringer UMC204HD con PyAudio."""

import os
import glob
import wave
import threading
import time
import queue
from datetime import datetime

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyaudio
from modules.log_utils import get_logger



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
        #self.log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "behringer_LL.log")
        self.output_path = None

        self.logger = get_logger("behringer_LL")

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

        # Ruta relativa al proyecto: /modules/recordings/yyyymmdd
        date_str = datetime.now().strftime("%Y%m%d")
        recordings_dir = os.path.join(os.path.dirname(__file__), "recordings", date_str)
        os.makedirs(recordings_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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

    def full_test(self) -> tuple[bool, dict]:
        """
        Realiza un test completo del dispositivo Behringer.
        Devuelve (resultado_global, detalles_dict)
        """
        detalles = {}
        resultado_global = True

        # 0. Verificar inicialización previa
        if self.audio_interface is None or self.device_index is None:
            msg = "[full_test] El dispositivo NO está inicializado. Abortando tests."
            self.logger.error(msg)
            detalles["inicializado"] = False
            self.logger.info(f"[full_test] inicializado: False")
            return False, detalles
        detalles["inicializado"] = True
        self.logger.info(f"[full_test] inicializado: True")

        # 1. Verificación de dispositivo de audio (sin crear nueva instancia)
        self.logger.info("[full_test] Verificando dispositivo de audio...")
        try:
            p = self.audio_interface
            num_devices = p.get_device_count()
            dispositivos = []
            for i in range(num_devices):
                try:
                    device_info = p.get_device_info_by_index(i)
                    dispositivos.append(device_info)
                except Exception:
                    continue
            detalles["dispositivos_detectados"] = [d.get("name", "?") for d in dispositivos]
            behringer_ok = any(("Behringer" in d.get("name", "") or "USB" in d.get("name", "")) and int(d.get("maxInputChannels", 0)) > 0 for d in dispositivos)
            detalles["behringer_detectado"] = behringer_ok
            self.logger.info(f"[full_test] behringer_detectado: {behringer_ok}")
            if not behringer_ok:
                self.logger.error("[full_test] No se detectó dispositivo Behringer USB.")
                resultado_global = False
        except Exception as e:
            self.logger.exception("[full_test] Error al buscar dispositivos: %s", e)
            detalles["behringer_detectado"] = False
            self.logger.info(f"[full_test] behringer_detectado: False")
            resultado_global = False

        # 2. Prueba de grabación corta
        self.logger.info("[full_test] Prueba de grabación corta...")
        test_record_ok = False
        test_file = None
        # Ruta relativa al proyecto: /modules/recordings/yyyymmdd
        date_str = datetime.now().strftime("%Y%m%d")
        recordings_dir = os.path.join(os.path.dirname(__file__), "recordings", date_str)
        try:
            test_duration = 2
            os.makedirs(recordings_dir, exist_ok=True)
            test_file = os.path.join(recordings_dir, f"test_recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav")
            self.output_path = test_file
            self.is_recording_event.set()
            self.start_time = time.time()
            self.duration = test_duration
            self.frames_queue.queue.clear()
            if self.open():
                wf = wave.open(test_file, "wb")
                wf.setnchannels(2)
                if self.audio_interface is not None:
                    wf.setsampwidth(self.audio_interface.get_sample_size(pyaudio.paInt24))
                else:
                    wf.setsampwidth(3)  # fallback
                wf.setframerate(192000)
                start = time.time()
                while time.time() - start < test_duration:
                    try:
                        frame = self.frames_queue.get(timeout=0.5)
                        wf.writeframes(frame)
                    except queue.Empty:
                        continue
                wf.close()
                self.close()
                self.is_recording_event.clear()
                test_record_ok = os.path.exists(test_file) and os.path.getsize(test_file) > 0
                detalles["grabacion_corta"] = test_record_ok
                detalles["archivo_test"] = test_file
                self.logger.info(f"[full_test] grabacion_corta: {test_record_ok}")
                if not test_record_ok:
                    self.logger.error(f"[full_test] Grabación de test fallida o archivo vacío: {test_file}")
                    resultado_global = False
            else:
                self.logger.error("[full_test] No se pudo abrir el stream para grabación de test.")
                detalles["grabacion_corta"] = False
                self.logger.info(f"[full_test] grabacion_corta: False")
                resultado_global = False
        except Exception as e:
            self.logger.exception("[full_test] Error durante la grabación de test: %s", e)
            detalles["grabacion_corta"] = False
            self.logger.info(f"[full_test] grabacion_corta: False")
            resultado_global = False
        finally:
            self.is_recording_event.clear()
            self.close()
            if test_file and os.path.exists(test_file):
                try:
                    os.remove(test_file)
                except Exception:
                    pass

        # 3. Verificación de permisos de acceso a hardware y archivos
        self.logger.info("[full_test] Verificando permisos de acceso a hardware y archivos...")
        try:
            self.logger.info("[full_test] Verificando permisos de acceso a dispositivos de audio...")
            pcm_devices = glob.glob("/dev/snd/pcmC*")
            acceso_hw = any(os.access(dev, os.R_OK | os.W_OK) for dev in pcm_devices)

            detalles["permiso_hw"] = acceso_hw
            self.logger.info(f"[full_test] permiso_hw: {acceso_hw}")
            if not acceso_hw:
                self.logger.error("[full_test] No hay permisos de lectura/escritura a ningún dispositivo en /dev/snd/pcmC*.")
                resultado_global = False
        except Exception as e:
            self.logger.exception("[full_test] Error verificando permisos de hardware: %s", e)
            detalles["permiso_hw"] = False
            self.logger.info(f"[full_test] permiso_hw: False")
            resultado_global = False
        try:
            os.makedirs(recordings_dir, exist_ok=True)
            testfile = os.path.join(recordings_dir, "test_perm.txt")
            with open(testfile, "w") as f:
                f.write("test")
            os.remove(testfile)
            detalles["permiso_fs"] = True
            self.logger.info(f"[full_test] permiso_fs: True")
        except Exception as e:
            self.logger.error("[full_test] No hay permisos de escritura en recordings/: %s", e)
            detalles["permiso_fs"] = False
            self.logger.info(f"[full_test] permiso_fs: False")
            resultado_global = False

        # 4. Chequeo de dependencias
        # self.logger.info("[full_test] Chequeando dependencias...")
        # try:
        #     import pyaudio
        #     detalles["pyaudio"] = True
        #     self.logger.info(f"[full_test] pyaudio: True")
        # except ImportError:
        #     self.logger.error("[full_test] PyAudio no está instalado.")
        #     detalles["pyaudio"] = False
        #     self.logger.info(f"[full_test] pyaudio: False")
        #     resultado_global = False

        # 5. Chequeo de espacio en disco
        self.logger.info("[full_test] Chequeando espacio en disco...")
        try:
            statvfs = os.statvfs(recordings_dir)
            espacio_libre = statvfs.f_frsize * statvfs.f_bavail
            detalles["espacio_libre_bytes"] = espacio_libre
            self.logger.info(f"[full_test] espacio_libre_bytes: {espacio_libre}")
            if espacio_libre < 10 * 1024 * 1024:  # 10 MB
                self.logger.error("[full_test] Espacio en disco insuficiente (<10MB).")
                resultado_global = False
        except Exception as e:
            self.logger.exception("[full_test] Error verificando espacio en disco: %s", e)
            detalles["espacio_libre_bytes"] = 0
            self.logger.info(f"[full_test] espacio_libre_bytes: 0")
            resultado_global = False
        
        # Resultado final        
        self.logger.info(f"[full_test] Resultado global: {resultado_global}")
        return resultado_global, detalles

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.append(str(Path(__file__).resolve().parent.parent))

    # Inicializa el dispositivo y ejecuta un test completo
    b = BehringerLowLevel()
    print("Inicializando dispositivo Behringer...")
    if b.init():
        print("Dispositivo inicializado. Ejecutando test completo...")
        resultado, detalles = b.full_test()
        if resultado:
            print("Resultado del test: OK")
        else:
            print("Resultado del test: ERROR")
            print("Detalles de fallos:")
            for clave, valor in detalles.items():
                if clave.startswith("error") or valor is False:
                    print(f" - {clave}: {valor}")
        # Cierra recursos de forma prolija y verifica el resultado
        deinit_ok = b.deinit()
        if deinit_ok:
            print("Recursos liberados correctamente.")
        else:
            print("Hubo un error al liberar los recursos.")
    else:
        print("No se pudo inicializar el dispositivo Behringer.")