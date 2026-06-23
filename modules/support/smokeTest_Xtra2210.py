#!/usr/bin/env python3
import os
import struct
import time

try:
    import serial
except ImportError:
    print("Falta pyserial. Instalalo con:")
    print("  sudo apt install python3-serial")
    raise SystemExit(1)


PORT = "/dev/ttyS4"
BAUDRATE = 115200
SLAVE_ID = 1
TIMEOUT = 0.8
INTER_FRAME_DELAY = 0.15

# Registros de entrada comunes en controladores EPEVER serie XTRA/XTRA-N.
# Se leen varios bloques para confirmar presencia con más evidencia que una sola respuesta.
REGISTER_BLOCKS = [
    (0x3100, 2, "PV input voltage/current"),
    (0x310C, 4, "Load voltage/current/power"),
    (0x311A, 2, "Battery SOC / temperature"),
    (0x311D, 1, "Battery real rated voltage"),
    (0x3200, 2, "Battery temperature / device temperature"),
]


def modbus_crc(data: bytes) -> int:
    crc = 0xFFFF
    for ch in data:
        crc ^= ch
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def build_read_input_registers_request(
    slave_id: int, start_reg: int, count: int
) -> bytes:
    payload = struct.pack(">BBHH", slave_id, 0x04, start_reg, count)
    crc = modbus_crc(payload)
    return payload + struct.pack("<H", crc)


def expected_response_length(register_count: int) -> int:
    return 1 + 1 + 1 + (2 * register_count) + 2


def parse_modbus_response(resp: bytes, slave_id: int, register_count: int):
    if len(resp) < 5:
        return False, "respuesta demasiado corta"

    data = resp[:-2]
    rx_crc = struct.unpack("<H", resp[-2:])[0]
    calc_crc = modbus_crc(data)
    if rx_crc != calc_crc:
        return False, f"CRC inválido (rx=0x{rx_crc:04X}, calc=0x{calc_crc:04X})"

    if resp[0] != slave_id:
        return False, f"slave id inesperado ({resp[0]})"

    func = resp[1]
    if func == 0x84:
        if len(resp) >= 5:
            exc = resp[2]
            return False, f"excepción Modbus 0x{exc:02X}"
        return False, "excepción Modbus"

    if func != 0x04:
        return False, f"función inesperada 0x{func:02X}"

    byte_count = resp[2]
    if byte_count != register_count * 2:
        return False, f"byte count inesperado ({byte_count})"

    regs = []
    for i in range(register_count):
        off = 3 + 2 * i
        regs.append(struct.unpack(">H", resp[off : off + 2])[0])

    return True, regs


def regs_to_u32(high_reg: int, low_reg: int) -> int:
    return ((high_reg & 0xFFFF) << 16) | (low_reg & 0xFFFF)


def decode_block(start_reg: int, regs):
    decoded = {}

    if start_reg == 0x3100 and len(regs) >= 2:
        decoded["pv_voltage_v"] = regs[0] / 100.0
        decoded["pv_current_a"] = regs[1] / 100.0

    elif start_reg == 0x310C and len(regs) >= 4:
        decoded["load_voltage_v"] = regs[0] / 100.0
        decoded["load_current_a"] = regs[1] / 100.0
        decoded["load_power_w"] = regs_to_u32(regs[3], regs[2]) / 100.0

    elif start_reg == 0x311A and len(regs) >= 2:
        decoded["battery_soc_pct"] = regs[0]
        raw_temp = regs[1]
        decoded["battery_temp_c"] = (
            raw_temp / 100.0 if raw_temp not in (0x7FFF, 0xFFFF) else None
        )

    elif start_reg == 0x311D and len(regs) >= 1:
        decoded["battery_rated_voltage_v"] = regs[0] / 100.0

    elif start_reg == 0x3200 and len(regs) >= 2:
        decoded["battery_temp_c"] = (
            regs[0] / 100.0 if regs[0] not in (0x7FFF, 0xFFFF) else None
        )
        decoded["device_temp_c"] = (
            regs[1] / 100.0 if regs[1] not in (0x7FFF, 0xFFFF) else None
        )

    return decoded


def values_look_plausible(decoded: dict) -> bool:
    plausible = False

    if "pv_voltage_v" in decoded and 0.0 <= decoded["pv_voltage_v"] <= 200.0:
        plausible = True
    if "pv_current_a" in decoded and 0.0 <= decoded["pv_current_a"] <= 100.0:
        plausible = True
    if "load_voltage_v" in decoded and 0.0 <= decoded["load_voltage_v"] <= 100.0:
        plausible = True
    if "battery_soc_pct" in decoded and 0 <= decoded["battery_soc_pct"] <= 100:
        plausible = True
    if "battery_rated_voltage_v" in decoded and decoded["battery_rated_voltage_v"] in (
        12.0,
        24.0,
        36.0,
        48.0,
    ):
        plausible = True
    if (
        "device_temp_c" in decoded
        and decoded["device_temp_c"] is not None
        and -40.0 <= decoded["device_temp_c"] <= 120.0
    ):
        plausible = True

    return plausible


def read_register_block(ser, slave_id: int, start_reg: int, reg_count: int):
    req = build_read_input_registers_request(slave_id, start_reg, reg_count)
    resp_len = expected_response_length(reg_count)

    ser.reset_input_buffer()
    ser.write(req)
    ser.flush()
    time.sleep(INTER_FRAME_DELAY)
    resp = ser.read(resp_len)

    if not resp:
        return False, "timeout"

    ok, result = parse_modbus_response(resp, slave_id, reg_count)
    if not ok:
        return False, result

    return True, result


def main():
    print("Verificando presencia de EPEVER XTRA2210 únicamente en:")
    print(f"  puerto={PORT}  baud={BAUDRATE}  slave={SLAVE_ID}\n")

    if not os.path.exists(PORT):
        print(f"ERROR: no existe {PORT}")
        raise SystemExit(2)

    collected = {}
    successes = 0
    plausible_hits = 0

    try:
        with serial.Serial(
            port=PORT,
            baudrate=BAUDRATE,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=TIMEOUT,
        ) as ser:
            for start_reg, reg_count, description in REGISTER_BLOCKS:
                ok, result = read_register_block(ser, SLAVE_ID, start_reg, reg_count)
                if not ok:
                    print(f"[FAIL] 0x{start_reg:04X} ({description}): {result}")
                    continue

                successes += 1
                decoded = decode_block(start_reg, result)
                collected[start_reg] = {
                    "description": description,
                    "raw": result,
                    "decoded": decoded,
                }

                print(f"[OK]   0x{start_reg:04X} ({description})")
                print(f"       raw={result}")
                if decoded:
                    print(f"       decodificado={decoded}")
                    if values_look_plausible(decoded):
                        plausible_hits += 1
                else:
                    print("       decodificado={}")

    except serial.SerialException as e:
        print(f"ERROR: no se pudo abrir/probar {PORT}: {e}")
        raise SystemExit(3)

    print("\nResumen:")
    print(f"  bloques respondidos: {successes}/{len(REGISTER_BLOCKS)}")
    print(f"  bloques con valores plausibles: {plausible_hits}")

    if successes >= 2 and plausible_hits >= 1:
        print(
            "\nCONFIRMADO: hay evidencia fuerte de un controlador EPEVER XTRA2210/XTRA en la línea RS485."
        )
        raise SystemExit(0)

    if successes >= 1:
        print(
            "\nRESPONDE MODBUS, pero la identificación no quedó suficientemente confirmada como XTRA2210."
        )
        raise SystemExit(4)

    print("\nNo se detectó respuesta válida del dispositivo esperado.")
    raise SystemExit(5)


if __name__ == "__main__":
    main()
