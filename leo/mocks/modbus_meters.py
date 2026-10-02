"""mocks/modbus_meters.py — pymodbus server standing in for the dual-source
premise backup meters (C3: Rishabh EM3490DSi / ABB M1M DS class devices).
Owner B.

Not covered by contracts/modbus_map.yaml (that file freezes only the three
battery blocks, C2); this register layout is this mock's own and is
documented here rather than in the frozen contract. One unit ID per
registered premise, assigned by premise index at startup from the
`premise_backup` registry (gateway/outage.py writes that registry; this
mock just needs the household IDs, read from Postgres at startup).

  read-only   30000 backup_kwh_x100     (cumulative backup energy)
              30001 current_a_x10
              30002 limit_active        (0/1)
  writable    30010 current_limit_a_x10 (per-premise limit, C9 writes this)

Real accumulation happens once gateway/outage.py (C9, Day 4) starts
driving backup allocation during an outage; until then every premise
reports zero draw, which is correct (no outage, no backup circuit energised).
"""

from __future__ import annotations

import asyncio
import logging
import os

import psycopg2
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


def _fetch_premise_ids() -> list[str]:
    try:
        conn = psycopg2.connect(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            user=os.environ.get("POSTGRES_USER", "leo"),
            password=os.environ.get("POSTGRES_PASSWORD", "leo"),
            dbname=os.environ.get("POSTGRES_DB", "leo"),
        )
        with conn, conn.cursor() as cur:
            cur.execute("SELECT household_id FROM premise_backup ORDER BY household_id")
            return [row[0] for row in cur.fetchall()]
    except Exception as exc:  # world.build may not have run yet
        log.warning("could not read premise_backup registry (%s); starting with no premises", exc)
        return []


def build_context(premise_ids: list[str]) -> tuple[ModbusServerContext, dict[int, str]]:
    devices: dict[int, ModbusDeviceContext] = {}
    unit_by_premise: dict[int, str] = {}
    for idx, household_id in enumerate(premise_ids, start=1):
        # +1: ModbusDeviceContext.getValues/setValues always shift the
        # incoming address by 1 before indexing into the block (see
        # modbus_battery.py's PYMODBUS_ADDR_OFFSET for the full story).
        block = ModbusSequentialDataBlock(BASE_ADDR + 1, [0] * BLOCK_SIZE)
        devices[idx] = ModbusDeviceContext(hr=block)
        unit_by_premise[idx] = household_id
    context = ModbusServerContext(devices=devices, single=False)
    return context, unit_by_premise


async def main() -> None:
    premise_ids = _fetch_premise_ids()
    context, unit_by_premise = build_context(premise_ids)
    log.info("mock-modbus premise meters on %s:%s, %d premises", HOST, PORT, len(premise_ids))
    await StartAsyncTcpServer(context=context, address=(HOST, PORT))


if __name__ == "__main__":
    asyncio.run(main())
