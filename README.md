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
- `Iridium`: telemetria SBD y mensajes alive.

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

Con esta configuracion:

- Sensores cada 10 minutos.
- Behringer cada 4 horas.
- Iridium envia un alive cada hora.
- AudioProc no se agenda: procesa cuando Behringer entrega un archivo.

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
