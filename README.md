# boya

Sistema Python para adquisición, procesamiento y telemetría de una boya instrumental.

El proyecto organiza cada subsistema en dos capas:

- `*_LL.py`: acceso low-level al hardware o al procesamiento específico.
- `*_fsm.py`: máquina de estados que recibe mensajes, ejecuta acciones y reporta estado.

El proceso principal (`main.py`) lanza los FSMs en procesos separados, conecta sus colas con el `Router` y usa un scheduler central para disparar adquisiciones y transmisiones.

## Modulos

- `AHT10`: temperatura y humedad por I2C.
- `MPU6050`: acelerometro/giroscopio por I2C.
- `AIS`: AIS/GPS por puerto serie.
- `Windsonic`: viento por puerto serie.
- `XTRA2210`: controlador solar por puerto serie/Modbus.
- `Behringer`: adquisicion de audio USB.
- `AudioProc`: procesamiento de audio generado por Behringer.
- `Iridium`: gateway satelital SBD; transmite mensajes binarios `alive` y `audioProc` en horarios regulares.

## Rutas del proyecto

Todas las rutas relativas se resuelven desde la raiz del repositorio.

Archivos principales:

- `config.json`: configuracion general, por ejemplo `data_dir` y `logs_dir`.
- `scheduler.json`: intervalos del scheduler central.
- `data/`: mediciones y salidas generadas.
- `logs/`: logs y reportes de ejecucion.
- `support/`: tablas, calibraciones y datos de referencia.
- `docs/`: manuales de hardware.

## Entorno

Dependencias de sistema recomendadas en Linux:

```bash
sudo apt-get update
sudo apt-get install -y portaudio19-dev python3-dev i2c-tools
```

Desde la raiz del repositorio:

```bash
source .venv/bin/activate
python -m pip install -r support/requirements-dev.txt
```

Para runtime solamente, sin herramientas de test:

```bash
python -m pip install -r support/requirements.txt
```

Para comandos de test se recomienda usar la venv explicitamente. La suite local por defecto no requiere hardware:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -m "not hardware" -q
```

## Ejecucion

Con hardware real:

```bash
PYTHONPATH=. .venv/bin/python main.py
```

Con mocks low-level:

```bash
USE_LL_MOCKS=1 PYTHONPATH=. .venv/bin/python main.py
```

Tambien se puede mockear un modulo individual:

```bash
USE_MOCK_AHT10=1 PYTHONPATH=. .venv/bin/python main.py
```

La forma recomendada para ensayos repetibles es declarar los modulos mockeados en `config.json`:

```json
"mock_modules": ["Windsonic", "Iridium", "AIS", "XTRA2210"]
```

Los mocks configurados siguen el flujo normal de sus FSMs y quedan identificados en logs, readings y `system_status.json` como `hardware mock`. El sistema valida configuraciones ambiguas, por ejemplo mezclar `USE_LL_MOCKS=1` global con una lista parcial en `mock_modules`.

## Scheduler central

El unico scheduler activo es el scheduler central de `main.py`. Los FSMs no tienen schedulers internos.

`scheduler.json` define los intervalos en segundos:

```json
{
  "schedules": {
    "AHT10": 600,
    "AIS": 600,
    "MPU6050": 600,
    "Windsonic": 600,
    "XTRA2210": 600,
    "Behringer": 14400,
    "Iridium": 3600,
    "AudioProc": null
  }
}
```

Con esta configuracion, el scheduler agenda activaciones en horas regulares UTC-3, ancladas a medianoche:

- Sensores cada 10 minutos: `:00`, `:10`, `:20`, etc.
- Behringer cada 4 horas: `00:00`, `04:00`, `08:00`, `12:00`, `16:00`, `20:00`.
- Iridium cada hora exacta, con ciclo de 4 horas: `alive`, `alive`, `alive`, `audio`.
- AudioProc no se agenda directamente: procesa cuando Behringer entrega un archivo.

El scheduler incrementa desde el slot programado, no desde la hora real de ejecucion, para evitar drift si el loop se demora.

## Iridium SBD

Iridium funciona como gateway satelital. La FSM de Iridium decide que transmitir y usa `modules/support/iridium_protocol.py` para codificar payloads binarios; el low-level `iridium_LL.py` queda limitado al transporte AT/SBD.

### Alive binario

El scheduler central envia Iridium cada hora exacta UTC-3. En las primeras tres horas del ciclo envia `alive`; en la cuarta envia `audio`:

```python
Message(MessageID.SIG_TRANSMIT, {"mode": "alive", "origin": "Scheduler"})
Message(MessageID.SIG_TRANSMIT, {"mode": "audio", "origin": "Scheduler"})
```

La FSM arma un payload binario de 16 bytes leyendo:

- `logs/system_status.json`: estado operativo de FSMs y low-levels.
- `data/ais_readings.jsonl`: ultima posicion GPS disponible (`gps_fix`, `lat`, `lon`).

Formato del payload `alive`, big-endian, sin version:

| Campo | Tipo | Bytes | Descripcion |
| --- | --- | ---: | --- |
| `message_type` | `uint8` | 1 | `0x01` para alive |
| `timestamp_utc` | `uint32` | 4 | epoch seconds UTC |
| `fsm_status` | `uint8` | 1 | bitmap de estado FSM |
| `ll_status` | `uint8` | 1 | bitmap de estado low-level |
| `gps_fix` | `uint8` | 1 | `0` sin fix, `1` con fix |
| `lat` | `int32` | 4 | grados `* 1e7`; `0x7FFFFFFF` sin fix |
| `lon` | `int32` | 4 | grados `* 1e7`; `0x7FFFFFFF` sin fix |

En los bitmaps, `0` significa OK y `1` significa error. Orden de bits:

| Bit | Modulo |
| ---: | --- |
| 0 | AHT10 |
| 1 | AIS |
| 2 | AudioProc |
| 3 | Behringer |
| 4 | Iridium |
| 5 | MPU6050 |
| 6 | Windsonic |
| 7 | XTRA2210 |

La deteccion de errores se basa en `system_status.json`: estado `ERROR`, ultimo resultado `error` o detalles con errores.


### AudioProc binario

En la cuarta hora del ciclo, el scheduler envia a Iridium `mode="audio"`. La FSM de Iridium busca el ultimo `output_file` valido en `data/audioProc_readings.jsonl`, carga el JSON `data/audio_proc/audioProc_*.json` y transmite un payload binario con:

- `message_type = 0x02`
- 2 bytes de status (`fsm_status`, `ll_status`)
- cantidad de valores de audio
- un byte por banda de frecuencia y por canal, codificado como dB `int8` (`None` se codifica como `-128`)

El payload de audio se considera valido solo si tiene exactamente tantas filas como bandas de frecuencia esperadas por canal. Para `192000 Hz`, la cantidad esperada actual es 49 bandas por canal.

El Router no dispara transmisiones Iridium al terminar AudioProc; solo registra el ultimo resultado. Las transmisiones satelitales quedan controladas por el scheduler central en horarios regulares.

Para pruebas sin visibilidad satelital, `config.json` puede dejar:

```json
"iridium_transmit_enabled": false
```

Con esa opcion, Iridium arma el payload y registra cada pedido en `logs/iridium_transmit_requests.jsonl`, pero no abre sesion SBD ni intenta transmitir por modem. Para transmision real, cambiar el valor a `true`.


## Tests

Suite normal para desarrollo, sin hardware:

```bash
./scripts/run_tests.sh
```

Equivalente manual:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -m "not hardware" -q
```

Cobertura local, excluyendo drivers de hardware real:

```bash
./scripts/run_coverage.sh
```

Tests de hardware real:

```bash
./scripts/run_hardware_tests.sh
```

Cobertura con hardware real, incluyendo drivers `*_LL.py`:

```bash
./scripts/run_hardware_coverage.sh
```

Equivalente manual:

```bash
RUN_HARDWARE_TESTS=1 PYTHONPATH=. .venv/bin/python -m pytest -m hardware -q -rs
```

Para exigir fix GPS en AIS/GPS:

```bash
REQUIRE_GPS_FIX=1 RUN_HARDWARE_TESTS=1 PYTHONPATH=. .venv/bin/python -m pytest test/test_ais_LL.py -q
```

## Logs y datos generados

Los archivos en `logs/` y las mediciones en `data/*.jsonl` son artefactos de ejecucion. Si se generan datos nuevos durante tests o ejecucion local, no deberian mezclarse con cambios de codigo salvo que se quieran versionar como fixtures.

### Formato de mediciones JSONL

Cada linea de `data/*_readings.jsonl` es un objeto JSON compacto:

```json
{"timestamp":"2026-06-09T21:42:07-03:00","data":{}}
```

Reglas generales:

- `timestamp` usa UTC-3 sin decimales en los segundos: `YYYY-MM-DDTHH:MM:SS-03:00`.
- Los `.log` de texto agregan la etiqueta humana `UTC-3` despues del timestamp.
- `data` contiene campos con unidades explicitas cuando corresponde, por ejemplo `_c`, `_rh`, `_deg`, `_mps`, `_v`, `_a`, `_w`, `_s`.
- Los registros reales no llevan `source`.
- Los registros generados por mocks llevan `source` con valor `hardware mock` o `firmware mock`.
- El nombre del modulo no se repite dentro del registro; queda implicito por el archivo `*_readings.jsonl`.
- `Windsonic` registra resumen fisico de viento: velocidad promedio/min/max en m/s, direccion promedio en grados, cantidad de muestras y muestras validas.
- `MPU6050` registra aceleracion en g y giroscopio en dps.
- `XTRA2210` registra energia en campos planos con unidades: `pv_voltage_v`, `pv_current_a`, `load_current_a`, `battery_voltage_v`, `battery_soc_pct`, entre otros.
- `Behringer` registra evento de adquisicion con `file`, `duration_s`, `sample_rate_hz`, `channels` y `size_bytes`.
- `AudioProc` registra `input_file` y `output_file`; el detalle queda en `data/audio_proc/audioProc_YYYYMMDD_HHMMSS.json`.

## CH341 e I2C para AHT10/MPU6050

Dependencias de sistema utiles en Linux:

```bash
sudo apt-get update
sudo apt-get install -y git build-essential linux-headers-$(uname -r) i2c-tools python3-pip
```

Driver CH341 I2C:

```bash
cd ~
git clone https://github.com/frank-zago/ch341-i2c-spi-gpio.git
cd ch341-i2c-spi-gpio
make
sudo modprobe i2c-dev
sudo insmod ./ch341-core.ko
sudo insmod ./i2c-ch341.ko
```

Verificar bus:

```bash
i2cdetect -l | grep -i ch341
```

Permisos para acceder a `/dev/i2c-*` sin sudo:

```bash
getent group i2c || sudo groupadd i2c
sudo usermod -aG i2c "$USER"
newgrp i2c
```

Smoke test AHT10:

```bash
BUS=$(i2cdetect -l | awk '/CH341/ {gsub("i2c-", "", $1); print $1; exit}')
echo "BUS=$BUS"
sudo i2cdetect -y "$BUS"
sudo i2ctransfer -y "$BUS" w3@0x38 0xAC 0x33 0x00
sleep 0.08
sudo i2ctransfer -y "$BUS" r6@0x38
```
