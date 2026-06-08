# boya

AHT10 por I²C usando CH341 en Linux (driver + test)
Requisitos de hardware

Adaptador CH341 en modo I²C (típicamente 1a86:5512)

Sensor AHT10 (dirección I²C típica: 0x38)

Cableado:

GND ↔ GND

VCC (3.3V recomendado) ↔ VCC

SCL ↔ SCL

SDA ↔ SDA

Pull-ups en SDA/SCL (muchos módulos ya los traen; si no, agregar 4.7k–10k a VCC)

1) Dependencias del sistema
sudo apt-get update
sudo apt-get install -y git build-essential linux-headers-$(uname -r) i2c-tools python3-pip
python3 -m pip install --user smbus2

2) Compilar el driver CH341 (I²C master)

Este driver crea un bus /dev/i2c-* para el CH341:

cd ~
git clone https://github.com/frank-zago/ch341-i2c-spi-gpio.git
cd ch341-i2c-spi-gpio
make


Archivos esperados (al menos):

ch341-core.ko

i2c-ch341.ko

3) Cargar el driver (manual, para probar)
cd ~/ch341-i2c-spi-gpio
sudo modprobe i2c-dev
sudo insmod ./ch341-core.ko
sudo insmod ./i2c-ch341.ko


Verificar que aparece el bus CH341:

i2cdetect -l | grep -i ch341


Nota: el número de bus (i2c-1, i2c-12, etc.) puede variar entre reinicios/enchufes. Siempre detectarlo con i2cdetect -l.

4) Hacerlo persistente tras reinicio
4.1 Instalar los módulos en /lib/modules
cd ~/ch341-i2c-spi-gpio
KVER="$(uname -r)"
sudo mkdir -p "/lib/modules/$KVER/extra/ch341"
sudo install -m 0644 ch341-core.ko i2c-ch341.ko "/lib/modules/$KVER/extra/ch341/"
sudo depmod -a

4.2 Autocargar módulos al boot
sudo tee /etc/modules-load.d/ch341.conf >/dev/null <<'EOF'
i2c-dev
ch341-core
i2c-ch341
EOF


Reiniciar y verificar:

i2cdetect -l | grep -i ch341


Secure Boot: si está habilitado, módulos no firmados pueden no cargar. Verificar con mokutil --sb-state. En nuestro caso funciona con Secure Boot disabled.

5) Permisos para acceder a /dev/i2c-* sin sudo

Agregar el usuario al grupo i2c:

getent group i2c || sudo groupadd i2c
sudo usermod -aG i2c "$USER"
newgrp i2c


Verificar:

ls -l /dev/i2c-*
groups

6) Smoke test desde terminal (AHT10)

Obtener el bus del CH341:

BUS=$(i2cdetect -l | awk '/CH341/ {gsub("i2c-","",$1); print $1; exit}')
echo "BUS=$BUS"

---

## Ejecutar tests

1) Activar el entorno virtual desde la raíz del repositorio:

```bash
source .venv/bin/activate
```

2) Ejecutar los tests de FSMs, router y LL funcionales:

```bash
PYTHONPATH=. pytest modules/test/test_fsm_mocks.py -q
PYTHONPATH=. pytest modules/test/test_router.py -q
PYTHONPATH=. pytest modules/test/test_ll_functional.py -q
```

3) Ejecutar el test del scheduler central (retry y alive):

```bash
PYTHONPATH=. pytest modules/test/test_central_scheduler.py -q
```

4) Ejecutar todos los tests juntos:

```bash
PYTHONPATH=. pytest -q
```

5) Alternativa con helper script:

```bash
./run_tests.sh
```

## Scheduler separado

El archivo `scheduler.json` define los intervalos de ejecución de los módulos y se usa en lugar de `config.json` para la parte de scheduler.

Ejemplo:

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

Con esta configuración:
- Sensores cada 10 minutos.
- Behringer cada 4 horas.
- Iridium envía un "alive" cada hora.
- AudioProc no tiene scheduler propio; solo procesa audio desde Behringer.


Escanear y confirmar que aparece 0x38:

sudo i2cdetect -y "$BUS"


Trigger + lectura de 6 bytes (prueba funcional):

sudo i2ctransfer -y "$BUS" w3@0x38 0xAC 0x33 0x00
sleep 0.08
sudo i2ctransfer -y "$BUS" r6@0x38

7) Smoke test en Python (equivalente a i2ctransfer)
python3 - <<'PY'
from smbus2 import SMBus, i2c_msg
import time, subprocess, re

# detectar bus CH341
out = subprocess.check_output(["bash","-lc","i2cdetect -l | grep -i CH341 | head -n1"]).decode()
m = re.match(r"i2c-(\d+)", out.strip())
bus = int(m.group(1))
addr = 0x38

with SMBus(bus) as b:
    b.i2c_rdwr(i2c_msg.write(addr, [0xAC, 0x33, 0x00]))  # trigger
    time.sleep(0.08)
    r = i2c_msg.read(addr, 6)
    b.i2c_rdwr(r)
    data = list(r)
print("bus:", bus, "addr:", hex(addr), "data:", [hex(x) for x in data])
PY