#!/bin/bash

# Chequeo no bloqueante del adaptador CH341 USB-I2C.
# No carga módulos del kernel.
# No detiene el arranque del servicio boya.
# Siempre termina con exit 0.

ROOT_DIR="/home/boya/boya"
LOG_DIR="$ROOT_DIR/logs"
STATUS_FILE="$LOG_DIR/ch341_i2c_status.json"

mkdir -p "$LOG_DIR"

TS="$(date --iso-8601=seconds)"

USB_LINE="$(lsusb | grep -Ei '1a86:5512|CH341|QinHeng' | head -n 1)"
I2C_LINE="$(i2cdetect -l 2>/dev/null | grep -i 'CH341' | head -n 1)"
MODULES="$(lsmod | awk '/^(ch341_core|i2c_ch341|i2c_dev)/ {print $1}' | paste -sd ',' -)"

if [ -n "$I2C_LINE" ]; then
    LEVEL="OK"
    MESSAGE="CH341 I2C disponible: $I2C_LINE"
else
    LEVEL="WARNING"

    if [ -n "$USB_LINE" ]; then
        MESSAGE="CH341 detectado por USB, pero no aparece como bus I2C. El driver puede no estar cargado."
    else
        MESSAGE="CH341 no detectado por USB. AHT10 y MPU6050 pueden quedar no disponibles."
    fi
fi

echo "[$TS] $LEVEL: $MESSAGE"
logger -t boya-preflight "$LEVEL: $MESSAGE"

cat > "$STATUS_FILE" <<EOF
{
  "timestamp": "$TS",
  "level": "$LEVEL",
  "message": "$MESSAGE",
  "usb_detected": $([ -n "$USB_LINE" ] && echo true || echo false),
  "i2c_bus_detected": $([ -n "$I2C_LINE" ] && echo true || echo false),
  "usb_line": "$USB_LINE",
  "i2c_line": "$I2C_LINE",
  "modules": "$MODULES"
}
EOF

exit 0
