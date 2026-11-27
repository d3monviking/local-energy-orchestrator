"""mocks/modbus_battery.py — pymodbus TCP server standing in for the three
single-phase hybrid inverters (C2), one unit ID per phase (1 R, 2 Y, 3 B).

Device contract (contracts/modbus_map.yaml, frozen):

  read-only   40000 soc_pct_x10 (x1000 fraction)   40004 alarm_bits
              40001 active_power_w (grid port)      40005 backup_port_w
              40002 terminal_voltage_x10            40006 soc_min_pct_x10
              40003 grid_present                    40007 soc_max_pct_x10
  writable    40010 mode (0 idle, 1 follow_setpoint, 2 backup)
              40011 setpoint_w (signed, + discharge, - charge)
              40012 watchdog_s

Behaviour a real inverter has and the gateway must live with:
  * watchdog: no write to 40011 for watchdog_s -> the block goes idle;
  * BMS: the pack clips any setpoint that would cross its SoC window;
    40001 reports what actually flowed, not what was commanded;
  * anti-islanding: on grid loss the grid port opens by itself and the
    block serves only its backup port, whatever the gateway asked for.

Simulation hooks (40100-40106) are NOT part of the device contract. They
are how the simulated world (sim/loop.py) plays physics: size the pack,
set its state of charge at the start of a run, say whether the grid is
present at the terminals and what the backup circuit draws, and advance
the pack by an exact number of simulated seconds once the gateway has
acted. Writing 40100 = 1 switches a unit to that stepped mode; in the
default free-running mode the pack integrates against the clock service.

  40100 sim_stepped          0 free-running (clock service) / 1 stepped
  40101 sim_capacity_kwh_x10
  40102 sim_power_kw_x10
  40103 sim_soc_x1000        write: set the state of charge
  40104 sim_grid_present     0/1, the grid at the block's terminals
  40105 sim_backup_demand_w  what the backup premises would draw now
  40106 sim_advance_s        write N: integrate N simulated seconds now
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import StartAsyncTcpServer

try:
    from sim.clock_client import sim_now
except ImportError:  # pragma: no cover
    from datetime import datetime, timezone

    def sim_now() -> "datetime":
        return datetime.now(timezone.utc)

log = logging.getLogger("leo.mocks.modbus_battery")
logging.basicConfig(level=logging.INFO)

BASE_ADDR = 40000
BLOCK_SIZE = 120  # 40000-40119: the device contract plus the simulation hooks

HOST = os.environ.get("MODBUS_HOST", "0.0.0.0")
PORT = int(os.environ.get("MODBUS_PORT", "5020"))
PHYSICS_PERIOD_S = float(os.environ.get("MODBUS_PHYSICS_PERIOD_S", "1.0"))
NOMINAL_V = float(os.environ.get("NOMINAL_V_LN", "250.0"))
DEFAULT_CAPACITY_KWH = float(os.environ.get("BATTERY_CAPACITY_KWH", "60"))  # scenario.yaml battery_blocks
DEFAULT_POWER_KW = float(os.environ.get("BATTERY_POWER_KW", "15"))

UNIT_PHASE = {1: "R", 2: "Y", 3: "B"}
MODE_IDLE, MODE_FOLLOW, MODE_BACKUP = 0, 1, 2

REG_SOC, REG_POWER, REG_VOLT, REG_GRID, REG_ALARM, REG_BACKUP_W, REG_SOC_MIN, REG_SOC_MAX = range(40000, 40008)
REG_MODE, REG_SETPOINT, REG_WATCHDOG = 40010, 40011, 40012
REG_SIM_STEPPED, REG_SIM_CAP, REG_SIM_POWER, REG_SIM_SOC, REG_SIM_GRID, REG_SIM_BACKUP_W, REG_SIM_ADVANCE = range(40100, 40107)
SIM_HOOKS = set(range(REG_SIM_STEPPED, REG_SIM_ADVANCE + 1))

ALARM_BMS_LIMIT = 1 << 0

# pymodbus's ModbusDeviceContext.getValues/setValues add 1 to the address
# before indexing the block, so every documented register is stored one
# raw address higher. read_reg/write_reg take documented addresses.
PYMODBUS_ADDR_OFFSET = 1


def to_unsigned16(value: int) -> int:
    return int(value) & 0xFFFF


def to_signed16(value: int) -> int:
    return value - 0x10000 if value >= 0x8000 else value


class InverterRegisters(ModbusSequentialDataBlock):
    """Register storage that notices external writes: a setpoint write
    feeds the watchdog; a simulation-hook write is handed to the unit's
    physics. Internal telemetry updates go through write_reg, which
    bypasses both."""

    def __init__(self, values: list[int]) -> None:
        super().__init__(BASE_ADDR + PYMODBUS_ADDR_OFFSET, values)
        self.last_setpoint_wall = time.monotonic()
        self.sim_s_since_setpoint = 0.0
        self.on_hook = None  # set by InverterPhysics

    def setValues(self, address: int, values) -> None:  # noqa: N802 (pymodbus API)
        super().setValues(address, values)
        first = address - PYMODBUS_ADDR_OFFSET
        vals = list(values) if hasattr(values, "__len__") else [values]
        for k, v in enumerate(vals):
            reg = first + k
            if reg == REG_SETPOINT:
                self.last_setpoint_wall = time.monotonic()
                self.sim_s_since_setpoint = 0.0
            elif reg in SIM_HOOKS and self.on_hook is not None:
                self.on_hook(reg, v)

    def read_reg(self, documented_addr: int, count: int = 1) -> list[int]:
        return self.getValues(documented_addr + PYMODBUS_ADDR_OFFSET, count)

    def write_reg(self, documented_addr: int, values: list[int]) -> None:
        super().setValues(documented_addr + PYMODBUS_ADDR_OFFSET, values)


@dataclass
class InverterPhysics:
    """One single-phase hybrid inverter + second-life LFP pack + BMS.

    Energy accounting is the same as eval/sweep.py's world: discharge
    delivers `eff` of what leaves the pack, charge stores `eff` of what
    is drawn, and the BMS keeps SoC within [soc_min, soc_max] on the grid
    port and down to `backup_floor` when serving the backup port."""

    unit_id: int
    regs: InverterRegisters
    capacity_kwh: float = DEFAULT_CAPACITY_KWH
    power_kw: float = DEFAULT_POWER_KW
    soc: float = 0.5
    soc_min: float = 0.15
    soc_max: float = 0.90
    backup_floor: float = 0.05
    efficiency: float = 0.92
    grid_present: bool = True
    backup_demand_kw: float = 0.0
    stepped: bool = False

    def __post_init__(self) -> None:
        self.regs.on_hook = self.hook
        self.regs.write_reg(REG_MODE, [MODE_IDLE])
        self.regs.write_reg(REG_SETPOINT, [0])
        self.regs.write_reg(REG_WATCHDOG, [60])
        self.regs.write_reg(REG_SIM_GRID, [1])
        self.publish(0.0, 0.0, 0)

    # ---- simulation hooks -------------------------------------------------

    def hook(self, reg: int, value: int) -> None:
        if reg == REG_SIM_STEPPED:
            self.stepped = bool(value)
        elif reg == REG_SIM_CAP:
            self.capacity_kwh = value / 10.0
        elif reg == REG_SIM_POWER:
            self.power_kw = value / 10.0
        elif reg == REG_SIM_SOC:
            self.soc = value / 1000.0
            self.regs.sim_s_since_setpoint = 0.0
            self.publish(0.0, 0.0, 0)
        elif reg == REG_SIM_GRID:
            self.grid_present = bool(value)
            self.publish(0.0, 0.0, 0)
        elif reg == REG_SIM_BACKUP_W:
            self.backup_demand_kw = value / 1000.0
        elif reg == REG_SIM_ADVANCE:
            self.step(value)

    # ---- physics ----------------------------------------------------------

    def _watchdog_expired(self) -> bool:
        watchdog_s = self.regs.read_reg(REG_WATCHDOG)[0] or 60
        if self.stepped:
            return self.regs.sim_s_since_setpoint >= watchdog_s
        return (time.monotonic() - self.regs.last_setpoint_wall) > watchdog_s

    def step(self, seconds: float) -> None:
        """Integrate `seconds` of simulated time with the current command."""
        hours = seconds / 3600.0
        if hours <= 0:
            return
        mode = self.regs.read_reg(REG_MODE)[0]
        if mode == MODE_FOLLOW and self._watchdog_expired():
            mode = MODE_IDLE
            self.regs.write_reg(REG_MODE, [MODE_IDLE])
            log.warning("unit %d: watchdog expired, reverting to idle", self.unit_id)
        if self.stepped:
            self.regs.sim_s_since_setpoint += seconds

        grid_kw, backup_kw, alarms = 0.0, 0.0, 0
        eff, cap = self.efficiency, self.capacity_kwh
        if not self.grid_present:
            # Anti-islanded: only the backup port is live, down to the hard floor.
            deliverable = max(0.0, (self.soc - self.backup_floor) * cap * eff / hours)
            backup_kw = min(self.backup_demand_kw, self.power_kw, deliverable)
            if backup_kw < self.backup_demand_kw - 1e-9:
                alarms |= ALARM_BMS_LIMIT
            self.soc -= backup_kw * hours / eff / cap
        elif mode == MODE_FOLLOW:
            commanded = max(-self.power_kw, min(self.power_kw, to_signed16(self.regs.read_reg(REG_SETPOINT)[0]) / 1000.0))
            if commanded > 0:
                grid_kw = min(commanded, max(0.0, (self.soc - self.soc_min) * cap * eff / hours))
                self.soc -= grid_kw * hours / eff / cap
            elif commanded < 0:
                grid_kw = -min(-commanded, max(0.0, (self.soc_max - self.soc) * cap / eff / hours))
                self.soc -= grid_kw * hours * eff / cap
            if abs(grid_kw - commanded) > 1e-3:
                alarms |= ALARM_BMS_LIMIT
        self.publish(grid_kw, backup_kw, alarms)

    def publish(self, grid_kw: float, backup_kw: float, alarms: int) -> None:
        r = self.regs
        r.write_reg(REG_SOC, [to_unsigned16(round(self.soc * 1000))])
        r.write_reg(REG_POWER, [to_unsigned16(round(grid_kw * 1000))])
        r.write_reg(REG_VOLT, [to_unsigned16(round((NOMINAL_V if self.grid_present else 0.0) * 10))])
        r.write_reg(REG_GRID, [1 if self.grid_present else 0])
        r.write_reg(REG_ALARM, [alarms])
        r.write_reg(REG_BACKUP_W, [to_unsigned16(round(backup_kw * 1000))])
        r.write_reg(REG_SOC_MIN, [to_unsigned16(round(self.soc_min * 1000))])
        r.write_reg(REG_SOC_MAX, [to_unsigned16(round(self.soc_max * 1000))])


def build_context() -> tuple[ModbusServerContext, dict[int, InverterPhysics]]:
    devices, units = {}, {}
    for unit_id in UNIT_PHASE:
        regs = InverterRegisters([0] * BLOCK_SIZE)
        units[unit_id] = InverterPhysics(unit_id=unit_id, regs=regs)
        devices[unit_id] = ModbusDeviceContext(hr=regs)
    return ModbusServerContext(devices=devices, single=False), units


async def _free_running_physics(units: dict[int, InverterPhysics]) -> None:
    """Free-running mode: integrate against the clock service's sim time.
    Units a simulation has switched to stepped mode are left alone."""
    last = None
    while True:
        await asyncio.sleep(PHYSICS_PERIOD_S)
        try:
            now = sim_now()
        except Exception as exc:
            log.debug("clock unreachable (%s); skipping free-running step", exc)
            continue
        if last is not None:
            elapsed_s = max(0.0, (now - last).total_seconds())
            for unit in units.values():
                if not unit.stepped:
                    try:
                        unit.step(elapsed_s)
                    except Exception:
                        log.exception("unit %d physics step failed", unit.unit_id)
        last = now


async def main() -> None:
    context, units = build_context()
    asyncio.create_task(_free_running_physics(units))
    log.info("mock inverters on %s:%s, units %s, %.0f kWh / %.1f kW each",
             HOST, PORT, list(units), DEFAULT_CAPACITY_KWH, DEFAULT_POWER_KW)
    await StartAsyncTcpServer(context=context, address=(HOST, PORT))


if __name__ == "__main__":
    asyncio.run(main())
