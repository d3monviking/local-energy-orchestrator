"""mocks/modbus_battery.py — pymodbus TCP server standing in for the three
single-phase hybrid inverters (C2). Owner B.

Register map is frozen in contracts/modbus_map.yaml and restated here:

  read-only   40000 soc_pct_x10        40004 alarm_bits
              40001 active_power_w     40005 backup_port_w
              40002 terminal_voltage_x10  40006 soc_min_pct_x10
              40003 grid_present       40007 soc_max_pct_x10
  writable    40010 mode (0 idle, 1 follow_setpoint, 2 backup)
              40011 setpoint_w (signed, + discharge, - charge)
              40012 watchdog_s (default 60)

Identical across unit IDs 1 (R), 2 (Y), 3 (B). If the orchestrator stops
writing 40011, the block reverts to idle after watchdog_s — real inverter
behaviour, and the orchestrator (gateway/orchestrator) must handle it,
not this mock.

A background task evolves SoC against simulated time (via sim/clock),
not wall-clock time, so battery state stays consistent with the rest of
the simulation at any clock speed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import StartAsyncTcpServer

try:
    from sim.clock_client import sim_now
except ImportError:  # pragma: no cover
    from datetime import datetime

    def sim_now() -> "datetime":
        return datetime.utcnow()

log = logging.getLogger("leo.mocks.modbus_battery")
logging.basicConfig(level=logging.INFO)

BASE_ADDR = 40000
BLOCK_SIZE = 20  # covers 40000-40019

HOST = os.environ.get("MODBUS_HOST", "0.0.0.0")
PORT = int(os.environ.get("MODBUS_PORT", "5020"))
PHYSICS_PERIOD_S = float(os.environ.get("MODBUS_PHYSICS_PERIOD_S", "1.0"))

# Matches scenario.yaml battery_blocks (one per phase). unit_id: (phase, capacity_kwh, power_kw)
UNIT_SPECS = {
    1: {"phase": "R", "capacity_kwh": 15.0, "power_kw": 7.5},
    2: {"phase": "Y", "capacity_kwh": 15.0, "power_kw": 7.5},
    3: {"phase": "B", "capacity_kwh": 15.0, "power_kw": 7.5},
}

MODE_IDLE, MODE_FOLLOW, MODE_BACKUP = 0, 1, 2


def to_unsigned16(value: int) -> int:
    return value & 0xFFFF


def to_signed16(value: int) -> int:
    return value - 0x10000 if value >= 0x8000 else value


REG_SETPOINT = 40011

# pymodbus's ModbusDeviceContext.getValues/setValues always does
# `address += 1` before indexing into the block (this replaced the old
# per-slave `zero_mode` flag). Every *documented* register address from
# modbus_map.yaml therefore has to be stored one raw address higher than
# written, both for the block's own base address and for every direct
# (non-protocol) access this module makes. `read_reg`/`write_reg` below
# are the only methods that take documented addresses; everything else
# in this module works in raw block-storage addresses.
PYMODBUS_ADDR_OFFSET = 1


class WatchdogBlock(ModbusSequentialDataBlock):
    """Same storage as the base class, but records the wall-clock time of
    any externally-received write to the setpoint register (40011), which
    is what the watchdog timeout (40012) is measured against.

    Internal physics updates must call `write_reg` instead of `setValues`
    so they are not mistaken for a command write.
    """

    def __init__(self, documented_base_addr: int, values: list[int]) -> None:
        super().__init__(documented_base_addr + PYMODBUS_ADDR_OFFSET, values)
        self.last_setpoint_write = time.monotonic()

    def setValues(self, address: int, values) -> None:  # noqa: N802 (pymodbus API)
        # Called by ModbusDeviceContext with `address` already shifted by
        # PYMODBUS_ADDR_OFFSET, i.e. in raw block-storage terms.
        super().setValues(address, values)
        documented = address - PYMODBUS_ADDR_OFFSET
        count = len(values) if hasattr(values, "__len__") else 1
        if documented <= REG_SETPOINT < documented + count:
            self.last_setpoint_write = time.monotonic()

    def read_reg(self, documented_addr: int, count: int = 1) -> list[int]:
        return self.getValues(documented_addr + PYMODBUS_ADDR_OFFSET, count)

    def write_reg(self, documented_addr: int, values: list[int]) -> None:
        # Bypasses the watchdog-tracking override above: this is the
        # physics loop writing telemetry, not an external command write.
        super().setValues(documented_addr + PYMODBUS_ADDR_OFFSET, values)


@dataclass
class BatteryPhysics:
    unit_id: int
    phase: str
    capacity_kwh: float
    power_kw: float
    soc: float = 0.5  # fraction
    soc_min: float = 0.15
    soc_max: float = 0.90
    efficiency: float = 0.92
    grid_present: bool = True
    _last_sim_time: object = field(default=None, repr=False)

    def step(self, block: WatchdogBlock, sim_elapsed_h: float) -> None:
        mode = block.read_reg(40010)[0]
        raw_setpoint = to_signed16(block.read_reg(REG_SETPOINT)[0])
        watchdog_s = block.read_reg(40012)[0] or 60

        watchdog_expired = (time.monotonic() - block.last_setpoint_write) > watchdog_s
        if watchdog_expired and mode == MODE_FOLLOW:
            mode = MODE_IDLE
            block.write_reg(40010, [MODE_IDLE])

        commanded_kw = 0.0
        if mode == MODE_FOLLOW:
            commanded_kw = max(-self.power_kw, min(self.power_kw, raw_setpoint / 1000.0))
        elif mode == MODE_BACKUP:
            commanded_kw = -min(self.power_kw, 1.0)  # placeholder backup draw until C9 lands

        # BMS override: clip so SoC never crosses its own bounds.
        actual_kw = commanded_kw
        if sim_elapsed_h > 0:
            if commanded_kw > 0:  # discharging
                max_deliverable_kwh = (self.soc - self.soc_min) * self.capacity_kwh
                max_kw = max_deliverable_kwh / sim_elapsed_h
                actual_kw = min(commanded_kw, max(0.0, max_kw))
            elif commanded_kw < 0:  # charging
                max_absorbable_kwh = (self.soc_max - self.soc) * self.capacity_kwh
                max_kw = max_absorbable_kwh / sim_elapsed_h
                actual_kw = -min(-commanded_kw, max(0.0, max_kw))

            delta_kwh = actual_kw * sim_elapsed_h
            if actual_kw > 0:
                delta_kwh /= self.efficiency
            else:
                delta_kwh *= self.efficiency
            self.soc = min(self.soc_max, max(self.soc_min, self.soc - delta_kwh / self.capacity_kwh))

        alarm_bits = 0
        if actual_kw != commanded_kw:
            alarm_bits |= 1 << 0  # bms_limit

        backup_w = max(0, int(-actual_kw * 1000)) if mode == MODE_BACKUP else 0

        block.write_reg(40000, [to_unsigned16(round(self.soc * 1000))])
        block.write_reg(40001, [to_unsigned16(round(actual_kw * 1000))])
        block.write_reg(40002, [to_unsigned16(round(230.0 * 10))])
        block.write_reg(40003, [1 if self.grid_present else 0])
        block.write_reg(40004, [alarm_bits])
        block.write_reg(40005, [backup_w])
        block.write_reg(40006, [to_unsigned16(round(self.soc_min * 1000))])
        block.write_reg(40007, [to_unsigned16(round(self.soc_max * 1000))])


def build_context() -> tuple[ModbusServerContext, dict[int, BatteryPhysics], dict[int, WatchdogBlock]]:
    devices: dict[int, ModbusDeviceContext] = {}
    physics: dict[int, BatteryPhysics] = {}
    blocks: dict[int, WatchdogBlock] = {}

    for unit_id, spec in UNIT_SPECS.items():
        block = WatchdogBlock(BASE_ADDR, [0] * BLOCK_SIZE)
        block.write_reg(40010, [MODE_IDLE])
        block.write_reg(40011, [0])
        block.write_reg(40012, [60])
        devices[unit_id] = ModbusDeviceContext(hr=block)
        physics[unit_id] = BatteryPhysics(
            unit_id=unit_id, phase=spec["phase"],
            capacity_kwh=spec["capacity_kwh"], power_kw=spec["power_kw"],
        )
        blocks[unit_id] = block

    context = ModbusServerContext(devices=devices, single=False)
    return context, physics, blocks


async def _physics_loop(physics: dict[int, BatteryPhysics], blocks: dict[int, WatchdogBlock]) -> None:
    # The clock service can still be starting when this container is up
    # (compose starts containers concurrently regardless of `depends_on`
    # readiness), so the first call must retry rather than let an
    # unhandled exception silently kill this background task forever.
    last_sim = None
    while last_sim is None:
        try:
            last_sim = sim_now()
        except Exception as exc:
            log.warning("clock unreachable at startup (%s); retrying", exc)
            await asyncio.sleep(PHYSICS_PERIOD_S)

    while True:
        await asyncio.sleep(PHYSICS_PERIOD_S)
        try:
            current_sim = sim_now()
        except Exception as exc:
            log.warning("clock unreachable (%s); skipping physics step", exc)
            continue
        elapsed_h = max(0.0, (current_sim - last_sim).total_seconds() / 3600.0)
        last_sim = current_sim
        try:
            for unit_id, phys in physics.items():
                phys.step(blocks[unit_id], elapsed_h)
        except Exception:
            log.exception("physics step failed; continuing on the next tick")


async def main() -> None:
    context, physics, blocks = build_context()
    asyncio.create_task(_physics_loop(physics, blocks))
    log.info("mock-modbus battery server on %s:%s, units %s", HOST, PORT, list(UNIT_SPECS))
    await StartAsyncTcpServer(context=context, address=(HOST, PORT))


if __name__ == "__main__":
    asyncio.run(main())
