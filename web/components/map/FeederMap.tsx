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

import { useEffect, useMemo, useRef, useState } from "react";
import DeckGL from "@deck.gl/react";
import { TileLayer } from "@deck.gl/geo-layers";
import { BitmapLayer, GeoJsonLayer, IconLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { batteryIcon, criticalIcon, sensorIcon, transformerIcon } from "./icons";
import { WebMercatorViewport, type PickingInfo } from "@deck.gl/core";

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

type Feature = GeoJSON.Feature<GeoJSON.Geometry, FeederProperties>;

/** Where each infrastructure label sits, in screen pixels from its true point.
 *  The transformer, batteries and busbar sensors all share the DT's coordinate,
 *  so they are drawn as a labelled cluster around it instead of stacked dots. */
const SITE_OFFSET: Record<string, [number, number]> = {
  transformer: [0, 0],
  "BATT-R": [42, -36], "BATT-Y": [42, 0], "BATT-B": [42, 36],
  busbar: [-40, 0],
};

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
  const [trafoPct, setTrafoPct] = useState<number | null>(null);

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
        // Transformer loading at this interval: the worst phase, as % of rating.
        const loads = d.results.map((r) => r.loading_pct).filter((v): v is number => v != null && !Number.isNaN(v));
        setTrafoPct(loads.length ? Math.max(...loads) : null);
        onViolatingCount?.(Object.values(byBus).filter((b) => b.violation).length);
      })
      .catch(() => !cancelled && setBusState(null));
    return () => {
      cancelled = true;
    };
  }, [runId, snappedTs, nominalV, forecast]);

  // Fit the whole feeder (and its far-end sensors) into whatever size the map has.
  const boxRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setSize((prev) => prev ?? { w: e.contentRect.width, h: e.contentRect.height }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const initialViewState = useMemo(() => {
    const [lon, lat] = data?.properties.centroid ?? [77.7942, 13.07];
    const fallback = { longitude: lon, latitude: lat, zoom: 16.5, pitch: 0, bearing: 0 };
    if (!data || !size || size.w < 50 || size.h < 50) return fallback;
    const coords: number[][] = [];
    for (const f of data.features) {
      if (f.geometry.type === "LineString") coords.push(...(f.geometry as GeoJSON.LineString).coordinates);
      else if (f.geometry.type === "Point") coords.push((f.geometry as GeoJSON.Point).coordinates);
    }
    const lons = coords.map((c) => c[0]), lats = coords.map((c) => c[1]);
    const vp = new WebMercatorViewport({ width: size.w, height: size.h }).fitBounds(
      [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
      { padding: { top: 44, bottom: 36, left: 24, right: 24 } },
    );
    return { longitude: vp.longitude, latitude: vp.latitude, zoom: Math.min(vp.zoom, 18), pitch: 0, bearing: 0 };
  }, [data, size]);

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
        // Dimmed and desaturated so the network and markers read clearly on top.
        return new BitmapLayer(props, {
          data: undefined,
          image: props.data,
          bounds: [west, south, east, north],
          desaturate: 0.45,
          tintColor: [150, 150, 150],
        });
      },
    });

    if (!data) return [tileLayer];

    const lineFeatures = data.features.filter((f) => f.geometry.type === "LineString");
    const pointFeatures = data.features.filter((f) => f.geometry.type === "Point");
    const homes = pointFeatures.filter((f) => f.properties.feature_type === "household" || f.properties.feature_type === "bus");
    const transformer = pointFeatures.find((f) => f.properties.feature_type === "transformer");
    const batteries = pointFeatures.filter((f) => f.properties.feature_type === "battery_block");
    const busbar = pointFeatures.filter((f) => f.properties.feature_type === "sensor" && f.properties.placement === "busbar");
    const farEnd = pointFeatures.filter((f) => f.properties.feature_type === "sensor" && f.properties.placement === "far_end");
    const pos = (f: Feature) => (f.geometry as GeoJSON.Point).coordinates as [number, number];

    const lineColor = (f: Feature): [number, number, number] => {
      if (!busState) return [150, 162, 175];
      // Colour by the worse of the two endpoint buses' voltage.
      const from = f.properties.from_bus ? busState[f.properties.from_bus] : undefined;
      const to = f.properties.to_bus ? busState[f.properties.to_bus] : undefined;
      const worse = [from, to]
        .filter((st): st is BusState => !!st)
        .sort((a, b) => Math.abs(b.voltage_v - nominalV) - Math.abs(a.voltage_v - nominalV))[0];
      return worse ? voltageColor(worse.voltage_v, nominalV, vLimitPct) : [150, 162, 175];
    };

    // A dark casing under every line keeps it legible on bright rooftops.
    const lineCasing = new GeoJsonLayer({
      id: "feeder-lines-casing",
      data: { type: "FeatureCollection", features: lineFeatures } as GeoJSON.FeatureCollection,
      getLineColor: [5, 8, 12, 200],
      getLineWidth: 4,
      lineWidthUnits: "pixels",
    });
    const feederLines = new GeoJsonLayer({
      id: "feeder-lines",
      data: { type: "FeatureCollection", features: lineFeatures } as GeoJSON.FeatureCollection,
      getLineColor: (f: Feature) => lineColor(f),
      getLineWidth: 2,
      lineWidthUnits: "pixels",
      updateTriggers: { getLineColor: [busState] },
      pickable: true,
    });

    const points = new ScatterplotLayer<Feature>({
      id: "homes",
      data: homes,
      getPosition: pos,
      getRadius: (f) => (f.properties.feature_type === "bus" ? 2 : 3.5),
      getFillColor: (f) => {
        const busId = f.properties.bus_id ?? f.properties.id;
        if (busState?.[busId] != null) return voltageColor(busState[busId].voltage_v, nominalV, vLimitPct);
        if (f.properties.feature_type === "household" && f.properties.phase) return PHASE_COLOR[f.properties.phase];
        return [150, 162, 175];
      },
      getLineColor: (f) => {
        if (backupBusIds?.has(f.properties.bus_id ?? f.properties.id)) return [63, 198, 198];
        return [5, 8, 12, 200];
      },
      getLineWidth: (f) => (backupBusIds?.has(f.properties.bus_id ?? f.properties.id) ? 2 : 1),
      lineWidthUnits: "pixels",
      stroked: true,
      radiusUnits: "pixels",
      updateTriggers: { getFillColor: [busState], getLineColor: [backupBusIds], getRadius: [backupBusIds], getLineWidth: [backupBusIds] },
      pickable: true,
    });

    // Equipment as icons. The transformer, batteries and busbar sensors share the
    // DT's coordinate, so they sit as a small cluster around it, offset in pixels.
    type Marker = { f: Feature; url: string; id: string; size: number; offset: [number, number]; pin?: boolean };
    const markers: Marker[] = [];
    if (transformer) {
      for (const b of batteries) {
        const ph = b.properties.phase ?? "";
        const d = ph ? liveDispatchByPhase?.[ph] : undefined;
        const soc = d ? Math.round(d.soc_after * 10) / 10 : null;
        markers.push({ f: { ...b, geometry: transformer.geometry }, url: batteryIcon(ph, soc), id: `batt-${ph}-${soc}`, size: 36, offset: SITE_OFFSET[b.properties.id] ?? [42, 0] });
      }
      if (busbar.length) markers.push({ f: { ...busbar[0], geometry: transformer.geometry }, url: sensorIcon(), id: "sensor", size: 30, offset: SITE_OFFSET.busbar });
      markers.push({ f: transformer, url: transformerIcon(), id: "transformer", size: 40, offset: SITE_OFFSET.transformer });
    }
    for (const f of farEnd) markers.push({ f, url: sensorIcon(f.properties.phase), id: `sensor-${f.properties.phase}`, size: 30, offset: [0, 0] });
    for (const f of homes.filter((h) => h.properties.is_critical)) {
      const onBackup = !!backupBusIds?.has(f.properties.bus_id ?? f.properties.id);
      markers.push({ f, url: criticalIcon(onBackup), id: `critical-${onBackup}`, size: 32, offset: [0, -3], pin: true });
    }
    const equipment = new IconLayer<Marker>({
      id: "equipment-icons",
      data: markers,
      getPosition: (m) => pos(m.f),
      getIcon: (m) => ({ url: m.url, id: m.id, width: 48, height: 48, anchorY: m.pin ? 48 : 24 }),
      getSize: (m) => m.size,
      sizeUnits: "pixels",
      getPixelOffset: (m) => m.offset,
      updateTriggers: { getIcon: [liveDispatchByPhase, backupBusIds] },
      pickable: true,
    });

    // Load badge under the transformer: the number that separates "with LEO" from "without".
    const pct = deEnergised ? null : trafoPct;
    const badge = transformer && pct != null ? new TextLayer<Feature>({
      id: "transformer-load",
      data: [transformer],
      getPosition: pos,
      getText: () => `${Math.round(pct)}% load`,
      getPixelOffset: [-34, 34],
      getSize: 13,
      fontWeight: 700,
      fontFamily: typeof document !== "undefined" ? getComputedStyle(document.body).fontFamily : "sans-serif",
      characterSet: "auto",
      getColor: pct > 100 ? [255, 255, 255, 255] : [6, 20, 12, 255],
      background: true,
      getBackgroundColor: pct > 100 ? [196, 53, 45, 245] : pct > 85 ? [224, 167, 46, 245] : [47, 191, 113, 245],
      getBorderColor: [5, 8, 12, 255],
      getBorderWidth: 1,
      backgroundBorderRadius: 4,
      backgroundPadding: [6, 3],
      updateTriggers: { getText: [pct], getColor: [pct], getBackgroundColor: [pct] },
    }) : null;

    return [tileLayer, lineCasing, feederLines, points, equipment, ...(badge ? [badge] : [])];
  }, [data, busState, nominalV, vLimitPct, backupBusIds, liveDispatchByPhase, trafoPct, deEnergised]);

  return (
    <div ref={boxRef} className="relative w-full h-full rounded-lg overflow-hidden border border-[var(--leo-border)]">
      <DeckGL
        initialViewState={initialViewState}
        controller={true}
        layers={layers}
        onHover={(info) => setHover(info.object ? { ...info, object: info.object.f ?? info.object } : null)}
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
          <p className="text-[var(--leo-text-dim)]">No grid supply. Critical premises outlined in teal are on LEO&apos;s backup circuit.</p>
        </div>
      )}

      <details className="group absolute top-2 right-2 rounded-md border border-white/10 bg-[rgb(11_15_20/0.9)] text-xs leading-snug backdrop-blur-sm open:w-64">
        <summary className="flex cursor-pointer select-none list-none items-center gap-3 px-3 py-1.5 text-[var(--leo-text)] [&::-webkit-details-marker]:hidden">
          {[
            ["rgb(47,191,113)", "ok"],
            ["rgb(224,167,46)", "near limit"],
            ["rgb(224,71,62)", "outside limits"],
          ].map(([c, t]) => (
            <span key={t} className="flex items-center gap-1.5 whitespace-nowrap group-open:hidden">
              <span aria-hidden className="inline-block h-[3px] w-3.5 rounded" style={{ background: c }} />
              {t}
            </span>
          ))}
          <span className="ml-auto whitespace-nowrap text-[var(--leo-text-dim)] group-open:ml-0">
            <span className="group-open:hidden">Legend ▸</span><span className="hidden group-open:inline">Legend ▾</span>
          </span>
        </summary>
        <div className="flex flex-col gap-3 px-3 pb-3 pt-1">
          <div className="flex flex-col gap-1.5">
            <span className="text-[var(--leo-text-dim)]">Voltage on lines and homes</span>
            {[
              ["rgb(47,191,113)", "healthy"],
              ["rgb(224,167,46)", "near the limit"],
              ["rgb(224,71,62)", `outside ${nominalV} V ±${vLimitPct}%`],
            ].map(([c, t]) => (
              <span key={t} className="flex items-center gap-2">
                <span aria-hidden className="inline-block h-[3px] w-5 rounded" style={{ background: c }} />
                {t}
              </span>
            ))}
          </div>
          <div className="flex flex-col gap-1.5">
            <span className="text-[var(--leo-text-dim)]">Equipment</span>
            {[
              [transformerIcon(), "Transformer, with its load (% of rating)"],
              [batteryIcon("", 0.6), "Battery, one per phase (fill = charge)"],
              [sensorIcon(), "Sensor (at the transformer and line ends)"],
              [criticalIcon(), "Critical premise"],
              [criticalIcon(true), "Critical premise on backup power"],
            ].map(([src, t]) => (
              <span key={t} className="flex items-center gap-2">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={src} alt="" width={20} height={20} className="shrink-0" />
                {t}
              </span>
            ))}
          </div>
          <div className="flex flex-col gap-1.5">
            <span className="text-[var(--leo-text-dim)]">Homes</span>
            <span className="flex items-center gap-2">
              <span aria-hidden className="ml-1 inline-block h-2.5 w-2.5 rounded-full border border-black bg-[rgb(47,191,113)]" />
              <span className="ml-1.5">home, coloured by voltage</span>
            </span>
          </div>
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
