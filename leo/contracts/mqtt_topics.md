# LEO MQTT topics

Frozen Day 1. See Build Specification v1.0 §11.3.

```
leo/sensor/{dev_eui}/up        sensor uplink, ChirpStack envelope
leo/sensor/{dev_eui}/event     power-fail / restore, immediate
leo/clock/tick                 simulated time advance
leo/gateway/heartbeat          gateway liveness, 60 s
```

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
    "supply_present": true
  }
}
```

## Event payload

```json
{
  "devEUI": "0004a30b001c0530",
  "object": {"event": "power_fail", "ts": "2026-09-25T19:41:07Z",
             "last_voltage_v": 198.3}
}
```
