"""world/feeder.py — generates the LV network from OpenStreetMap street geometry.

Owner A. See Build Specification v1.0 §6 and System Architecture v3.0 §C6.

Procedure (Build Spec §6.1):
  1. Fix the neighbourhood bounding box (scenario.yaml).
  2. Pull the drivable street graph and building footprints from OSM.
  3. Place the transformer at a road junction near the load centroid.
  4. Topology = minimum spanning tree of the road graph, rooted at the
     transformer.
  5. Attach each household to its nearest road segment via a short
     service drop, splitting the segment at the projection point.
  6. Assign phases round-robin along the feeder, then perturb for
     deliberate imbalance.
  7. Size conductors by downstream connected load.
  8. R/X per km: pandapower's LV overhead standard types stand in for the
     IS 14255 / manufacturer ABC datasheet figures, per §6.2, until those
     are sourced. (verify)
  9. Transformer: 100 kVA, 11/0.433 kV, ~4.5% impedance. (verify)
 10. Six sensors: three at the LV busbar (one per phase), three at the
     far end of the longest run on each phase.

Topology and geometry come from one source (OSM), so electrical length and
drawn length always agree — see §6 for why this replaces rescaling a
borrowed feeder template.

Out of scope here: zero-sequence line parameters and the `runpp_3ph`
validation against the IEEE European LV Test Feeder. Those are Day 2,
in gateway/network_model.py.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
import shapely.geometry as sgeom
import yaml

MODULE_DIR = Path(__file__).parent
DEFAULT_OSM_PATH = MODULE_DIR / "data" / "hoskote_raw.osm"
DEFAULT_SCENARIO_PATH = MODULE_DIR.parent / "scenario.yaml"

PHASES = ["R", "Y", "B"]

# Conductor library. pandapower's LV overhead standard types stand in for
# IS 14255 / a manufacturer ABC datasheet, per Build Spec §6.2 — this is an
# explicitly sanctioned temporary substitute, not a fabricated figure.
# (verify before external citation — see Build Spec §9.3)
CONDUCTOR_LIBRARY = {
    "3x95+70": {  # main run
        "r_ohm_per_km": 0.306, "x_ohm_per_km": 0.290, "ampacity_a": 350.0,
        "pp_std_type": "94-AL1/15-ST1A 0.4",
    },
    "3x50+35": {  # branch
        "r_ohm_per_km": 0.594, "x_ohm_per_km": 0.300, "ampacity_a": 210.0,
        "pp_std_type": "48-AL1/8-ST1A 0.4",
    },
    "16": {  # service drop
        "r_ohm_per_km": 1.877, "x_ohm_per_km": 0.350, "ampacity_a": 105.0,
        "pp_std_type": "15-AL1/3-ST1A 0.4",
    },
}

# A backbone edge carrying at least this share of total connected household
# load is sized as the main run; everything else on the backbone is a branch.
MAIN_RUN_LOAD_FRACTION = 0.5

# A projected attachment point within this many metres of an existing node
# reuses that node rather than inserting a degenerate near-zero-length pole.
MIN_POLE_OFFSET_M = 2.0

METRES_PER_DEGREE_LAT = 111_320.0


def _metres_per_degree_lon(lat_deg: float) -> float:
    return METRES_PER_DEGREE_LAT * np.cos(np.radians(lat_deg))


def load_scenario(path: Path = DEFAULT_SCENARIO_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_osm(osm_path: Path = DEFAULT_OSM_PATH) -> tuple[nx.Graph, "pd.DataFrame"]:
    """Load the drivable street graph and building footprints from a cached
    OSM XML extract (see world/data/hoskote_raw.osm — a real bbox in
    Hoskote, Bengaluru Rural district, downloaded via the OSM API).

    Returns the largest connected component of the street graph as an
    undirected simple graph with a `length` (m) attribute per edge, and a
    GeoDataFrame of building footprint polygons.
    """
    multidigraph = ox.graph_from_xml(str(osm_path), simplify=True, retain_all=True)
    multigraph = ox.convert.to_undirected(multidigraph)

    # Collapse parallel edges to the shortest one -> simple graph for MST.
    graph = nx.Graph()
    graph.add_nodes_from(multigraph.nodes(data=True))
    for u, v, data in multigraph.edges(data=True):
        length = data["length"]
        if graph.has_edge(u, v):
            if length < graph.edges[u, v]["length"]:
                graph.edges[u, v].update(length=length, geometry=data.get("geometry"))
        else:
            graph.add_edge(u, v, length=length, geometry=data.get("geometry"))

    largest_cc = max(nx.connected_components(graph), key=len)
    graph = graph.subgraph(largest_cc).copy()

    buildings = ox.features_from_xml(str(osm_path), tags={"building": True})
    buildings = buildings[buildings.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    return graph, buildings


def _node_point(graph: nx.Graph, node) -> sgeom.Point:
    data = graph.nodes[node]
    return sgeom.Point(data["x"], data["y"])


def _edge_geometry(graph: nx.Graph, u, v) -> sgeom.LineString:
    data = graph.edges[u, v]
    geom = data.get("geometry")
    if geom is not None:
        return geom
    return sgeom.LineString([_node_point(graph, u), _node_point(graph, v)])


def _centroids(geoms) -> list[sgeom.Point]:
    # Plain shapely per-geometry, not geopandas' vectorised .centroid, so
    # this doesn't trip the "geographic CRS" warning at neighbourhood scale
    # (a few hundred metres) where the distortion is negligible.
    return [g.centroid for g in geoms]


def pick_transformer_node(graph: nx.Graph, buildings: "pd.DataFrame"):
    """Place the transformer at a road junction (degree >= 2) nearest the
    load centroid, per §6.1 step 3."""
    pts = _centroids(buildings.geometry)
    centroid = sgeom.Point(np.mean([p.x for p in pts]), np.mean([p.y for p in pts]))
    junctions = [n for n in graph.nodes if graph.degree(n) >= 2] or list(graph.nodes)
    return min(junctions, key=lambda n: _node_point(graph, n).distance(centroid))


def build_mst(graph: nx.Graph, root) -> nx.Graph:
    """MST of the road graph, per §6.1 step 4. LV feeders are radial and
    follow roads, so the MST is a plausible network."""
    tree = nx.minimum_spanning_tree(graph, weight="length")
    assert nx.is_connected(tree)
    assert root in tree.nodes
    return tree


@dataclass
class HouseholdAttachment:
    household_bus_id: str
    parent_bus_id: str
    service_drop_length_m: float


def attach_households(
    tree: nx.Graph,
    buildings: "pd.DataFrame",
    n_households: int,
    rng: random.Random,
) -> tuple[nx.Graph, list[HouseholdAttachment]]:
    """Attach `n_households` buildings to their nearest tree edge via a
    short service drop, per §6.1 step 5.

    The tree edge is split at the projection point (a "pole"), unless the
    projection lands within MIN_POLE_OFFSET_M of an existing node, in
    which case the existing node is reused. Splitting a tree edge keeps
    the graph a tree.
    """
    if len(buildings) < n_households:
        raise ValueError(
            f"OSM extract has only {len(buildings)} buildings, need {n_households}"
        )
    chosen = buildings.sample(n=n_households, random_state=rng.randint(0, 2**31))
    centroids = _centroids(chosen.geometry)

    lat0 = np.mean([_node_point(tree, n).y for n in tree.nodes])
    m_per_lon = _metres_per_degree_lon(lat0)

    def to_metres(dx_deg: float, dy_deg: float) -> tuple[float, float]:
        return dx_deg * m_per_lon, dy_deg * METRES_PER_DEGREE_LAT

    attachments: list[HouseholdAttachment] = []
    pole_counter = 0

    for i, (_, point) in enumerate(zip(chosen.index, centroids)):
        # Nearest edge by planar distance in metres (small area -> flat-earth
        # approximation is fine at this scale).
        best = None
        for u, v, edata in tree.edges(data=True):
            if edata.get("is_service_drop"):
                continue  # attach to the road network, not to another household's drop
            edge_geom = _edge_geometry(tree, u, v)
            dx, dy = to_metres(point.x - edge_geom.centroid.x, point.y - edge_geom.centroid.y)
            coarse_d = (dx**2 + dy**2) ** 0.5
            if best is not None and coarse_d > best[0] * 3 + 200:
                continue  # cheap prune before the exact projection below
            proj_frac = edge_geom.project(point, normalized=True)
            proj_pt = edge_geom.interpolate(proj_frac, normalized=True)
            dx, dy = to_metres(point.x - proj_pt.x, point.y - proj_pt.y)
            d = (dx**2 + dy**2) ** 0.5
            if best is None or d < best[0]:
                best = (d, u, v, proj_pt)

        dist_m, u, v, proj_pt = best

        # Decide attachment node: reuse an endpoint if the projection is
        # essentially at it, else split the edge with a new pole bus.
        du = to_metres(proj_pt.x - _node_point(tree, u).x, proj_pt.y - _node_point(tree, u).y)
        dv = to_metres(proj_pt.x - _node_point(tree, v).x, proj_pt.y - _node_point(tree, v).y)
        if (du[0] ** 2 + du[1] ** 2) ** 0.5 < MIN_POLE_OFFSET_M:
            attach_node = u
        elif (dv[0] ** 2 + dv[1] ** 2) ** 0.5 < MIN_POLE_OFFSET_M:
            attach_node = v
        else:
            pole_counter += 1
            pole_id = f"POLE-{pole_counter}"
            edge_geom = _edge_geometry(tree, u, v)
            full_length = edge_geom.length
            len_u = edge_geom.project(proj_pt)
            tree.add_node(pole_id, x=proj_pt.x, y=proj_pt.y)
            tree.remove_edge(u, v)
            frac_u = len_u / full_length if full_length > 0 else 0.5
            tree.add_edge(u, pole_id, length=full_length * frac_u)
            tree.add_edge(pole_id, v, length=full_length * (1 - frac_u))
            attach_node = pole_id

        hh_bus_id = f"HH-BUS-{i:03d}"
        tree.add_node(hh_bus_id, x=point.x, y=point.y)
        tree.add_edge(attach_node, hh_bus_id, length=max(dist_m, 1.0), is_service_drop=True)
        attachments.append(HouseholdAttachment(hh_bus_id, attach_node, max(dist_m, 1.0)))

    return tree, attachments


def assign_phases(
    household_ids: list[str], rng: random.Random, imbalance_frac: float = 0.2
) -> dict[str, str]:
    """Round-robin phase assignment along the feeder, then perturb a
    fraction of households onto a skewed distribution for deliberate
    imbalance — per §6.1 step 6. Imbalance is the phenomenon being
    modelled, not an artefact to avoid."""
    phases = {hid: PHASES[i % 3] for i, hid in enumerate(household_ids)}
    n_perturb = int(round(len(household_ids) * imbalance_frac))
    skew_weights = [0.5, 0.3, 0.2]  # R made deliberately heavier
    for hid in rng.sample(household_ids, n_perturb):
        phases[hid] = rng.choices(PHASES, weights=skew_weights, k=1)[0]
    return phases


def classify_conductors(tree: nx.Graph, root, household_load_kw: dict[str, float]) -> dict:
    """Size conductors by downstream connected load, per §6.1 step 7.
    Service drops (household leaf edges) are always '16'; backbone edges
    are 'main run' or 'branch' depending on the share of total load they
    carry downstream of the transformer."""
    total_kw = sum(household_load_kw.values())
    downstream_kw: dict[tuple, float] = {}

    def dfs(node, parent):
        load = household_load_kw.get(node, 0.0)
        for nbr in tree.neighbors(node):
            if nbr == parent:
                continue
            child_load = dfs(nbr, node)
            downstream_kw[frozenset((node, nbr))] = child_load
            load += child_load
        return load

    dfs(root, None)

    conductor_type = {}
    for u, v, data in tree.edges(data=True):
        if data.get("is_service_drop"):
            conductor_type[frozenset((u, v))] = "16"
            continue
        share = downstream_kw.get(frozenset((u, v)), 0.0) / total_kw if total_kw else 0.0
        conductor_type[frozenset((u, v))] = (
            "3x95+70" if share >= MAIN_RUN_LOAD_FRACTION else "3x50+35"
        )
    return conductor_type


def _dev_eui(seed_str: str) -> str:
    return hashlib.sha1(seed_str.encode()).hexdigest()[:16]


def place_sensors(tree: nx.Graph, root, phase_of: dict[str, str]) -> "pd.DataFrame":
    """Three busbar sensors (one per phase, at the transformer) and three
    far-end sensors (longest run on each phase), per §6.1 step 10."""
    lengths = nx.single_source_dijkstra_path_length(tree, root, weight="length")
    rows = []
    for phase in PHASES:
        rows.append({
            "id": f"SEN-BUSBAR-{phase}",
            "bus_id": root,
            "phase": phase,
            "placement": "busbar",
            "dev_eui": _dev_eui(f"busbar-{phase}"),
        })
        phase_households = [hid for hid, p in phase_of.items() if p == phase]
        if phase_households:
            far_end = max(phase_households, key=lambda hid: lengths.get(hid, 0.0))
            rows.append({
                "id": f"SEN-FAREND-{phase}",
                "bus_id": far_end,
                "phase": phase,
                "placement": "far_end",
                "dev_eui": _dev_eui(f"farend-{phase}"),
            })
    return pd.DataFrame(rows)


def _sanctioned_load_kw(is_business: bool, rng: random.Random) -> float:
    if is_business:
        return round(rng.uniform(3.0, 8.0), 2)
    return round(rng.uniform(1.0, 3.0), 2)


def build_household_registry(
    attachments: list[HouseholdAttachment],
    shares: dict,
    rng: random.Random,
) -> "pd.DataFrame":
    """Static registry attributes for the `household` table. Truth-side
    attributes (persona, PV tilt/azimuth/soiling, appliances) belong to
    world/households.py, not here."""
    rows = []
    n = len(attachments)
    n_business = round(n * shares.get("is_business", 0.1))
    n_pv = round(n * shares.get("has_pv", 0.25))
    business_idx = set(rng.sample(range(n), n_business))
    pv_idx = set(rng.sample(range(n), n_pv))
    critical_idx = rng.sample(sorted(business_idx), min(1, len(business_idx))) if business_idx else []

    for i, att in enumerate(attachments):
        is_business = i in business_idx
        has_pv = i in pv_idx
        rows.append({
            "id": f"HH-{i:03d}",
            "bus_id": att.household_bus_id,
            "sanctioned_load_kw": _sanctioned_load_kw(is_business, rng),
            "has_pv": has_pv,
            "pv_kwp": round(rng.uniform(1.0, 3.0), 2) if has_pv else None,
            "is_business": is_business,
            "is_critical": i in critical_idx,
            "critical_class": "health" if i in critical_idx else "none",
            "enrolled_at": None,
        })
    return pd.DataFrame(rows)


def build_bus_line_tables(
    tree: nx.Graph, root, neighbourhood_id: str, conductor_type: dict
) -> tuple["pd.DataFrame", "pd.DataFrame"]:
    parent = {root: None}
    order = list(nx.bfs_tree(tree, root))
    for node in order:
        for nbr in tree.neighbors(node):
            if nbr not in parent:
                parent[nbr] = node

    bus_rows = []
    for node in tree.nodes:
        data = tree.nodes[node]
        bus_rows.append({
            "id": str(node),
            "neighbourhood_id": neighbourhood_id,
            "lat": data["y"],
            "lon": data["x"],
            "is_transformer": node == root,
            "parent_bus_id": str(parent[node]) if parent.get(node) is not None else None,
        })
    bus_df = pd.DataFrame(bus_rows)

    line_rows = []
    for u, v, data in tree.edges(data=True):
        ctype = conductor_type[frozenset((u, v))]
        lib = CONDUCTOR_LIBRARY[ctype]
        # from_bus is always the parent side, to_bus the child side.
        from_bus, to_bus = (u, v) if parent.get(v) == u else (v, u)
        prefix = "SVC" if data.get("is_service_drop") else "LN"
        line_rows.append({
            "id": f"{prefix}-{from_bus}-{to_bus}",
            "from_bus": str(from_bus),
            "to_bus": str(to_bus),
            "length_m": data["length"],
            "conductor_type": ctype,
            "r_ohm_per_km": lib["r_ohm_per_km"],
            "x_ohm_per_km": lib["x_ohm_per_km"],
            "ampacity_a": lib["ampacity_a"],
        })
    line_df = pd.DataFrame(line_rows)
    return bus_df, line_df


@dataclass
class FeederResult:
    neighbourhood: dict
    bus: "pd.DataFrame"
    line: "pd.DataFrame"
    household: "pd.DataFrame"
    sensor: "pd.DataFrame"
    tree: nx.Graph
    root: str


def build_feeder(
    scenario: Optional[dict] = None, osm_path: Path = DEFAULT_OSM_PATH
) -> FeederResult:
    scenario = scenario or load_scenario()
    seed = scenario["sim"]["seed"]
    rng = random.Random(seed)
    n_households = scenario["households"]["count"]

    graph, buildings = load_osm(osm_path)
    root = pick_transformer_node(graph, buildings)
    tree = build_mst(graph, root)
    tree, attachments = attach_households(tree, buildings, n_households, rng)

    household_ids = [a.household_bus_id for a in attachments]
    phase_of = assign_phases(household_ids, rng)

    household_df = build_household_registry(attachments, scenario["households"]["shares"], rng)
    load_by_bus = dict(zip(household_df["bus_id"], household_df["sanctioned_load_kw"]))
    conductor_type = classify_conductors(tree, root, load_by_bus)

    neighbourhood = scenario["neighbourhood"]
    bus_df, line_df = build_bus_line_tables(tree, root, neighbourhood["dt_id"], conductor_type)

    household_df = household_df.merge(
        pd.Series(phase_of, name="phase").rename_axis("bus_id").reset_index(),
        on="bus_id",
    )

    sensor_df = place_sensors(tree, root, phase_of)

    return FeederResult(
        neighbourhood=neighbourhood,
        bus=bus_df,
        line=line_df,
        household=household_df,
        sensor=sensor_df,
        tree=tree,
        root=root,
    )


def to_geojson(result: FeederResult) -> dict:
    """A FeatureCollection of bus points and line segments for the map
    component (§8.2), carrying the provenance statement (§6.4)."""
    features = []
    for _, row in result.bus.iterrows():
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
            "properties": {"kind": "bus", "id": row["id"], "is_transformer": bool(row["is_transformer"])},
        })
    bus_xy = result.bus.set_index("id")[["lon", "lat"]]
    for _, row in result.line.iterrows():
        p1 = bus_xy.loc[row["from_bus"]]
        p2 = bus_xy.loc[row["to_bus"]]
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[p1["lon"], p1["lat"]], [p2["lon"], p2["lat"]]]},
            "properties": {"kind": "line", "id": row["id"], "conductor_type": row["conductor_type"]},
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "provenance": (
            "Feeder topology generated from OpenStreetMap street geometry for "
            f"{result.neighbourhood['name']}; conductor parameters stand in from "
            "pandapower's LV overhead library pending IS 14255 / manufacturer ABC "
            "datasheet figures; three-phase power-flow implementation to be "
            "validated against the IEEE European LV Test Feeder reference "
            "solution. Topology is illustrative — real LV feeders follow "
            "historical build-out rather than the minimum spanning tree of the "
            "road graph. Deployment uses DISCOM GIS and consumer indexing."
        ),
    }


if __name__ == "__main__":
    result = build_feeder()
    out_dir = MODULE_DIR / "data" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)

    result.bus.to_csv(out_dir / "bus.csv", index=False)
    result.line.to_csv(out_dir / "line.csv", index=False)
    result.household.to_csv(out_dir / "household.csv", index=False)
    result.sensor.to_csv(out_dir / "sensor.csv", index=False)

    import json
    with open(out_dir / "feeder.geojson", "w") as f:
        json.dump(to_geojson(result), f, indent=2)

    print(f"buses: {len(result.bus)}  lines: {len(result.line)}  households: {len(result.household)}")
    print(f"conductor mix:\n{result.line['conductor_type'].value_counts()}")
    print(f"phase mix:\n{result.household['phase'].value_counts()}")
    print(f"transformer bus: {result.root}")
    print(f"wrote {out_dir}")
