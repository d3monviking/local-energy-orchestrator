"""gateway/devices.py — Modbus drivers for the devices the operator owns:
the three battery inverters (C2, Modbus TCP, contracts/modbus_map.yaml)
and the backup-circuit premise meters (C3, Modbus RTU in the field, TCP
to the mock here). Only the device contract registers are used — never
the mocks' simulation hooks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pymodbus.client import ModbusTcpClient

log = logging.getLogger("leo.gateway.devices")

MODE_IDLE, MODE_FOLLOW = 0, 1


def to_unsigned16(value: int) -> int:
    return int(value) & 0xFFFF


def to_signed16(value: int) -> int:
    return value - 0x10000 if value >= 0x8000 else value


class DeviceError(RuntimeError):
    pass


class _Link:
    def __init__(self, host: str, port: int, name: str):
        self.host, self.port, self.name = host, port, name
        self.client = ModbusTcpClient(host, port=port, timeout=3)

    def _ensure(self) -> None:
        if not self.client.connected and not self.client.connect():
            raise DeviceError(f"{self.name}: cannot connect to {self.host}:{self.port}")

    def write(self, unit: int, address: int, value: int) -> None:
        self._ensure()
        rr = self.client.write_register(address=address, value=to_unsigned16(value), device_id=unit)
        if rr.isError():
            raise DeviceError(f"{self.name} unit {unit}: write {address} failed: {rr}")

    def read(self, unit: int, address: int, count: int) -> list[int]:
        self._ensure()
        rr = self.client.read_holding_registers(address=address, count=count, device_id=unit)
        if rr.isError():
            raise DeviceError(f"{self.name} unit {unit}: read {address} failed: {rr}")
        return rr.registers

    def close(self) -> None:
        self.client.close()


@dataclass
class InverterTelemetry:
    soc: float              # fraction
    power_kw: float         # grid port, + discharge, - charge (what actually flowed)
    voltage_v: float
    grid_present: bool
    alarm_bits: int
    backup_kw: float        # backup port


class Inverters(_Link):
    """The three single-phase hybrid inverters, one Modbus unit per phase."""

    def __init__(self, host: str, port: int, unit_by_phase: dict[str, int]):
        super().__init__(host, port, "inverters")
        self.unit_by_phase = unit_by_phase

    def command(self, phase: str, setpoint_kw: float, watchdog_s: int) -> None:
        unit = self.unit_by_phase[phase]
        self.write(unit, 40012, watchdog_s)
        self.write(unit, 40010, MODE_FOLLOW)
        self.write(unit, 40011, round(setpoint_kw * 1000))

    def idle(self, phase: str) -> None:
        unit = self.unit_by_phase[phase]
        self.write(unit, 40011, 0)
        self.write(unit, 40010, MODE_IDLE)

    def read_telemetry(self, phase: str) -> InverterTelemetry:
        r = self.read(self.unit_by_phase[phase], 40000, 6)
        return InverterTelemetry(soc=r[0] / 1000.0, power_kw=to_signed16(r[1]) / 1000.0, voltage_v=r[2] / 10.0,
                                 grid_present=bool(r[3]), alarm_bits=r[4], backup_kw=r[5] / 1000.0)


@dataclass
class MeterReading:
    backup_kwh: float       # cumulative
    current_a: float
    limit_active: bool


class PremiseMeters(_Link):
    """Dual-source meters with current limiters on the backup circuit."""

    def __init__(self, host: str, port: int, unit_by_household: dict[str, int]):
        super().__init__(host, port, "premise-meters")
        self.unit_by_household = unit_by_household

    def set_limit(self, household_id: str, amps: float) -> None:
        self.write(self.unit_by_household[household_id], 30010, round(max(0.0, amps) * 10))

    def read_meter(self, household_id: str) -> MeterReading:
        r = self.read(self.unit_by_household[household_id], 30000, 3)
        return MeterReading(backup_kwh=r[0] / 100.0, current_a=r[1] / 10.0, limit_active=bool(r[2]))
