# LEO MQTT topics

Frozen Day 1. See Build Specification v1.0 §11.3.

```
leo/sensor/{dev_eui}/up        sensor uplink, ChirpStack envelope              sensors -> gateway (C4)
leo/sensor/{dev_eui}/event     power-fail / restore, immediate                  sensors -> gateway (C9)
leo/clock/tick                 simulated time (ISO 8601, retained)              clock -> everyone
leo/gateway/ack                {"tick", "run_id", "mode"} once a tick is acted on   gateway -> simulator
leo/gateway/heartbeat          {"gateway_id", "ts", "run_id", "mode"} per tick  gateway -> cloud
```

The gateway runs C7 (battery live correction) on every tick that lands on
a 15-minute boundary and C9 (outage detection, backup, restoration) on
every tick. A running clock ticks every second; a paused clock is silent
and publishes once per `/jump`, which is how `sim/loop.py` steps the world
in lockstep: publish the sensors' view of time T, jump to T, wait for
`leo/gateway/ack` of T, then advance the inverters. Nothing in the field
listens to the ack; it exists so a simulation never outruns the gateway.

## Uplink payload

ChirpStack-shaped so the ingestion path is unchanged when a real network server appears.
`object` carries decoded values rather than base64, which is what ChirpStack delivers
when a codec is configured. Keep `devEUI` as the join key to `sensor`.

```json
{
  "devEUI": "0004a30b001c0530",
  "fCnt": 14237,
  "fPort": 2,
  "rxInfo": [{"gatewayID": "gw-01", "rssi": -97, "loRaSNR": 7.5,
              "time": "2026-09-25T13:45:00Z"}],
  "object": {
    "interval_s": 60,
    "batch_end": "2026-09-25T13:45:00Z",
    "voltages_v": [241.2, 240.8, 239.9, 238.4, 237.1],
    "supply_present": true,
    "active_power_kw": 41.7
  }
}
```

`voltages_v` are the batch's samples (empty when `supply_present` is false;
sensors are battery-backed, so they keep reporting through an outage).
`active_power_kw` is sent only by **busbar** sensors: the split-core CT on
the phase's outgoing feeder, read at `batch_end`. It measures the phase's
household load and excludes the battery block, which connects on the
transformer side of the CT.

The gateway rejects (and counts by reason on `GET /state`) unknown devices,
a topic/payload devEUI mismatch, unparseable or naive timestamps,
implausible values (voltage outside 0-400 V, CT outside +-500 kW), a CT
reading from a far-end sensor, and repeats of an already-seen
`(devEUI, batch_end)`.

## Event payload

```json
{
  "devEUI": "0004a30b001c0530",
  "object": {"event": "power_fail", "ts": "2026-09-25T19:41:07Z",
             "last_voltage_v": 198.3}
}
```

`event` is `power_fail` or `power_restore`; `last_voltage_v` may be null.
