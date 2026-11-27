"""mocks/modbus_meters.py — pymodbus TCP server standing in for the dual-
source premise backup meters on the operator's backup circuit (C3: Rishabh
EM3490DSi / ABB M1M DS class devices, Modbus RTU in the field).

Unit IDs 1..N, one per registered backup premise, in household_id order
of the premise_backup registry (gateway/devices.py and sim/loop.py both
derive the mapping from that table). This register layout is the mock's
own; contracts/modbus_map.yaml freezes only the battery blocks.

  read-only   30000 backup_kwh_x100      cumulative energy served on backup
              30001 current_a_x10        present backup-circuit current
              30002 limit_active         1 when the limiter is holding the premise below its draw
  writable    30010 current_limit_a_x10  per-premise limit, written by the gateway (C9)

The meter itself does not decide anything: the gateway sets the limit and
the simulated world (sim/loop.py) writes what the premise actually drew
through it, as the physical meter would measure.
"""

from __future__ import annotations

import asyncio
import logging
import os

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import StartAsyncTcpServer

log = logging.getLogger("leo.mocks.modbus_meters")
logging.basicConfig(level=logging.INFO)

BASE_ADDR = 30000
BLOCK_SIZE = 20

HOST = os.environ.get("MODBUS_METERS_HOST", "0.0.0.0")
PORT = int(os.environ.get("MODBUS_METERS_PORT", "5021"))
N_UNITS = int(os.environ.get("PREMISE_METER_UNITS", "16"))


def build_context(n_units: int = N_UNITS) -> ModbusServerContext:
    # +1: ModbusDeviceContext shifts the address by one before indexing
    # the block (see modbus_battery.py's PYMODBUS_ADDR_OFFSET).
    devices = {u: ModbusDeviceContext(hr=ModbusSequentialDataBlock(BASE_ADDR + 1, [0] * BLOCK_SIZE))
               for u in range(1, n_units + 1)}
    return ModbusServerContext(devices=devices, single=False)


async def main() -> None:
    log.info("mock premise meters on %s:%s, units 1..%d", HOST, PORT, N_UNITS)
    await StartAsyncTcpServer(context=build_context(), address=(HOST, PORT))


if __name__ == "__main__":
    asyncio.run(main())
