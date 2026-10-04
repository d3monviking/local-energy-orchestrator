"use client";

/**
 * components/map/FeederMap.tsx — deck.gl over a satellite basemap of the
 * real neighbourhood (Build Spec v1.0 §8.2). PathLayer-equivalent
 * (GeoJsonLayer handles LineStrings) for feeder lines, points for
 * households/sensors/the transformer/battery blocks. Voltage colouring
 * and the TripsLayer for animated power flow attach once a recorded
 * run exists for the timeline to scrub (tracked, not built here).
 *
 * No basemap API key needed: Esri World Imagery is one of the three
 * providers the Build Spec names as free-tier-sufficient (§9.1), served
 * as plain XYZ tiles via deck.gl's own TileLayer — no Mapbox/MapLibre
 * token required.
 */

import { useEffect, useMemo, useState } from "react";
import DeckGL from "@deck.gl/react";
import { TileLayer } from "@deck.gl/geo-layers";
import { BitmapLayer, GeoJsonLayer, ScatterplotLayer } from "@deck.gl/layers";
import type { PickingInfo } from "@deck.gl/core";

const CLOUD_API_URL =
  process.env.NEXT_PUBLIC_CLOUD_API_URL ?? "http://localhost:8030";

const ESRI_WORLD_IMAGERY =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";

type FeederProperties = {
  feature_type: "line" | "bus" | "transformer" | "household" | "sensor" | "battery_block";
  id: string;
  bus_id?: string;
  from_bus?: string;
  to_bus?: string;
  phase?: "R" | "Y" | "B";
  has_pv?: boolean;
  is_business?: boolean;
  is_critical?: boolean;
  placement?: "busbar" | "far_end";
};

type FeederCollection = {
  type: "FeatureCollection";
  properties: {
    dt_id: string; name: string; centroid: [number, number];
    nominal_v_ln: number; v_limit_pct: number;
  };
  features: GeoJSON.Feature<GeoJSON.Geometry, FeederProperties>[];
};

type NetworkResultRow = { bus_id: string; phase: string; voltage_v: number; loading_pct: number | null; violation: boolean };
type BusState = { voltage_v: number; violation: boolean };

const PHASE_COLOR: Record<string, [number, number, number]> = {
  // Phase identity, muted so it never reads as a voltage status.
  R: [201, 143, 139],
  Y: [201, 183, 127],
  B: [142, 169, 207],
};

const POINT_STYLE: Record<
  FeederProperties["feature_type"],
  { radius: number; color: [number, number, number] }
> = {
  line: { radius: 0, color: [255, 255, 255] },
  bus: { radius: 2, color: [147, 161, 176] },
  transformer: { radius: 9, color: [230, 237, 243] },
  household: { radius: 3.5, color: [47, 191, 113] },
  sensor: { radius: 5, color: [147, 161, 176] },
  battery_block: { radius: 8, color: [63, 198, 198] },
};

export type FeederMapProps = {
  dtId: string;
  /** Recorded run to colour voltage from, and the scrubbed timestamp within it. Omit either to show topology only (Day 2 behaviour). */
  runId?: string;
  ts?: string;
  nominalV?: number;
  vLimitPct?: number;
  /** bus_ids of households actually on backup power this run — drawn with a distinct ring (§8.2). */
  backupBusIds?: Set<string>;
  /** phase -> latest dispatch row at the scrubbed ts, for the battery_block tooltip. */
  liveDispatchByPhase?: Record<string, { actual_kw: number; soc_after: number; mode: string; rule_triggered: string | null }>;
  /** Count of buses in violation at the current ts, for the parent's banner. */
  onViolatingCount?: (n: number) => void;
  /** Show what the day-ahead forecast PREDICTED for this interval instead of what happened. */
  forecast?: boolean;
};

const FEATURE_LABEL: Record<FeederProperties["feature_type"], string> = {
  line: "Line", bus: "Pole", transformer: "Transformer", household: "Home", sensor: "Sensor", battery_block: "Battery",
};

function voltageColor(v: number, nominal: number, limitPct: number): [number, number, number] {
  const deviation = Math.abs(v - nominal) / nominal;
  if (deviation > limitPct / 100) return [224, 71, 62]; // red: violation
  if (deviation > (limitPct / 100) * 0.7) return [224, 167, 46]; // amber: approaching
  return [47, 191, 113]; // green: healthy
}

/** The transformer, all 3 battery blocks, and all 3 busbar sensors
 * share one lon/lat (the DT site — electrically accurate, confirmed in
 * api.py's own comment, but 7 markers stacked on one pixel means only
 * the topmost ever renders). Display-only fix: keep the transformer
 * anchored at the true point (lines terminate there) and arrange
 * everything else that shares its coordinate in a small ring around
 * it, so each is its own visible, hoverable marker. */
function fanOutTransformerSite(
  features: GeoJSON.Feature<GeoJSON.Geometry, FeederProperties>[]
): GeoJSON.Feature<GeoJSON.Geometry, FeederProperties>[] {
  const byCoord = new Map<string, GeoJSON.Feature<GeoJSON.Geometry, FeederProperties>[]>();
  for (const f of features) {
    const [lon, lat] = (f.geometry as GeoJSON.Point).coordinates;
    const key = `${lon.toFixed(7)},${lat.toFixed(7)}`;
    (byCoord.get(key) ?? byCoord.set(key, []).get(key)!).push(f);
  }
  const RING_DEG = 0.00005; // ~5.5m at this latitude — distinct markers, still visually "at the DT"
  const out: GeoJSON.Feature<GeoJSON.Geometry, FeederProperties>[] = [];
  for (const group of byCoord.values()) {
    if (group.length === 1) {
      out.push(group[0]);
      continue;
    }
    const transformer = group.find((f) => f.properties.feature_type === "transformer");
    const orbiting = group.filter((f) => f !== transformer);
    if (transformer) out.push(transformer);
    const [lon, lat] = (group[0].geometry as GeoJSON.Point).coordinates;
    orbiting.forEach((f, i) => {
      const angle = (2 * Math.PI * i) / orbiting.length - Math.PI / 2;
      out.push({
        ...f,
        geometry: { type: "Point", coordinates: [lon + RING_DEG * Math.cos(angle), lat + RING_DEG * Math.sin(angle)] },
      });
    });
  }
  return out;
}

export default function FeederMap({
  dtId,
  runId,
  ts,
  nominalV: nominalVProp,
  vLimitPct: vLimitPctProp,
  backupBusIds,
  liveDispatchByPhase,
  onViolatingCount,
  forecast = false,
}: FeederMapProps) {
  const [data, setData] = useState<FeederCollection | null>(null);
  // Read from the fetched neighbourhood data (DB's actual nominal_v_ln
  // is 250, not the 230 a prop default would silently assume) -
  // confirmed getting this wrong flips every violation's over/under
  // label, not just a cosmetic rounding difference. A caller-supplied
  // prop only matters before that fetch resolves.
  const nominalV = data?.properties.nominal_v_ln ?? nominalVProp ?? 230;
  const vLimitPct = data?.properties.v_limit_pct ?? vLimitPctProp ?? 6;
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<PickingInfo | null>(null);
  const [busState, setBusState] = useState<Record<string, BusState> | null>(null);
  const [deEnergised, setDeEnergised] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch(`${CLOUD_API_URL}/api/feeder/${dtId}`)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        return r.json();
      })
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, [dtId]);

  // Snapped to the same 15-minute grid the backend snaps `ts_end` to
  // (api.py's network_result_at), and used as the effect's own
  // dependency instead of the raw, continuously-changing `ts` prop.
  // During Play, ts changes every animation frame (~60/sec); re-firing
  // this effect that often means each fetch's cleanup cancels the
  // PREVIOUS one before its round trip (a full ORDER BY abs(epoch
  // diff) scan) can complete, so almost no response ever won the race
  // - confirmed the map's colours only ever updated on manual scrub,
  // where ts eventually stops changing long enough for one request to
  // land. Snapping first means this only actually re-fetches once per
  // recorded interval, not once per frame.
  const snappedTs = useMemo(() => {
    if (!ts) return undefined;
    const FIFTEEN_MIN_MS = 15 * 60 * 1000;
    const ms = Math.floor(new Date(ts).getTime() / FIFTEEN_MIN_MS) * FIFTEEN_MIN_MS;
    return new Date(ms).toISOString();
  }, [ts]);

  useEffect(() => {
    if (!runId || !snappedTs) {
      setBusState(null);
      return;
    }
    let cancelled = false;
    fetch(`${CLOUD_API_URL}/api/network_result/${runId}?ts_end=${encodeURIComponent(snappedTs)}&forecast=${forecast}`)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        return r.json();
      })
      .then((d: { results: NetworkResultRow[]; de_energised?: boolean }) => {
        if (cancelled) return;
        setDeEnergised(!!d.de_energised);
        // A bus carries one voltage per phase; the map shows the worst
        // (furthest from nominal) phase at that bus, since that's the
        // one that would actually trip a protective device there.
        const byBus: Record<string, BusState> = {};
        for (const row of d.results) {
          const prev = byBus[row.bus_id];
          if (!prev || Math.abs(row.voltage_v - nominalV) > Math.abs(prev.voltage_v - nominalV)) {
            byBus[row.bus_id] = { voltage_v: row.voltage_v, violation: row.violation };
          }
        }
        setBusState(byBus);
        onViolatingCount?.(Object.values(byBus).filter((b) => b.violation).length);
      })
      .catch(() => !cancelled && setBusState(null));
    return () => {
      cancelled = true;
    };
  }, [runId, snappedTs, nominalV, forecast]);

  const initialViewState = useMemo(() => {
    const [lon, lat] = data?.properties.centroid ?? [77.7942, 13.07];
    return { longitude: lon, latitude: lat, zoom: 16.5, pitch: 0, bearing: 0 };
  }, [data]);

  const layers = useMemo(() => {
    const tileLayer = new TileLayer({
      id: "satellite-basemap",
      data: ESRI_WORLD_IMAGERY,
      maxZoom: 19,
      minZoom: 0,
      tileSize: 256,
      renderSubLayers: (props) => {
        const { west, south, east, north } = props.tile.bbox as {
          west: number; south: number; east: number; north: number;
        };
        return new BitmapLayer(props, {
          data: undefined,
          image: props.data,
          bounds: [west, south, east, north],
        });
      },
    });

    if (!data) return [tileLayer];

    const lineFeatures = data.features.filter((f) => f.geometry.type === "LineString");
    const pointFeatures = fanOutTransformerSite(data.features.filter((f) => f.geometry.type === "Point"));

    const feederLines = new GeoJsonLayer({
      id: "feeder-lines",
      data: { type: "FeatureCollection", features: lineFeatures } as GeoJSON.FeatureCollection,
      getLineColor: (f: GeoJSON.Feature<GeoJSON.Geometry, FeederProperties>) => {
        if (!busState) return [93, 110, 125];
        // Colour by the worse of the two endpoint buses' voltage.
        const from = f.properties.from_bus ? busState[f.properties.from_bus] : undefined;
        const to = f.properties.to_bus ? busState[f.properties.to_bus] : undefined;
        const worse = [from, to]
          .filter((s): s is BusState => !!s)
          .sort((a, b) => Math.abs(b.voltage_v - nominalV) - Math.abs(a.voltage_v - nominalV))[0];
        return worse ? voltageColor(worse.voltage_v, nominalV, vLimitPct) : [93, 110, 125];
      },
      getLineWidth: (f: GeoJSON.Feature<GeoJSON.Geometry, FeederProperties>) => {
        const from = f.properties.from_bus ? busState?.[f.properties.from_bus] : undefined;
        const to = f.properties.to_bus ? busState?.[f.properties.to_bus] : undefined;
        // Violating lines get a visibly bolder stroke, not just a color
        // change - a thin red line reads the same as a thin green one
        // at a glance; a thick one doesn't.
        return from?.violation || to?.violation ? 3.5 : 1.2;
      },
      lineWidthMinPixels: 1,
      lineWidthUpdateTriggers: { getLineWidth: [busState] },
      pickable: true,
    });

    const points = new ScatterplotLayer<GeoJSON.Feature<GeoJSON.Geometry, FeederProperties>>({
      id: "feeder-points",
      data: pointFeatures,
      getPosition: (f) => (f.geometry as GeoJSON.Point).coordinates as [number, number],
      getRadius: (f) => POINT_STYLE[f.properties.feature_type].radius,
      getFillColor: (f) => {
        const vType = f.properties.feature_type;
        const busId = f.properties.bus_id ?? f.properties.id;
        if ((vType === "household" || vType === "bus") && busState?.[busId] != null) {
          return voltageColor(busState[busId].voltage_v, nominalV, vLimitPct);
        }
        if (vType === "household" && f.properties.phase) {
          return PHASE_COLOR[f.properties.phase] ?? POINT_STYLE.household.color;
        }
        return POINT_STYLE[vType].color;
      },
      getLineColor: (f) => {
        const onBackup = backupBusIds?.has(f.properties.bus_id ?? f.properties.id);
        if (onBackup) return [63, 198, 198]; // battery-coloured ring: on the backup circuit
        return f.properties.is_critical ? [230, 237, 243] : [0, 0, 0, 0];
      },
      getLineWidth: (f) => (backupBusIds?.has(f.properties.bus_id ?? f.properties.id) ? 2.5 : 1),
      lineWidthMinPixels: 1,
      stroked: true,
      radiusUnits: "pixels",
      pickable: true,
    });

    return [tileLayer, feederLines, points];
  }, [data, busState, nominalV, vLimitPct, backupBusIds]);

  return (
    <div className="relative w-full h-full rounded-lg overflow-hidden border border-[var(--leo-border)]">
      <DeckGL
        initialViewState={initialViewState}
        controller={true}
        layers={layers}
        onHover={(info) => setHover(info.object ? info : null)}
        getTooltip={undefined}
      />

      {error && (
        <div role="alert" className="absolute top-2 left-2 rounded-md bg-[var(--leo-bad)] text-black text-[13px] px-2 py-1">
          Map data could not be loaded ({error})
        </div>
      )}

      {deEnergised && (
        <div role="status" className="absolute bottom-8 left-2 max-w-xs rounded-md bg-black/80 px-3 py-2 text-[13px]">
          <p className="font-semibold">Feeder de-energised</p>
          <p className="text-[var(--leo-text-dim)]">No grid supply. Homes ringed in teal are on LEO&apos;s backup circuit.</p>
        </div>
      )}

      <details open className="group absolute top-2 right-2 rounded-md bg-black/75 text-xs leading-snug">
        <summary className="cursor-pointer select-none px-2.5 py-1.5 text-[var(--leo-text-dim)] hover:text-[var(--leo-text)]">
          Legend
        </summary>
        <div className="flex flex-col gap-1 px-2.5 pb-2">
        <span className="text-[var(--leo-text-dim)]">Voltage at each point</span>
        <span className="flex items-center gap-2">
          <span aria-hidden className="inline-block h-[3px] w-4 rounded" style={{ background: "rgb(47,191,113)" }} />
          healthy
        </span>
        <span className="flex items-center gap-2">
          <span aria-hidden className="inline-block h-[3px] w-4 rounded" style={{ background: "rgb(224,167,46)" }} />
          near the limit
        </span>
        <span className="flex items-center gap-2">
          <span aria-hidden className="inline-block h-[5px] w-4 rounded" style={{ background: "rgb(224,71,62)" }} />
          outside ±{vLimitPct}% of {nominalV} V (thick line)
        </span>
        <span className="flex items-center gap-2">
          <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-full border-2" style={{ borderColor: "rgb(63,198,198)" }} />
          on backup power
        </span>
        <span className="flex items-center gap-2">
          <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: "rgb(230,237,243)" }} />
          transformer
        </span>
        <span className="flex items-center gap-2">
          <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: "rgb(63,198,198)" }} />
          battery (one per phase)
        </span>
        <span className="flex items-center gap-2">
          <span aria-hidden className="inline-block h-2 w-2 rounded-full" style={{ background: "rgb(147,161,176)" }} />
          sensor
        </span>
        </div>
      </details>

      {data && (
        <div className="absolute bottom-1.5 left-2 rounded bg-black/60 px-1.5 py-0.5 text-[11px] text-[var(--leo-text-dim)]">
          {data.properties.name} · {data.properties.dt_id} · streets © OpenStreetMap · imagery © Esri
        </div>
      )}

      {hover?.object && (() => {
        const props = hover.object.properties as FeederProperties;
        const busId = props.bus_id ?? props.id;
        const vState = busState?.[busId];
        const dispatchState = props.feature_type === "battery_block" && props.phase
          ? liveDispatchByPhase?.[props.phase]
          : undefined;
        return (
          <div
            className="absolute rounded-md bg-black/80 text-xs px-2 py-1 pointer-events-none flex flex-col gap-0.5"
            style={{ left: hover.x + 12, top: hover.y + 12 }}
          >
            <span>
              {FEATURE_LABEL[props.feature_type]} {props.id}
              {props.phase && ` · phase ${props.phase}`}
            </span>
            {vState && (
              <span className={vState.violation ? "text-[var(--leo-bad)]" : "text-[var(--leo-ok)]"}>
                {vState.voltage_v.toFixed(1)} V{vState.violation ? " (outside limits)" : ""}
              </span>
            )}
            {dispatchState && (
              <span className="text-[var(--leo-text-dim)]">
                {dispatchState.mode === "backup" ? "feeding the backup circuit" : Math.abs(dispatchState.actual_kw) < 0.05 ? "idle" : `${dispatchState.actual_kw > 0 ? "discharging" : "charging"} ${Math.abs(dispatchState.actual_kw).toFixed(1)} kW`} ·
                {" "}{(dispatchState.soc_after * 100).toFixed(0)}% charged
              </span>
            )}
            {props.is_critical && <span>Critical premise (kept powered in outages)</span>}
          </div>
        );
      })()}
    </div>
  );
}
