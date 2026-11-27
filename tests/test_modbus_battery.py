"""mocks/modbus_battery.py: the inverter behaviour the gateway must live with."""

import pytest

from mocks.modbus_battery import (
    REG_MODE, REG_POWER, REG_SETPOINT, REG_SIM_ADVANCE, REG_SIM_BACKUP_W, REG_SIM_GRID, REG_SIM_SOC,
    REG_SIM_STEPPED, REG_WATCHDOG, InverterPhysics, InverterRegisters, to_signed16, to_unsigned16,
)

OFFSET = 1  # pymodbus adds 1 before indexing; external writes arrive shifted


def unit(soc=0.5):
    u = InverterPhysics(unit_id=1, regs=InverterRegisters([0] * 120), capacity_kwh=60.0, power_kw=15.0)
    ext(u, REG_SIM_STEPPED, 1)
    ext(u, REG_SIM_SOC, round(soc * 1000))
    return u


def ext(u, reg, value):
    """A write as it arrives from a Modbus client."""
    u.regs.setValues(reg + OFFSET, [to_unsigned16(value)])


def command(u, kw, watchdog=1800):
    ext(u, REG_WATCHDOG, watchdog)
    ext(u, REG_MODE, 1)
    ext(u, REG_SETPOINT, round(kw * 1000))


def power_kw(u):
    return to_signed16(u.regs.read_reg(REG_POWER)[0]) / 1000.0


def test_discharge_follows_setpoint_with_sweep_energy_accounting():
    u = unit(0.5)
    command(u, 10.0)
    ext(u, REG_SIM_ADVANCE, 900)
    assert power_kw(u) == pytest.approx(10.0)
    assert u.soc == pytest.approx(0.5 - 10.0 * 0.25 / 0.92 / 60.0)


def test_bms_clips_at_the_soc_floor():
    u = unit(0.16)
    command(u, 15.0)
    ext(u, REG_SIM_ADVANCE, 900)
    assert power_kw(u) == pytest.approx((0.16 - 0.15) * 60 * 0.92 / 0.25, abs=1e-3)
    assert u.soc == pytest.approx(0.15, abs=1e-4)
    assert u.regs.read_reg(40004)[0] & 1  # bms_limit alarm


def test_watchdog_counts_simulated_seconds():
    u = unit(0.5)
    command(u, 5.0, watchdog=1800)
    ext(u, REG_SIM_ADVANCE, 900)
    ext(u, REG_SIM_ADVANCE, 900)      # one missed write: keeps the last setpoint
    assert power_kw(u) == pytest.approx(5.0)
    ext(u, REG_SIM_ADVANCE, 900)      # two: idle
    assert power_kw(u) == 0.0 and u.regs.read_reg(REG_MODE)[0] == 0


def test_grid_loss_islands_onto_backup_port_down_to_hard_floor():
    u = unit(0.10)
    command(u, 10.0)                  # ignored: the grid port is open
    ext(u, REG_SIM_GRID, 0)
    ext(u, REG_SIM_BACKUP_W, 4000)
    ext(u, REG_SIM_ADVANCE, 900)
    assert power_kw(u) == 0.0
    assert u.regs.read_reg(40003)[0] == 0
    assert u.regs.read_reg(40005)[0] / 1000.0 == pytest.approx(4.0)
    for _ in range(8):
        ext(u, REG_SIM_ADVANCE, 900)
    assert u.soc == pytest.approx(0.05, abs=1e-4)  # backup digs below 15%, never below 5%
