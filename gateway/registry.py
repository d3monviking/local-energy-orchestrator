"""gateway/registry.py — what the gateway knows about its transformer.

Loaded from the registry tables (contracts/ddl.sql) that a DISCOM GIS /
consumer-indexing export fills in the field and world/build.py fills in
the prototype: the LV network, households and their phases, LEO's own
sensors, battery blocks and backup premises. The gateway builds its own
network model (C6) from these, never from the simulator's world objects.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from gateway.controller import BlockSpec
from gateway.network_model import build_pandapower_net, compute_phase_limits, run_power_flow


@dataclass
class Feeder:
    """The subset of world.feeder.FeederResult that network_model needs."""
    bus: pd.DataFrame
    line: pd.DataFrame
    household: pd.DataFrame
    root: str


@dataclass
class Registry:
    dt_id: str
    transformer_kva: float
    nominal_v_ln: float
    v_limit_pct: float
    feeder: Feeder
    sensors: pd.DataFrame        # id, bus_id, phase, placement, dev_eui
    blocks: dict                 # phase -> BlockSpec
    unit_by_phase: dict          # phase -> Modbus unit id
    premises: pd.DataFrame       # household_id, phase, priority_class, max_current_a (sorted by household_id)

    @property
    def meter_unit_by_household(self) -> dict[str, int]:
        """Premise-meter Modbus unit IDs: 1..N in household_id order."""
        return {h: k for k, h in enumerate(self.premises["household_id"], start=1)}

    def voltage_sensitivity(self) -> dict[str, float]:
        """Per-phase busbar dV/dP (pu per kW of battery power) from the
        three-phase power flow, probed once at no load — the same probe
        eval/sweep.py uses. It is set by the transformer impedance and
        barely moves through the day."""
        net = build_pandapower_net(self.feeder, self.transformer_kva, self.nominal_v_ln)
        run_power_flow(net)
        return {p: max(compute_phase_limits(net, self.feeder.root, p, self.v_limit_pct, b.power_kw).dv_dp_pu_per_kw, 1e-6)
                for p, b in self.blocks.items()}


def _frame(cur, sql: str, params=()) -> pd.DataFrame:
    cur.execute(sql, params)
    return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def load_registry(conn, dt_id: str | None = None) -> Registry:
    cur = conn.cursor()
    nb = _frame(cur, "SELECT id, transformer_kva, nominal_v_ln, v_limit_pct FROM neighbourhood"
                     + (" WHERE id = %s" if dt_id else " ORDER BY id LIMIT 1"), (dt_id,) if dt_id else ())
    if nb.empty:
        raise LookupError("no neighbourhood registered — run `python -m world.build` first")
    nb = nb.iloc[0]
    bus = _frame(cur, "SELECT id, is_transformer FROM bus WHERE neighbourhood_id = %s", (nb["id"],))
    root = bus.loc[bus["is_transformer"], "id"]
    if root.empty:
        raise LookupError(f"neighbourhood {nb['id']} has no transformer bus")
    line = _frame(cur, """SELECT l.id, l.from_bus, l.to_bus, l.length_m, l.r_ohm_per_km, l.x_ohm_per_km, l.ampacity_a
                          FROM line l JOIN bus b ON b.id = l.from_bus WHERE b.neighbourhood_id = %s ORDER BY l.id""",
                  (nb["id"],))
    household = _frame(cur, """SELECT h.id, h.bus_id, h.phase::text AS phase FROM household h
                               JOIN bus b ON b.id = h.bus_id WHERE b.neighbourhood_id = %s ORDER BY h.id""", (nb["id"],))
    sensors = _frame(cur, """SELECT s.id, s.bus_id, s.phase::text AS phase, s.placement::text AS placement, s.dev_eui
                             FROM sensor s JOIN bus b ON b.id = s.bus_id WHERE b.neighbourhood_id = %s ORDER BY s.id""",
                     (nb["id"],))
    blocks_df = _frame(cur, """SELECT id, phase::text AS phase, unit_id, capacity_kwh, power_kw, soc_min, soc_max, efficiency
                               FROM battery_block ORDER BY unit_id""")
    premises = _frame(cur, """SELECT pb.household_id, h.phase::text AS phase, pb.priority_class, pb.max_current_a
                              FROM premise_backup pb JOIN household h ON h.id = pb.household_id
                              ORDER BY pb.household_id""")
    cur.close()
    blocks = {r.phase: BlockSpec(r.phase, float(r.capacity_kwh), float(r.power_kw), float(r.soc_min),
                                 float(r.soc_max), float(r.efficiency)) for r in blocks_df.itertuples()}
    return Registry(
        dt_id=str(nb["id"]), transformer_kva=float(nb["transformer_kva"]), nominal_v_ln=float(nb["nominal_v_ln"]),
        v_limit_pct=float(nb["v_limit_pct"]),
        feeder=Feeder(bus=bus, line=line, household=household, root=str(root.iloc[0])),
        sensors=sensors, blocks=blocks, unit_by_phase=dict(zip(blocks_df["phase"], blocks_df["unit_id"].astype(int))),
        premises=premises,
    )
