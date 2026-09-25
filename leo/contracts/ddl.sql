-- LEO contracts/ddl.sql — frozen Day 1. Any later change is breaking;
-- requires both owners to agree. See Build Specification v1.0 §11.1.

-- ============ enums ============
CREATE TYPE phase_t           AS ENUM ('R','Y','B');
CREATE TYPE provenance_t      AS ENUM ('measured','estimated','forecast','simulated');
CREATE TYPE sensor_place_t    AS ENUM ('busbar','far_end');
CREATE TYPE battery_mode_t    AS ENUM ('idle','normal','pre_outage','backup','restoration');
CREATE TYPE mode_owner_t      AS ENUM ('C7','C9');
CREATE TYPE consent_purpose_t AS ENUM (
  'meter_data_access','dr_offers','critical_premise_disclosure','sensor_hosting');
CREATE TYPE entry_type_t      AS ENUM (
  'dr_incentive','absorption_payment','discharge_rebate','backup_fee');
CREATE TYPE event_kind_t      AS ENUM (
  'power_fail','restore','grid_loss','grid_return','overload_trip',
  'dr_event_start','dr_event_end','backup_start','backup_end','citizen_report');
CREATE TYPE rec_status_t      AS ENUM ('open','acknowledged','dispatched','resolved');
CREATE TYPE severity_t        AS ENUM ('low','medium','high');
CREATE TYPE critical_class_t  AS ENUM ('none','health','water','livelihood','education');

-- ============ runs ============
-- Every observation and model output belongs to exactly one run.
CREATE TABLE run (
  run_id      TEXT PRIMARY KEY,        -- 'normal','outage','baseline','metrics-arm-a'
  scenario    TEXT NOT NULL,
  leo_enabled BOOLEAN NOT NULL,
  seed        BIGINT NOT NULL,
  sim_start   TIMESTAMPTZ NOT NULL,
  sim_end     TIMESTAMPTZ NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  notes       TEXT
);

-- ============ static world ============
CREATE TABLE neighbourhood (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  dt_id           TEXT NOT NULL,
  centroid_lat    DOUBLE PRECISION NOT NULL,
  centroid_lon    DOUBLE PRECISION NOT NULL,
  transformer_kva DOUBLE PRECISION NOT NULL,
  nominal_v_ln    DOUBLE PRECISION NOT NULL DEFAULT 250.0,
  v_limit_pct     DOUBLE PRECISION NOT NULL DEFAULT 6.0,
  timezone        TEXT NOT NULL DEFAULT 'Asia/Kolkata'
);

CREATE TABLE bus (
  id               TEXT PRIMARY KEY,
  neighbourhood_id TEXT NOT NULL REFERENCES neighbourhood(id),
  lat              DOUBLE PRECISION NOT NULL,
  lon              DOUBLE PRECISION NOT NULL,
  is_transformer   BOOLEAN NOT NULL DEFAULT FALSE,
  parent_bus_id    TEXT REFERENCES bus(id)
);

CREATE TABLE line (
  id             TEXT PRIMARY KEY,
  from_bus       TEXT NOT NULL REFERENCES bus(id),
  to_bus         TEXT NOT NULL REFERENCES bus(id),
  length_m       DOUBLE PRECISION NOT NULL,
  conductor_type TEXT NOT NULL,           -- '3x95+70','3x50+35','16'
  r_ohm_per_km   DOUBLE PRECISION NOT NULL,
  x_ohm_per_km   DOUBLE PRECISION NOT NULL,
  ampacity_a     DOUBLE PRECISION NOT NULL
);

CREATE TABLE household (
  id                 TEXT PRIMARY KEY,
  bus_id             TEXT NOT NULL REFERENCES bus(id),
  phase              phase_t NOT NULL,
  sanctioned_load_kw DOUBLE PRECISION NOT NULL,
  has_pv             BOOLEAN NOT NULL DEFAULT FALSE,
  pv_kwp             DOUBLE PRECISION,     -- known to LEO
  is_business        BOOLEAN NOT NULL DEFAULT FALSE,
  is_critical        BOOLEAN NOT NULL DEFAULT FALSE,
  critical_class     critical_class_t NOT NULL DEFAULT 'none',
  enrolled_at        DATE
);

-- Truth. The simulator writes it; LEO must never read it.
-- Enforce with a DB role that has no SELECT on *_truth tables.
CREATE TABLE household_truth (
  household_id   TEXT PRIMARY KEY REFERENCES household(id),
  persona        TEXT NOT NULL,
  pv_tilt_deg    DOUBLE PRECISION,
  pv_azimuth_deg DOUBLE PRECISION,
  pv_soiling     DOUBLE PRECISION,
  has_inverter   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE appliance (
  id           BIGSERIAL PRIMARY KEY,
  household_id TEXT NOT NULL REFERENCES household(id),
  kind         TEXT NOT NULL,            -- fridge, ac, cooler, pump, ...
  rating_w     DOUBLE PRECISION NOT NULL,
  count        INT NOT NULL DEFAULT 1
);

CREATE TABLE sensor (
  id        TEXT PRIMARY KEY,
  bus_id    TEXT NOT NULL REFERENCES bus(id),
  phase     phase_t NOT NULL,
  placement sensor_place_t NOT NULL,
  dev_eui   TEXT NOT NULL UNIQUE
);

CREATE TABLE battery_block (
  id           TEXT PRIMARY KEY,
  phase        phase_t NOT NULL UNIQUE,   -- exactly one block per phase
  unit_id      INT NOT NULL UNIQUE,       -- Modbus unit ID 1..3
  capacity_kwh DOUBLE PRECISION NOT NULL,
  power_kw     DOUBLE PRECISION NOT NULL,
  soc_min      DOUBLE PRECISION NOT NULL DEFAULT 0.15,
  soc_max      DOUBLE PRECISION NOT NULL DEFAULT 0.90,
  efficiency   DOUBLE PRECISION NOT NULL DEFAULT 0.92
);

CREATE TABLE premise_backup (
  household_id   TEXT PRIMARY KEY REFERENCES household(id),
  priority_class INT NOT NULL,            -- 1 = highest
  max_current_a  DOUBLE PRECISION NOT NULL
);

CREATE TABLE consent (
  household_id TEXT NOT NULL REFERENCES household(id),
  purpose      consent_purpose_t NOT NULL,
  granted      BOOLEAN NOT NULL,
  changed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (household_id, purpose)
);

-- ============ observations ============
CREATE TABLE sensor_reading (
  run_id         TEXT NOT NULL REFERENCES run(run_id),
  sensor_id      TEXT NOT NULL REFERENCES sensor(id),
  ts_end         TIMESTAMPTZ NOT NULL,    -- interval END
  voltage_v      DOUBLE PRECISION,
  supply_present BOOLEAN NOT NULL,
  provenance     provenance_t NOT NULL,
  PRIMARY KEY (run_id, sensor_id, ts_end)
);

-- received_at enforces the latency rule.
-- Model queries MUST filter on received_at <= run_time, never on ts_end.
CREATE TABLE meter_interval (
  run_id        TEXT NOT NULL REFERENCES run(run_id),
  household_id  TEXT NOT NULL REFERENCES household(id),
  ts_end        TIMESTAMPTZ NOT NULL,
  import_kwh    DOUBLE PRECISION,
  export_kwh    DOUBLE PRECISION,
  avg_voltage_v DOUBLE PRECISION,
  received_at   TIMESTAMPTZ NOT NULL,
  provenance    provenance_t NOT NULL,
  PRIMARY KEY (run_id, household_id, ts_end)
);
CREATE INDEX ON meter_interval (run_id, received_at);

CREATE TABLE battery_telemetry (
  run_id       TEXT NOT NULL REFERENCES run(run_id),
  block_id     TEXT NOT NULL REFERENCES battery_block(id),
  ts           TIMESTAMPTZ NOT NULL,
  soc          DOUBLE PRECISION NOT NULL,
  power_kw     DOUBLE PRECISION NOT NULL, -- + discharge, - charge
  voltage_v    DOUBLE PRECISION,
  grid_present BOOLEAN NOT NULL,
  alarm_bits   INT NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, block_id, ts)
);

CREATE TABLE premise_meter (
  run_id       TEXT NOT NULL REFERENCES run(run_id),
  household_id TEXT NOT NULL REFERENCES household(id),
  ts_end       TIMESTAMPTZ NOT NULL,
  backup_kwh   DOUBLE PRECISION NOT NULL,
  current_a    DOUBLE PRECISION,
  limit_active BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (run_id, household_id, ts_end)
);

CREATE TABLE event (
  run_id  TEXT NOT NULL REFERENCES run(run_id),
  id      BIGSERIAL,
  ts      TIMESTAMPTZ NOT NULL,
  kind    event_kind_t NOT NULL,
  scope   TEXT,                           -- 'upstream','phase:R','local:BUS-12'
  source  TEXT NOT NULL,                  -- 'sensor','inverter','citizen','sim'
  payload JSONB,
  PRIMARY KEY (run_id, id)
);

-- ============ model outputs ============
CREATE TABLE forecast (
  run_id        TEXT NOT NULL REFERENCES run(run_id),
  model         TEXT NOT NULL,            -- 'load_v3','pv_v3'
  model_version TEXT NOT NULL,
  dt_id         TEXT NOT NULL,
  phase         phase_t NOT NULL,
  ts_end        TIMESTAMPTZ NOT NULL,
  run_time      TIMESTAMPTZ NOT NULL,
  p10_kw        DOUBLE PRECISION NOT NULL,
  p50_kw        DOUBLE PRECISION NOT NULL,
  p90_kw        DOUBLE PRECISION NOT NULL,
  inputs_as_of  JSONB NOT NULL,
  PRIMARY KEY (run_id, model, phase, ts_end, run_time)
);

CREATE TABLE network_result (
  run_id      TEXT NOT NULL REFERENCES run(run_id),
  ts_end      TIMESTAMPTZ NOT NULL,
  bus_id      TEXT NOT NULL REFERENCES bus(id),
  phase       phase_t NOT NULL,
  voltage_v   DOUBLE PRECISION NOT NULL,
  loading_pct DOUBLE PRECISION,
  violation   BOOLEAN NOT NULL DEFAULT FALSE,
  is_forecast BOOLEAN NOT NULL,
  PRIMARY KEY (run_id, ts_end, bus_id, phase, is_forecast)
);

CREATE TABLE phase_limit (
  run_id           TEXT NOT NULL REFERENCES run(run_id),
  ts_end           TIMESTAMPTZ NOT NULL,
  phase            phase_t NOT NULL,
  max_charge_kw    DOUBLE PRECISION NOT NULL,  -- positive magnitude
  max_discharge_kw DOUBLE PRECISION NOT NULL,  -- positive magnitude
  binding_bus_id   TEXT,
  PRIMARY KEY (run_id, ts_end, phase)
);

CREATE TABLE plan (
  run_id      TEXT NOT NULL REFERENCES run(run_id),
  plan_date   DATE NOT NULL,
  phase       phase_t NOT NULL,
  ts_end      TIMESTAMPTZ NOT NULL,
  setpoint_kw DOUBLE PRECISION NOT NULL,   -- + discharge, - charge
  mode        battery_mode_t NOT NULL,
  planner     TEXT NOT NULL,               -- 'cvx' | 'rules'
  reserve_kwh DOUBLE PRECISION NOT NULL,
  approved_at TIMESTAMPTZ,
  approved_by TEXT,
  PRIMARY KEY (run_id, plan_date, phase, ts_end)
);

CREATE TABLE mode_transition (
  run_id    TEXT NOT NULL REFERENCES run(run_id),
  ts        TIMESTAMPTZ NOT NULL,
  from_mode battery_mode_t NOT NULL,
  to_mode   battery_mode_t NOT NULL,
  owner     mode_owner_t NOT NULL,
  reason    TEXT NOT NULL,
  PRIMARY KEY (run_id, ts)
);

CREATE TABLE dispatch (
  run_id         TEXT NOT NULL REFERENCES run(run_id),
  block_id       TEXT NOT NULL REFERENCES battery_block(id),
  ts_end         TIMESTAMPTZ NOT NULL,
  setpoint_kw    DOUBLE PRECISION NOT NULL,
  actual_kw      DOUBLE PRECISION NOT NULL,
  soc_after      DOUBLE PRECISION NOT NULL,
  mode           battery_mode_t NOT NULL,
  rule_triggered TEXT,                     -- 'undervoltage','overvoltage',NULL
  PRIMARY KEY (run_id, block_id, ts_end)
);

CREATE TABLE dr_event (
  run_id       TEXT NOT NULL REFERENCES run(run_id),
  id           TEXT NOT NULL,
  phase        phase_t NOT NULL,
  window_start TIMESTAMPTZ NOT NULL,
  window_end   TIMESTAMPTZ NOT NULL,
  target_kw    DOUBLE PRECISION NOT NULL,
  v_paise_kwh  BIGINT NOT NULL,
  PRIMARY KEY (run_id, id)
);

CREATE TABLE dr_offer (
  run_id        TEXT NOT NULL,
  event_id      TEXT NOT NULL,
  household_id  TEXT NOT NULL REFERENCES household(id),
  level         DOUBLE PRECISION NOT NULL CHECK (level IN (0,0.25,0.5,0.75)),
  predicted_kwh DOUBLE PRECISION NOT NULL,
  sent_at       TIMESTAMPTZ,
  channel       TEXT,
  replied       BOOLEAN NOT NULL DEFAULT FALSE,
  is_holdout    BOOLEAN NOT NULL DEFAULT FALSE,
  verified_kwh  DOUBLE PRECISION,
  PRIMARY KEY (run_id, event_id, household_id),
  FOREIGN KEY (run_id, event_id) REFERENCES dr_event(run_id, id)
);

CREATE TABLE recommendation (
  run_id             TEXT NOT NULL REFERENCES run(run_id),
  rec_id             TEXT NOT NULL,
  dt_id              TEXT NOT NULL,
  phase              phase_t,
  window_start       TIMESTAMPTZ NOT NULL,
  window_end         TIMESTAMPTZ NOT NULL,
  issue              TEXT NOT NULL,
  severity           severity_t NOT NULL,
  residual_gap_kw    DOUBLE PRECISION,
  local_actions      JSONB NOT NULL DEFAULT '[]',
  recommended_action TEXT NOT NULL,
  evidence           JSONB NOT NULL DEFAULT '{}',
  status             rec_status_t NOT NULL DEFAULT 'open',
  status_changed_at  TIMESTAMPTZ,
  PRIMARY KEY (run_id, rec_id)
);

-- amount_paise is always >= 0. There is no penalty mechanism anywhere.
CREATE TABLE ledger (
  run_id                TEXT NOT NULL REFERENCES run(run_id),
  id                    BIGSERIAL,
  household_id          TEXT NOT NULL REFERENCES household(id),
  date                  DATE NOT NULL,
  entry_type            entry_type_t NOT NULL,
  deficit_kwh           DOUBLE PRECISION,
  matched_kwh           DOUBLE PRECISION,
  amount_paise          BIGINT NOT NULL CHECK (amount_paise >= 0),
  period_budget_paise   BIGINT NOT NULL,
  payout_scaling_factor DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  linked_event_id       TEXT,
  rank_snapshot         INT,
  running_balance_paise BIGINT NOT NULL,
  PRIMARY KEY (run_id, id)
);
CREATE INDEX ON ledger (run_id, household_id, date);

CREATE TABLE period_revenue (
  run_id            TEXT NOT NULL REFERENCES run(run_id),
  period_start      DATE NOT NULL,
  period_end        DATE NOT NULL,
  dfpo_paise        BIGINT NOT NULL DEFAULT 0,
  arbitrage_paise   BIGINT NOT NULL DEFAULT 0,
  backup_fees_paise BIGINT NOT NULL DEFAULT 0,
  alpha             DOUBLE PRECISION NOT NULL DEFAULT 0.6,
  PRIMARY KEY (run_id, period_start)
);
