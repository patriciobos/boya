#!/usr/bin/env bash
set -euo pipefail

BUS="${1:-12}"
ADDR="${2:-0x38}"
WAIT_S="${3:-0.1}"

echo "AHT10 read"
echo "  bus:   $BUS"
echo "  addr:  $ADDR"
echo "  wait:  ${WAIT_S}s"
echo

# Trigger measurement
sudo i2ctransfer -y "$BUS" w3@"$ADDR" 0xAC 0x33 0x00 >/dev/null

# Wait for conversion
sleep "$WAIT_S"

# Read 6 bytes
RAW_OUTPUT="$(sudo i2ctransfer -y "$BUS" r6@"$ADDR")"
echo "Raw bytes: $RAW_OUTPUT"

# Convert "0x08 0x66 ..." into bash array
read -r -a HEX <<< "$RAW_OUTPUT"

if [ "${#HEX[@]}" -ne 6 ]; then
  echo "ERROR: expected 6 bytes, got ${#HEX[@]}" >&2
  exit 1
fi

# Hex string -> decimal
to_dec() {
  printf "%d" "$((16#${1#0x}))"
}

B0="$(to_dec "${HEX[0]}")"
B1="$(to_dec "${HEX[1]}")"
B2="$(to_dec "${HEX[2]}")"
B3="$(to_dec "${HEX[3]}")"
B4="$(to_dec "${HEX[4]}")"
B5="$(to_dec "${HEX[5]}")"

BUSY=$(( (B0 >> 7) & 0x01 ))
CALIBRATED=$(( (B0 >> 3) & 0x01 ))

# 20-bit raw humidity
RAW_H=$(( (B1 << 12) | (B2 << 4) | (B3 >> 4) ))

# 20-bit raw temperature
RAW_T=$(( ((B3 & 0x0F) << 16) | (B4 << 8) | B5 ))

# Use awk for floating-point math
HUMIDITY="$(awk -v raw="$RAW_H" 'BEGIN { printf "%.2f", (raw / 1048576.0) * 100.0 }')"
TEMPERATURE="$(awk -v raw="$RAW_T" 'BEGIN { printf "%.2f", (raw / 1048576.0) * 200.0 - 50.0 }')"

echo
echo "Decoded:"
echo "  status byte : ${HEX[0]} (dec $B0)"
echo "  busy        : $BUSY"
echo "  calibrated  : $CALIBRATED"
echo "  raw humidity: $RAW_H"
echo "  raw temp    : $RAW_T"
echo "  humidity    : ${HUMIDITY} %RH"
echo "  temperature : ${TEMPERATURE} °C"
