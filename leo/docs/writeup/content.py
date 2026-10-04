"""Section text for the LEO submission. sections(ctx) returns [(title, html)];
the first entry is the cover page."""


def sections(c):
    cite, lakh, n, pct, fig, table, E = c["cite"], c["lakh"], c["n"], c["pct"], c["fig"], c["table"], c["E"]
    R = {r["config"]: r for r in E["results"]}
    rec = R["leo_60_15_smart"]
    rel, op, dc, ph = rec["reliability"], rec["operator"], rec["discom"], rec["physical"]
    hh = rec["households"]["per_household"]
    A = E["assumptions"]
    alpha = A["operator"]["alpha"]
    sms_acc = 100 * ph["dr_accepted"] / ph["dr_offers"] if ph["dr_offers"] else 0
    ac_kw_event = ph["ac_curtail_kwh"] / max(ph["ac_events"], 1) / 2.0
    pump_kwh_day = ph["pump_shift_kwh"] / max(ph["smart_pumps"], 1) / 365
    loss_saved = rel["loss_kwh_base"] - rel["loss_kwh"]
    gross = dc["gross_saving"]
    to_op = -(dc["paid_to_operator_capacity"] + dc["paid_to_operator_energy"] + dc["energy_settlement_with_operator"])
    th = rec["system_thresholds"]
    out = []

    # ------------------------------------------------------------------ cover
    out.append(("", f"""
<div class='cover'>
  <h0>LEO</h0>
  <div class='sub'>Local Energy Orchestrator — neighbourhood-scale flexibility that keeps clean power dependable
  on India's most stressed distribution transformers</div>
  <div class='meta'>
    Schneider Electric Grid Reliability Hackathon · Challenge: <b>Grid Reliability — Renewable Intermittency</b><br/>
    Submission document: solution write-up, architecture, design artefacts, simulation, measured reliability improvement,
    ownership &amp; O&amp;M model and unit economics<br/>
    Target deployment: an overloaded 100 kVA distribution transformer in peri-urban Karnataka (BESCOM area)<br/>
    October 2026
  </div>
  <h2 style='margin-top:26pt'>Contents</h2>
  {{{{TOC}}}}
</div>"""))

    # ------------------------------------------------------- 1 Executive summary
    out.append(("Executive summary", f"""
<p>India's distribution transformers (DTs) are where renewable intermittency and growing evening demand collide. Rooftop
solar exports at midday and disappears at sunset exactly as cooling and lighting load peaks; on the Indian Energy Exchange
this “duck curve” takes evening prices to the ₹10/kWh ceiling while midday blocks clear near ₹1.91/kWh{cite('ember','ranjith_iex','energymap')}.
Overloaded DTs fail: BESCOM lost 38,288 of 497,991 transformers (7.96%) in FY 2023-24, 29% of them to overload{cite('hindu_dt','dt_failure')}.
Regulators have now made flexibility an obligation — Karnataka's DF/DSM Regulations 2026 require DISCOMs to procure demand
flexibility of 0.5% of peak, rising to 2.0% by FY 2029-30, on the Maharashtra model of a ₹2,000/kW-yr shortfall penalty{cite('mercom_kerc','merc','saur_merc')}.</p>

<p><b>LEO</b> is a software-and-small-hardware coordination layer that a local cooperative or NGO, registered as a demand
flexibility aggregator, runs at the DT. It forecasts tomorrow's load and solar per phase, plans three single-phase
second-life battery blocks and two automated demand-response programmes around the forecast, corrects live from its own
low-cost sensors, protects critical premises during outages through a small backup circuit, and settles everything
transparently a day later when smart-meter data arrives. It commands only assets the operator owns; everything on the
DISCOM network is a recommendation.</p>

<p>We built the whole system as a running prototype and evaluated it on a simulated year — one week in every month — of a
real Hoskote (Bengaluru Rural) street grid with 150 households on a 100 kVA DT, against an identical baseline without LEO.</p>

<div class='kpis'>
 <div class='kpi'><div class='v'>{n(rel['evening_peak_kw_mean_base'])} → {n(rel['evening_peak_kw_mean'])} kW</div><div class='l'>average evening peak at the DT ({n(-pct(rel['evening_peak_kw_mean_base'], rel['evening_peak_kw_mean']))}% lower; rating ≈ 95 kW)</div></div>
 <div class='kpi'><div class='v'>0% → {n(rel['critical_premise_availability_pct'])}%</div><div class='l'>critical premises (clinic, water pump, school, shops) powered through outages and load shedding</div></div>
 <div class='kpi'><div class='v'>{n(rel['dt_failure_rate_pct_base'],1)}% → {n(rel['dt_failure_rate_pct'],1)}%</div><div class='l'>expected DT failure rate per year (IEEE C57.91 ageing); {n(rel['dt_failure_customer_hours_avoided'])} customer-hours/yr of failure outages avoided</div></div>
 <div class='kpi'><div class='v'>{n(ph['verified_peak_kw'],1)} kW</div><div class='l'>verified peak reduction for the DISCOM's DFPO obligation</div></div>
 <div class='kpi'><div class='v'>{lakh(gross)}/yr</div><div class='l'>DISCOM gross saving per DT ({lakh(gross*1000)}/yr per 1,000 overloaded DTs)</div></div>
 <div class='kpi'><div class='v'>{n(op['payback_years'],1)} years</div><div class='l'>operator payback; 10-year NPV {lakh(op['npv'])}; households pay ₹0 upfront</div></div>
</div>

<p>The economics close because three value streams stack on an overloaded DT: evening power the DISCOM no longer buys
({lakh(dc['power_purchase_saved'])}/yr), DFPO penalty avoided ({lakh(dc['dfpo_penalty_avoided'])}/yr) and transformer failures avoided
({lakh(dc['dt_failures_avoided'])}/yr). A two-part contract — the ₹2,000/kW-yr DFPO benchmark plus ₹{A['dfpo']['evening_energy_rs_per_kwh']:.2f}
per verified evening kWh — falls inside a deal zone (₹{op['break_even_rate_rs_per_kwh']:.2f}–₹{dc['max_rate_rs_per_kwh']:.2f}/kWh) where the operator,
the DISCOM and households all come out ahead. Every price in the model is sourced from public reports, tariff orders and
Indian pilots, or flagged for verification.</p>
"""))

    # -------------------------------------------------------- 2 Problem statement
    out.append(("Problem statement", f"""
<p>The challenge asks for affordable, neighbourhood-scale flexibility tools — forecasting, smart load management, demand
response, shared storage and local coordination — that keep electricity dependable when renewable supply falls below
demand, stay local and manageable, are viable for low-income urban and peri-urban communities, and support the DISCOM{cite('brief')}.
Four facts about Indian distribution grids shape our answer.</p>

<h2>2.1 The evening ramp and the duck curve</h2>
<p>Solar generation drives day-ahead prices to their floor at midday — average lowest blocks around ₹1.91/kWh and sometimes
below ₹1 — and collapses at sunset, when residential cooling and lighting surge. Evening prices routinely hit the
₹10,000/MWh regulatory ceiling, an average evening premium of about ₹7.92/kWh and a midday-to-evening spread above
₹8,000/MWh{cite('ember','ranjith_iex','energymap','ijert_iex')}. The intermittency gap is therefore daily and predictable, not only
a weather event.</p>

<h2>2.2 Transformers fail from thermal overload</h2>
<p>BESCOM's DT failure rate has stayed between 7.5% and 8.07%; in FY 2023-24 it lost 38,288 of 497,991 units (7.96%), and
sustained overload causes 29.09% of failures{cite('hindu_dt','dt_failure')}. Overloading raises hot-spot temperature and
accelerates insulation ageing, cutting an expected 30-year life to 11–15 years; one Indian case study recorded failures within
20–28 months{cite('dt_overload','dt_premature')}. A 100 kVA unit costs about ₹5.05 lakh to replace; a repair about ₹11,520{cite('dt_repair','research')}.
Peri-urban DTs that grew with their neighbourhoods without augmentation are the ones at risk — and a failed DT means a
multi-day outage for every household on it.</p>

<h2>2.3 Losses peak with the peak</h2>
<p>BESCOM's AT&amp;C losses fell to 9.13% in FY 2023-24{cite('hindu_loss')}. The technical part is resistive (I²R) and grows with the
square of current, so a disproportionate share is lost precisely at the evening peak; single-phase heavy appliances make
Indian LV networks unbalanced, adding neutral current{cite('dt_overload','research')}.</p>

<h2>2.4 The DISCOM is blind below the feeder, and household data arrives a day late</h2>
<p>DISCOMs see the 11 kV feeder through SCADA and the National Feeder Monitoring System{cite('nfms')} but only about 3% of DT meters
were communicating in 2024{cite('ceew')}. Smart meters are rolling out under RDSS (over 5.28 crore installed){cite('rdss')}, but the
contractual AMISP service level is that 95% of 15-minute data reaches the DISCOM within 8 hours and 98% within 12 hours{cite('bses_sla','rec_ladakh','prayas_sm')}.
Any design that assumes live household data in India today will not work in the field.</p>

<h2>2.5 Flexibility is now an obligation</h2>
<p>Maharashtra's 2024 DF/DSM Regulations created Demand Flexibility Portfolio Obligations (DFPO): 2.5% of the previous
year's peak, rising to 7.0% by FY 2029-30, measured in kW at a single instance in the peak period, with a ₹0.20 crore/MW
(₹2,000/kW) disincentive for shortfall and an equal incentive for over-achievement, and they formally recognise
aggregators{cite('merc','merc_draft','saur_merc','aggregator')}. Karnataka followed in March 2026 with targets of 0.5% rising to
2.0% and verification by empanelled independent agencies{cite('mercom_kerc','nie_kerc','sq_kerc')}. DISCOMs therefore need a source of
verifiable, local kW — and a low-income neighbourhood with an overloaded DT is where that kW is worth the most.</p>
"""))

    # ------------------------------------------------- 3 Alignment to challenge
    rows = [
        ["Bridge intermittency gaps", "Midday solar stored in three per-phase blocks and released over the evening peak; pumps moved into the solar window daily; AC events on stress days; Pre-outage for announced load shedding",
         f"Evening peak {n(rel['evening_peak_kw_mean_base'])}→{n(rel['evening_peak_kw_mean'])} kW; {n(rel['solar_absorbed_kwh'])} kWh/yr of local solar stored; critical premises {n(rel['critical_premise_availability_pct'])}% powered"],
        ["Stay local and manageable", "One DT, one edge gateway that keeps running offline, LoRa sensors with no SIM cost, one technician per 20 DTs; no DISCOM equipment is commanded",
         "Operator controls only its own assets; DISCOM receives recommendations"],
        ["Genuinely affordable", "Second-life LFP packs; ₹999 IR blasters and ₹2,090 smart plugs instead of new appliances; households pay nothing upfront and bills are unchanged",
         f"Operator payback {n(op['payback_years'],1)} yr; pump homes earn ≈{lakh(hh['pump_home_fee_rs']+hh['pump_home_tod_saving_rs']+hh['streams_rs_per_household'])}/yr, AC homes ≈{lakh(hh['ac_home_event_pay_rs']+hh['streams_rs_per_household'])}/yr"],
        ["Support the DISCOM", "Verified kW for DFPO; day-ahead stress forecast; structured recommendations (tap change, outage scope); fewer transformer failures; lower peak purchase and losses",
         f"{n(ph['verified_peak_kw'],1)} kW verified; DISCOM gross saving {lakh(gross)}/yr per DT; losses −{n(loss_saved)} kWh/yr"],
        ["Measurable reliability improvement vs a defined baseline", "Identical simulated world with and without LEO, 84 days spanning all seasons, metrics M1–M5 plus outage hours and transformer ageing", "Section 11"],
        ["Ownership, O&amp;M and unit economics", "Cooperative/NGO aggregator running a cluster of 20 DTs; two-part DFPO contract; household revenue share", "Sections 12–13"],
    ]
    out.append(("Alignment to the selected challenge", f"""
<p>We chose <b>Grid Reliability — Renewable Intermittency</b>. LEO touches four of the illustrative solution areas named in
the brief: shared storage with fair-access pooling (second-life batteries), local control with smart load management,
demand response and critical-load priority, pooled reliability across users, and a DISCOM-facing dashboard that predicts
feeder stress and recommends actions{cite('brief')}.</p>
{table(["Brief requirement", "How LEO answers it", "Evidence in this document"], rows, "Traceability from the brief to LEO.")}
"""))

    # ------------------------------------------------------ 4 Proposed solution
    out.append(("Proposed solution", f"""
<p>LEO is the missing coordination layer between the DISCOM's feeder-level systems and the households on one transformer.
It complements rather than competes with utility ADMS/DERMS: we adopt the DERMS idea of aggregating many small resources
into one controllable virtual resource, but without claiming utility-grade control authority{cite('schneider','proposal')}.</p>

<h2>4.1 What is installed at one transformer</h2>
<ul>
<li><b>Six voltage/outage sensors</b> on LoRaWAN — three at the DT's LV busbar (one per phase, each with a split-core CT) and three
at the far end of each phase{cite('arch','esmi','nline')}.</li>
<li><b>Three single-phase storage blocks</b>, one per phase: 60 kWh second-life LFP pack with BMS and a 15 kW hybrid inverter with
a grid port and a backup port. Three blocks rather than one three-phase unit because LV problems are per phase{cite('arch')}.</li>
<li><b>A backup circuit</b> — a separate LV cable from the battery backup ports to six registered critical premises (a clinic,
a borewell pump, a tuition centre and three shops), each with a dual-source meter that enforces a current limit.</li>
<li><b>Automated demand-response devices</b> in enrolled homes: a 16 A smart plug on the water pump, a Wi-Fi IR blaster on the
AC{cite('smart_devices')}.</li>
<li><b>An edge gateway</b> (Raspberry Pi-class, on UPS) that keeps planning, live control and outage response running if the
internet link drops; and an <b>operator cloud</b> for forecasting training, DR, settlement, recommendations and the consoles.</li>
</ul>

<h2>4.2 What LEO does every day</h2>
<ol>
<li><b>Forecast (D-1, 19:30).</b> Per-phase P10/P50/P90 net load for the next 24 h from weather, calendar and day-late meter history.</li>
<li><b>Model the network.</b> Run a three-phase power flow on the forecast: which bus, which hour, how far outside limits, and how much
each battery may safely charge or discharge.</li>
<li><b>Plan storage and DR.</b> Fill each block in the midday trough (solar export first) and spread its energy over the
evening peak; schedule enrolled pumps into the solar window; on the year's most stressed days, call an automated AC event
and send SMS offers for the residual.</li>
<li><b>Operator approves (D-1, 23:30)</b> in the console, where every action carries its reason.</li>
<li><b>Live correction.</b> Every 15 minutes, re-solve the battery setpoint from the busbar CT and sensors; absorb overvoltage at
midday; on a load-shedding notice enter Pre-outage; on grid loss run the backup circuit.</li>
<li><b>Escalate.</b> Residual violations become structured recommendations to the DISCOM (for example, a tap change).</li>
<li><b>Settle (D+1).</b> When meter data arrives, verify reductions against IPMVP Option C baselines and a random holdout, pay DR
participants first, then pool the household share of revenue{cite('ipmvp','caiso_dr')}.</li>
</ol>

<h2>4.3 Who runs it and who pays</h2>
<p>A local cooperative, NGO or self-help-group federation registers as a demand-flexibility aggregator — the role the MERC
and KERC regulations recognise{cite('merc','mercom_kerc','aggregator')} — and runs LEO on a cluster of about 20 DTs with one
technician. The DISCOM pays it under a two-part contract: the DFPO capacity value for verified kW plus a payment per verified
evening kWh. Households pay nothing upfront, their DISCOM bills are untouched, and they are paid from the operator's realised
revenue; there is no penalty anywhere in the system. Section 12 details the model.</p>
"""))

    # ----------------------------------------------------- 5 Features & journeys
    out.append(("Key features and user journeys", f"""
<h2>5.1 Key features</h2>
{table(["Feature", "What it does", "Why it matters"], [
        ["Day-ahead stress forecast", "Per-phase P10/P50/P90 load and solar, run through a three-phase network model", "Overloads and voltage problems are predicted a day early, with the reason (load, temperature, irradiance)"],
        ["Peak-shaving storage", "Water-filling plan, re-solved every 15 minutes from the busbar CT", "Energy goes to the highest-load intervals, so the battery never runs empty before the peak"],
        ["Automated pump shifting", f"Enrolled pumps skip their 18:00 run; LEO schedules it in the midday hour with most solar surplus (≈{pump_kwh_day:.2f} kWh per home per day)", "Moves load out of the evening ramp every day, onto local solar"],
        ["Automated AC events", f"On DFPO stress days enrolled ACs cycle to 40% for 2 h with pre-cooling; ≈{ac_kw_event:.1f} kW per event", "Large, reliable kW at the peak instant, worth paying ₹75 per home per event"],
        ["SMS / IVR offers", "Contextual bandit chooses ₹0, ₹25, ₹50 or ₹100 per household per event", "Reaches homes without smart devices or smartphones; pays only where it buys a real cut"],
        ["Pre-outage", "On a DISCOM load-shedding notice, reserve what the backup premises need (×1.5) and keep shaving with the rest", "Critical premises stay powered through scheduled cuts"],
        ["Backup circuit", "Inverters island onto their backup ports; premises served by priority with current limits; staggered re-transfer", "Clinic, water and livelihoods keep running; no cold-load surge on restoration"],
        ["Recommendations", "Tap-change, inverter-setting and outage-scope notes to the DISCOM, with evidence", "LEO advises; the DISCOM decides on its own network"],
        ["Explainable console", "Predicted events with reasons, every action with its why, outcome revealed against the forecast", "Operators and DISCOM engineers can audit every decision"],
        ["Transparent settlement", "IPMVP baselines, random holdout, DR paid first, pooled rewards capped by realised revenue", "Defensible to the independent verification agency; households never promised more than was earned"],
    ], "LEO's main features.")}

<h2>5.2 User journeys</h2>
<h3>Operator — the day before a hot April evening</h3>
<p>At 19:30 the forecast predicts phase-Y undervoltage to 218 V and a transformer overload to about 206% of rating from 16:15.
The console lists each predicted event with its causes and LEO's planned response: charge each block from midday solar,
discharge 41 kWh per phase across 16:30–21:00, move 20 pumps to 10:45, call an AC event and SMS offers for 17:45–19:45. At
23:30 the operator approves. Next evening the console shows the plan executing, the live corrections, and the outcome
against the forecast; residual undervoltage on long lines is escalated to the DISCOM as a tap-change recommendation.</p>
<h3>Household with a pump or AC</h3>
<p>The household enrols once (app, SMS or IVR, with per-purpose consent{cite('dpdp')}), and a ₹2,090 smart plug or ₹999 IR blaster is
fitted. The pump now runs at midday on neighbourhood solar; on about twenty summer evenings the AC is cycled for two hours after
a pre-cool, with an SMS beforehand and a one-tap override. It earns ₹{A['smart_dr']['pump_fee_rs_per_month']}/month (pump) or ₹75 per event (AC), plus its share of
the community pool, and its electricity bill is unchanged.</p>
<h3>Critical premise during load shedding</h3>
<p>BESCOM announces a 19:40–21:10 cut at 14:00. LEO enters Pre-outage, tops the blocks up to the backup reserve and SMSes all
150 homes. At 19:40 the inverters island and the clinic, borewell pump, tuition centre and shops stay powered from the backup
circuit by priority; at 21:10 they are re-transferred to the grid in batches.</p>
<h3>DISCOM engineer</h3>
<p>The DISCOM dashboard shows an action queue — forecast undervoltage on phase B with a recommended tap raise, an outage with its
scope — each with acknowledge/dispatch/resolve states and machine-readable JSON, plus verified kW for DFPO reporting.</p>
"""))

    # --------------------------------------------------------- 6 Architecture
    comp = [
        ["C1", "Voltage/outage sensors", "Field", "Per-phase RMS voltage every minute, power-fail/restore instantly; busbar units add a CT", "LoRaWAN IN865 → ChirpStack → MQTT"],
        ["C2", "Three single-phase storage blocks", "Field", "Second-life LFP + BMS + 15 kW hybrid inverter (grid and backup ports)", "Modbus TCP (SunSpec where supported)"],
        ["C3", "Backup circuit and premise meters", "Field", "Separate LV cable to 6 premises; dual-source meters with current limiters", "Modbus RTU (RS-485)"],
        ["—", "Automated DR devices", "Field (homes)", "16 A smart plugs on pumps, Wi-Fi IR blasters on ACs", "Wi-Fi via device cloud API"],
        ["C4", "Ingestion and store", "Edge", "Validates and timestamps every observation; single source of truth on the gateway", "MQTT, Modbus polling"],
        ["C5", "Forecasting inference", "Edge", "LightGBM quantile load model + pvlib PV per phase", "—"],
        ["C6", "Network model", "Edge", "pandapower 3-phase power flow: violations and safe charge/discharge limits", "—"],
        ["C7", "Battery orchestrator", "Edge", "Day-ahead water-filling plan; 15-minute receding-horizon re-solve; Normal/Pre-outage modes", "Modbus TCP to C2"],
        ["C8", "Demand-response engine", "Cloud", "Event days and windows; disjoint LinUCB offers; automated pump and AC programmes", "SMS/IVR via C14; device API"],
        ["C9", "Outage detection and backup", "Edge", "Classifies scope from sensor pattern; Backup/Restoration modes; premise allocation", "Modbus RTU to C3"],
        ["C10", "Data connectors", "Cloud", "Weather, IEX, SLDC, consented meter data", "REST; India Energy Stack"],
        ["C11", "Training service", "Cloud", "Weekly load-model retrain, PV calibration", "—"],
        ["C12", "Settlement and pooling", "Cloud", "IPMVP baselines, holdout verification, two-part revenue, DR payments, Streams 1–2", "UPI payouts"],
        ["C13", "Recommendation engine", "Cloud", "Residual gaps and outages → structured DISCOM actions", "Dashboard, API/webhook, SMS"],
        ["C14", "Messaging and consent", "Cloud", "DLT-registered SMS, IVR in Kannada/Hindi/English, per-purpose consent", "SMS / IVR"],
        ["C15", "Consoles", "Cloud", "Operator console, DISCOM dashboard, impact and economics", "HTTPS"],
    ]
    out.append(("System architecture", f"""
<p>The architecture follows one rule: the operator commands only assets it owns, and everything that must keep working during
an internet outage runs on the neighbourhood edge gateway. Figure 1 shows the system context; Figure 2 the components by tier
with the protocol on each link. Component numbers follow our System Architecture v3.0{cite('arch')}, updated to the built system.</p>
{fig('01_system_context.png', caption='System context. The dashed boundary is the operator&apos;s: LEO commands only the sensors, batteries, backup circuit and its own messages. The DISCOM switches its own network; LEO only recommends. Money flows from the DISCOM to the operator under the DFPO contract and from the operator to households.')}
{fig('02_layered_architecture.png', 'Layered architecture: field hardware, the neighbourhood edge gateway (offline-capable) and the operator cloud, with protocols on each link.')}
{table(["#", "Component", "Tier", "Role", "Interface / protocol"], comp, "Components.")}
<h2>6.1 Why three tiers</h2>
<p><b>Field</b> hardware is cheap, standard and operator-owned. <b>Edge</b> components are those whose failure would be unsafe or
costly if the internet dropped — live battery control, outage detection and the backup circuit. <b>Cloud</b> components need
history, people or money and tolerate hours of latency — training, DR offers, settlement, recommendations and the consoles.
This split also makes multi-DT operation cheap: one cloud serves a cluster; each DT adds only a gateway and field hardware.</p>
<h2>6.2 Energy and money flows</h2>
<p>Energy flows are shown as a day in Figure 9 (Section 11) and money flows as a Sankey in Figure 10 (Section 13).</p>
"""))

    # ------------------------------------------------------- 7 Technical approach
    out.append(("Technical approach", f"""
<h2>7.1 Design principle: plan day-ahead, correct live</h2>
<p>Household meter data arrives 8–24 hours late{cite('bses_sla')}, DT meters rarely communicate{cite('ceew')}, and only the operator's own
sensors are live. LEO therefore plans a day ahead from forecasts and corrects within the day using simple, transparent rules on
its own measurements. Figure 3 puts the latency on every arrow.</p>
{fig('03_data_flow_latency.png', width='72%', caption='Data flow with latency on each arrow. Meter data is a day late, so LEO plans at D-1 19:30, the operator approves at 23:30, live correction runs every 15 minutes from LEO&apos;s own sensors, and settlement runs on D+1.')}
{fig('08_decision_pipeline.png', width='48%', caption='The decision pipeline. Each stage records its reason, which is what the operator console shows — the pipeline doubles as the explainability story.')}

<h2>7.2 Forecasting (C5)</h2>
<p>A LightGBM quantile model{cite('lightgbm')} predicts gross load per phase at P10/P50/P90 for 96 fifteen-minute intervals from
weather forecasts (temperature, humidity, irradiance){cite('openmeteo')}, calendar and holiday features, and lagged consumption that
respects the meter-data latency rule — a feature is only used if it would have arrived by run time. Rooftop PV behind net
meters is invisible, so a pvlib physics model{cite('pvlib')} is scaled by a calibration factor fitted on clear-sky middays and multiplied by each
home's installed kWp; net load is gross minus PV. On weeks the model never saw in training (four quarterly folds), the evening
feeder peak was forecast with a mean absolute percentage error of 1.9% and per-phase load with a 7.6% weighted error; actual
load exceeded P90 5.1% of the time (nominal 10%), i.e. slightly conservative. These are on synthetic data and will be wider in
the field; Section 19 lists the validation we plan.</p>

<h2>7.3 Network model (C6)</h2>
<p>pandapower's unbalanced three-phase power flow{cite('pandapower')} runs on a feeder generated from the OpenStreetMap street graph{cite('osm')}
(transformer, minimum-spanning-tree LV lines, households at real building footprints, round-robin phase assignment with deliberate
imbalance). It returns per-bus, per-phase voltage and transformer and line loading, flags violations of the ±6% band, and gives
each block's safe charge and discharge limits from the busbar voltage sensitivity, so a battery action never creates a violation
of its own.</p>

<h2>7.4 Battery orchestration (C7)</h2>
<p>Both directions are water-filling problems on the phase's load curve: in the evening, find the level such that shaving
everything above it uses exactly the energy the block holds; at midday, find the level such that filling everything below it uses
exactly the energy the block can take — the lowest-load midday intervals are those where rooftop PV exports, so the block absorbs
local surplus first. The same solver runs day-ahead on the P50 forecast (the plan the operator approves) and every 15 minutes on
what is left of the window, with the phase load measured by the busbar CT and the forecast bias-corrected. A far-end overvoltage
triggers extra charging. SoC stays within 15–90%. In Pre-outage the floor rises to 1.5× the backup premises' allowance for the
announced cut; in Backup the floor drops to 5%. The architecture's reference-trajectory idea from Huang et al.{cite('huang')} and a
convex planner{cite('cvxpy')} remain as the next planner; the water-filling planner is its closed-form special case for a single
objective (flatten the peak) and is what we evaluated.</p>

<h2>7.5 Demand response (C8)</h2>
<p><b>Event days.</b> DFPO is measured in kW at a single instance in the peak period{cite('merc')}, so DR is reserved for the year's
most stressed days — the top 5% by forecast evening peak, about 22 events a year, 2 hours each. Tata Power-DDL called 12–16 events a
year, three to four a month in April–September{cite('tata_ddl')}.</p>
<p><b>Automated programmes.</b> 70% of pump owners (21 homes) enrol a smart plug: every day their pump moves from 18:00 to the
midday hour with the most forecast solar surplus, with a 5% household override. 60% of AC owners (19 homes) enrol an IR blaster:
on event days the AC cycles to 40% for two hours, pre-cools in the two hours before (30% of the curtailed energy) and rebounds
slightly after (20%), with a 10% override. Each participating home is paid ₹75 per event; each pump home ₹{A['smart_dr']['pump_fee_rs_per_month']}/month.</p>
<p><b>SMS / IVR offers.</b> For homes without devices, a contextual bandit chooses a flat offer of ₹0 (appeal), ₹25, ₹50 or ₹100 per
event, maximising expected value of the cut (kW at ₹2,000/kW-yr spread over the year's events plus the ₹7.92/kWh evening premium)
minus the expected payout. We use disjoint LinUCB{cite('li_linucb')} — one ridge regression per offer level — with context features
including window load, appliance ownership, whether a shop is open during the window, and the household's separate response
rates to paid offers and to appeals. It is pre-trained on a randomised pilot at real event times and offer cadence, then serves
greedily with a small exploration bonus. Offers respect a 4-per-month cap and a 3-day gap; 10% of eligible homes are held out at
random to verify the programme's causal effect. Reduced energy rebounds by 50% after the window.</p>
<p><b>Calibration against Indian pilots.</b> Our simulated SMS acceptance is {sms_acc:.0f}% against Tata Power-DDL's 56% average
participation (with ₹250 payouts){cite('tata_ddl')} — deliberately conservative. Our automated AC events deliver ≈{ac_kw_event/150:.2f} kW
per connected customer, inside the 0.03–0.16 kW range measured by BYPL and AEEE's automated DR pilot{cite('bypl')}.</p>

<h2>7.6 Outages and backup (C9) and operating modes</h2>
{fig('07_mode_state_machine.png', 'Operating modes, triggers and owning component. C7 owns Normal and Pre-outage because it holds the forecasts; C9 owns Backup and Restoration because it holds the events.', '62%')}
<p>Power-fail messages from the six sensors are classified by pattern: all sensors dark → upstream fault; one phase → phase fault;
one far-end only → local. Inverters anti-island automatically (IEC 62116 / IEEE 1547){cite('iec62116')} — this is inverter behaviour,
not an orchestrator decision — and then supply only their backup ports. C9 allocates each phase's available energy (SoC down to
5%, spread over the expected outage) to premises by priority (health 16 A, water 10 A, livelihood/education 6 A) and on
restoration re-transfers them in batches to avoid a cold-load surge. Figures 6–8 show the three scenarios.</p>
{fig('06a_sequence_day_ahead.png', caption='Sequence — peak day: forecast, plan, approval, offers and device schedules, live re-solve, settlement.')}
{fig('06b_sequence_load_shedding.png', caption='Sequence — announced load shedding: notice, Pre-outage, cut, anti-islanding, backup by priority, staggered restoration.')}
{fig('06c_sequence_unplanned_fault.png', caption='Sequence — unplanned upstream fault: detection and scope from sensors, backup from remaining SoC, recommendation to the DISCOM.')}

<h2>7.7 Settlement and pooling (C12)</h2>
<p>Settlement runs once a day when meter data for the previous day arrives. Each household's reduction is its IPMVP Option C
baseline (same window over similar non-event days, adjusted by that day's pre-event usage) minus actual consumption; the random
holdout tests whether the programme caused the reduction and whether baselines are biased{cite('ipmvp','caiso_dr','imperial_tata')}. Revenue
is the two-part contract (Section 13) plus backup fees. Payouts are sized backwards from realised revenue: a share α = {alpha:.0%}
forms the payout budget; contractual DR payments are paid first; the rest funds Stream 1 (households exporting solar while the
blocks charge, pro-rata) and Stream 2 (a rebate of the blocks' evening discharge, allocated down a participation-ranked queue
with a per-household cap). If claims exceed the budget they are scaled pro-rata, never promised. No payment is ever negative.
This follows sonnenCommunity's pooled model rather than a per-kWh auction{cite('sonnen','proposal')} (Section 9).</p>

<h2>7.8 Recommendations (C13) and explainability (C15)</h2>
<p>When local resources cannot close a gap — typically undervoltage along long lines, which a battery at the busbar cannot fix —
LEO sends the DISCOM a structured recommendation (e.g. raise the tap on phase B, 16:15–21:00, with forecast and residual
evidence), and for outages, the detected scope. The operator console pairs each predicted event with its reasons, the actions
LEO took and why, and the outcome compared with the forecast.</p>
"""))

    # ------------------------------------------------- 8 Standards & integration
    std = [
        ["DLMS/COSEM (IS 15959, IEC 62056), IS 16444", "Meter data model", "Synthetic meter files keyed by OBIS codes, released on the SLA schedule by a mock endpoint", "Household AMI data via the DISCOM's MDMS / India Energy Stack"],
        ["India Energy Stack (Beckn, W3C DID, Verifiable Credentials)", "Consented data exchange", "Mock endpoint with the same schema", "Consented meter-data exchange with DISCOMs (four pilot DISCOMs)"],
        ["MQTT", "Gateway message bus", "Mosquitto broker for all telemetry", "Same"],
        ["LoRaWAN IN865–867, ChirpStack", "Sensor radio", "Simulated ChirpStack payloads", "Sensors to the gateway concentrator; no SIM cost; works offline"],
        ["Modbus TCP, SunSpec models", "Inverter control", "pymodbus mock server, three unit IDs", "Three single-phase hybrid inverters; OpenEMS drivers where available"],
        ["Modbus RTU (RS-485)", "Premise meters", "Mock devices", "Dual-source meters with current limiters"],
        ["IEC 62116; IEEE 1547-2018", "Inverter grid behaviour", "Anti-islanding modelled per block", "Inverter certification; volt-var/volt-watt where supported"],
        ["IEC 61968/61970 (CIM)", "Network model exchange", "Asset naming in recommendations", "GIS / network-model exchange with the DISCOM"],
        ["OpenADR 2.0/3.0; IEEE 2030.5", "DR / DER signalling", "Not used", "DISCOM-to-aggregator event signals (both referenced by IES)"],
        ["IEC 60870-5-104, DNP3, IEC 61850", "DISCOM SCADA / substation", "Not used", "DISCOM-internal; LEO never writes to it"],
        ["IPMVP Option C", "Measurement and verification", "Baselines and holdout in settlement", "Verification by empanelled independent agencies (KERC)"],
        ["TRAI TCCCPR 2018 (DLT)", "Commercial SMS", "Mock SMS service", "Registered sender IDs and templates (₹5,900 one-time)"],
        ["DPDP Act 2023", "Personal data", "Per-purpose consent model", "Legal basis for data handling"],
        ["UPI", "Payments", "Recorded in ledger", "Monthly payouts"],
    ]
    out.append(("Standards, protocols and integration with existing infrastructure", f"""
<p>LEO uses the standards Indian utilities and vendors already use, so moving from the prototype to a pilot changes addresses and
drivers, not code. LEO talks to the simulator through MQTT, Modbus and REST exactly as it would to real devices{cite('arch')}.</p>
{table(["Standard / protocol", "Layer", "Prototype use", "Real-world integration"], std, "Standards and protocols: current prototype use and the integration path.")}
<p>References: DLMS/COSEM and Indian meter standards{cite('dlms')}; India Energy Stack{cite('ies','beckn')}; MQTT{cite('mqtt')}; LoRaWAN and
ChirpStack{cite('lorawan')}; SunSpec Modbus{cite('sunspec')} and OpenEMS{cite('openems')}; anti-islanding{cite('iec62116')}; CIM{cite('cim')};
OpenADR and IEEE 2030.5{cite('openadr')}; IPMVP{cite('ipmvp')}; TRAI DLT{cite('trai','dlt')}; DPDP{cite('dpdp')}; CEA smart-metering
interoperability{cite('cea_interop')}.</p>
<h2>8.1 Integration with what the DISCOM already has</h2>
<ul>
<li><b>Meter data:</b> via the MDMS export or India Energy Stack, with consumer consent — LEO never needs live household data.</li>
<li><b>Network data:</b> GIS / consumer indexing gives the consumer→DT→feeder mapping and sanctioned loads; LEO's generated feeder is
replaced by the DISCOM's model, exchanged in CIM terms.</li>
<li><b>Operations:</b> recommendations arrive on a dashboard, an API/webhook or SMS/email to the section office; the DISCOM acts in its
own SCADA/OMS. DR events can later arrive from the DISCOM over OpenADR or IEEE 2030.5.</li>
<li><b>DFPO reporting:</b> verified kW per DT per event, with baselines and holdout, in a form an independent verification agency
can reproduce.</li>
</ul>
"""))

    # ------------------------------------------------- 9 Design decisions
    dec = [
        ["Settlement ledger", "Plain relational ledger, Sonnen-style pooled model", "Blockchain / DLT settlement",
         f"Brooklyn Microgrid (LO3 Energy) stalled at pilot scale; its retrospective and an empirical comparison both found a database faster, cheaper and better suited to small, periodic, locally-trusted settlements{cite('brooklyn','comillas')}. UPPCL's P2P pilot was 12 participants with mock trading{cite('powerledger')}."],
        ["Price mechanism", "Pooled capacity with fair-access rules", "Iterative P2P double auction between households",
         f"Auction convergence assumes price-elastic participants (e.g. microgrids with storage); a household's rooftop export is nearly fixed regardless of price{cite('huang','proposal')}."],
        ["DR learning", "Contextual bandit (disjoint LinUCB)", "Full reinforcement learning",
         f"The question — which offer to make this household now — has no long action sequences; fatigue and budget are captured as context features. Bandits converge with far less data and are standard for which-offer problems{cite('li_linucb','proposal')}."],
        ["DR learner structure", "One model per offer level", "One shared linear model with level as a feature",
         "The shared model paid shops that are open during the event and non-responders, and gave ₹0 to price-sensitive homes; separate per-arm models learn each offer's own response."],
        ["DR mechanism", "Automated pump shifting and AC cycling + SMS offers", "SMS behavioural DR only",
         f"A typical low-income home cuts ~0.17 kW by SMS; automated devices give reliable kW and daily shifting, and cost ₹999–2,090{cite('smart_devices','bypl')}."],
        ["DFPO revenue unit", "kW at the peak instance + ₹/kWh evening energy", "₹/kWh only",
         f"MERC measures flexibility in kW at a single peak instance and penalises shortfall per MW{cite('merc')}; most DISCOM value is avoided evening purchase, so an energy component lets the DISCOM share it."],
        ["Battery planner", "Water-filling peak shave, re-solved every 15 minutes", "Greedy rule: discharge flat out when voltage dips",
         "The greedy rule emptied every block by 19:00 — before the worst interval. Water-filling spends energy on the highest-load intervals by construction."],
        ["Storage topology", "Three single-phase blocks", "One three-phase battery",
         "Indian LV networks are unbalanced; a balanced three-phase inverter cannot relieve one phase. Three 10–60 kWh second-life packs are also easier to procure than one large pack."],
        ["Battery chemistry", "Second-life LFP", "New packs; NMC",
         f"Second-life packs cost 40–60% of new{cite('wri_2l','pvmag_2l')}; LFP has far better thermal stability in tropical heat, and fade flattens under gentle stationary cycling{cite('frontiers_2l','mdpi_thermal','mdpi_degr')}."],
        ["Inverter size", "15 kW per phase", "7.5 kW per phase",
         "Research prices one 7.5–15 kW unit at the same ~₹1.2 L; the larger rating cut the verified peak by ~10 kW more for no extra capex."],
        ["Sensors", "Six LoRaWAN loggers + busbar CTs", "Cellular (GSM/NB-IoT) per sensor",
         f"No SIM cost, no dependence on mobile operators, works when the internet is down; ESMI documented GSM disruptions{cite('esmi')}."],
        ["Live data", "Operator's own sensors only", "Live household smart-meter data",
         f"AMISP SLAs deliver 15-minute data 8–24 h late; designs that assume it live fail in the field{cite('bses_sla','prayas_sm')}."],
        ["Control authority", "Advisory to the DISCOM; control only own assets", "Full ADMS/SCADA, closed-loop VVO/FLISR",
         f"Requires field-device authority a third party should not claim; we keep the DERMS aggregation idea with a clean interface boundary{cite('schneider')}."],
        ["Ownership", "Cooperative / NGO DF aggregator", "Distribution franchisee",
         f"Bhiwandi cut AT&amp;C from ~63% to 19–25% but several franchises were cancelled and the input-based model is criticised{cite('prayas_bhiwandi','indinfra_df','csep_df')}; the aggregator role exists in regulation today."],
        ["Simulation environment", "Own closed-loop LV simulator (pandapower)", "Grid2Op",
         f"Grid2Op targets transmission-level redispatch and French grids, not Indian LV demand-side flexibility{cite('grid2op')}."],
    ]
    out.append(("Design decisions and alternatives we rejected", f"""
<p>We grounded the design in one academic paper, two policy roadmaps and several industry deployments, and for each we recorded
what we took and what we left out{cite('proposal')}. The single filter every choice passed was: <i>minimal grid-level intervention,
genuine affordability, useful to the DISCOM without new capex</i>.</p>
<h2>9.1 What we adopted, and from where</h2>
<ul>
<li><b>Huang et al. (2025)</b> — scheduling storage against reference signals (a target SoC trajectory and a value benchmark) rather than
relying on accurate long-horizon forecasts; <i>not</i> its double-auction market{cite('huang')}.</li>
<li><b>Schneider EcoStruxure ADMS/DERMS</b> — the DERMS concept of aggregating many small distributed resources into one virtual
resource, with attention to reverse power flow, hosting capacity and non-wire alternatives; <i>not</i> SCADA, OMS or closed-loop
VVO/FLISR{cite('schneider')}.</li>
<li><b>sonnenCommunity</b> — pooled shared-capacity membership settled in an ordinary ledger; <i>not</i> Brooklyn Microgrid's blockchain
auction{cite('sonnen','brooklyn')}.</li>
<li><b>CEEW / Centre for Net Zero roadmap</b> — DT meters are rarely communicating, so design around day-late AMI; a synthetic smart-meter
repository is the recommended near-term strategy; consent architecture is a named gap; DFPOs and aggregators are the
near-term market{cite('ceew')}.</li>
<li><b>RMI / distribution franchisee literature</b> — kept as a future ownership option only{cite('prayas_bhiwandi','csep_df')}.</li>
</ul>
<h2>9.2 Decision register</h2>
{table(["Decision", "Chosen", "Rejected", "Why (evidence)"], dec, "Key design decisions, alternatives and the evidence that forced them.")}
"""))

    # ------------------------------------------------- 10 Evaluation method
    cfg_rows = [
        ["Baseline", "No battery, no DR, no backup circuit — same world, weather, outages and households"],
        ["LEO software + SMS DR", "Forecasting, network model, SMS offers; no storage, no devices"],
        ["LEO software + automated DR", "Adds smart pump plugs and AC IR blasters"],
        ["LEO, 3 × 15 / 30 / 45 / 60 kWh", "Battery blocks of each size (C/2 to C/4 inverters) + SMS DR + backup circuit"],
        ["LEO, 3 × 30 / 60 kWh + automated DR", "Battery + automated DR + SMS DR + backup circuit (recommended: 60 kWh)"],
    ]
    out.append(("Evaluation method and baseline", f"""
<p>No DISCOM data was available, so — as CEEW recommends while AMI matures{cite('ceew')} — we built a synthetic but physically grounded
world and ran LEO against it through the same interfaces it would use in the field.</p>
<h2>10.1 The simulated neighbourhood</h2>
<ul>
<li><b>Network:</b> a real Hoskote (Bengaluru Rural) street grid from OpenStreetMap, one 100 kVA DT (433 V, 250 V phase-to-neutral, ±6%
limits), 150 households at real building footprints across three phases{cite('osm')}. The DT has outgrown its rating, as many
peri-urban DTs have.</li>
<li><b>Households:</b> bottom-up appliance loads every 15 minutes (base load, fridge, temperature-driven AC and cooler, pumps, shop hours) —
median 210 kWh/month and 0.52 kW in the April evening; 25% with 2–6 kWp rooftop PV; 20% AC, 35% coolers, 15% pumps, 10% shops.
Hidden behavioural personas drive DR responses and are never visible to LEO{cite('emarc')}.</li>
<li><b>Weather:</b> one real year of hourly Open-Meteo weather for Hoskote (Sept 2025 – Sept 2026); PV from pvlib with each home's hidden
tilt, azimuth and soiling{cite('openmeteo','pvlib')}.</li>
<li><b>Outages:</b> about 50 per year — 70% upstream, 20% single-phase, 10% local — more frequent in the monsoon and the evening, lognormal
durations with a 35-minute median, literature-typical because ESMI raw data was not downloadable{cite('esmi')}; plus two announced
90-minute evening load-shedding cuts in each sampled summer week.</li>
<li><b>Latency:</b> meter data reaches LEO on the AMISP SLA schedule; LEO's live view is only its sensors, CTs and batteries.</li>
</ul>
<h2>10.2 Protocol</h2>
<p>We replay one week in every month (days 8–14) — 84 days covering summer, monsoon and winter — under each configuration, scaling
sums to a year. Every configuration sees the same world, weather, outage schedule and household draws, so differences come from
LEO's decisions alone. The load forecast is retrained in four quarterly folds with the evaluated weeks held out, so no forecast is
scored on data it was fitted to. Battery state of charge, transformer temperature and DR history carry over day to day within each
week. Transformer ageing uses the IEEE C57.91 thermal model with first-order top-oil dynamics and the ageing acceleration factor{cite('ieee_c5791')};
technical losses come from the power flow.</p>
{table(["Configuration", "What is enabled"], cfg_rows, "Configurations evaluated against the baseline.")}
<h2>10.3 Metrics</h2>
{table(["Metric", "Definition"], [
        ["M1 Overload trips / outage hours", "LV fuse trips from sustained conductor overload; customer-hours of outage, including expected DT-failure outages (failures/yr × 48 h repair × 150 homes)"],
        ["M2 Voltage quality", "Customer-hours outside ±6% (all buses) and far-end sensor minutes"],
        ["M3 Critical-premise availability", "Backup-served minutes ÷ outage minutes at registered premises"],
        ["M4 Evening peak reduction", "Transformer evening peak (16:30–23:30), average and worst day"],
        ["M5 Verified flexibility", "Mean cut in the DT's evening peak on the year's 10% most stressed days (the DFPO measurement days), kW"],
        ["Transformer ageing", "Equivalent ageing hours per year → expected life and failure rate"],
    ], "Reliability metrics.")}
"""))

    # ------------------------------------------------- 11 Reliability results
    cfgs = ["dr_only", "smart_only", "leo_15", "leo_60_15", "leo_60_15_smart"]
    rows = [["Baseline (no LEO)", n(rel['evening_peak_kw_mean_base']), n(rel['evening_peak_kw_max_base']), "0", "0%",
             f"{n(rel['dt_failure_rate_pct_base'],1)}%", n(rel['overload_hours_base']), n(rel['undervoltage_customer_hours_base']), n(rel['loss_kwh_base'])]]
    for k in cfgs:
        r = R[k]["reliability"]
        rows.append([R[k]["label"], n(r['evening_peak_kw_mean']), n(r['evening_peak_kw_max']), n(R[k]['physical']['verified_peak_kw'], 1),
                     f"{n(r['critical_premise_availability_pct'] or 0)}%", f"{n(r['dt_failure_rate_pct'],1)}%", n(r['overload_hours']),
                     n(r['undervoltage_customer_hours']), n(r['loss_kwh'])])
    out.append(("Measurable reliability improvement", f"""
<p>All figures are per transformer per year, measured against the defined baseline — the identical world without LEO (Section 10).
The recommended configuration is three 60 kWh blocks on 15 kW inverters plus automated DR.</p>
<div class='kpis'>
 <div class='kpi'><div class='v'>{n(rel['critical_premise_outage_hours_served'])} h</div><div class='l'>premise-hours of outage kept powered ({n(rel['critical_premise_availability_pct'])}% of critical-premise outage time vs 0%)</div></div>
 <div class='kpi'><div class='v'>{n(rel['dt_failure_customer_hours_avoided'])}</div><div class='l'>customer-hours/yr of DT-failure outages avoided (failure rate {n(rel['dt_failure_rate_pct_base'],1)}% → {n(rel['dt_failure_rate_pct'],1)}%)</div></div>
 <div class='kpi'><div class='v'>{n(-pct(rel['evening_peak_kw_mean_base'], rel['evening_peak_kw_mean']))}%</div><div class='l'>lower average evening peak ({n(rel['evening_peak_kw_mean_base'])} → {n(rel['evening_peak_kw_mean'])} kW); worst day {n(rel['evening_peak_kw_max_base'])} → {n(rel['evening_peak_kw_max'])} kW</div></div>
 <div class='kpi'><div class='v'>{n(rel['overload_hours_base'])} → {n(rel['overload_hours'])} h</div><div class='l'>hours per year above rating; energy above rating {n(rel['overload_kvah_base'])} → {n(rel['overload_kvah'])} kVAh</div></div>
 <div class='kpi'><div class='v'>−{n(-pct(rel['undervoltage_customer_hours_base'], rel['undervoltage_customer_hours']))}%</div><div class='l'>customer-hours of undervoltage ({n(rel['undervoltage_customer_hours_base'])} → {n(rel['undervoltage_customer_hours'])})</div></div>
 <div class='kpi'><div class='v'>{n(rel['solar_absorbed_kwh'])} kWh</div><div class='l'>rooftop solar per year stored locally instead of exported ({n(100*rel['solar_absorbed_kwh']/rel['solar_export_kwh'])}% of export)</div></div>
</div>
{fig('04_energy_day.png', 'The intermittency bridge on the year&apos;s peak day (27 April): midday charging from rooftop solar, 20 pumps moved to 10:45, the evening discharge spread across the peak, and the automated AC event with SMS offers. The transformer peak falls from 186 kW to 116 kW.')}
{table(["Configuration", "Evening peak avg (kW)", "Worst day (kW)", "Verified kW (M5)", "Critical availability (M3)", "DT failure rate", "Hours overloaded", "Undervoltage cust-h (M2)", "Losses (kWh)"], rows, "Reliability by configuration, per transformer per year (selected configurations; Appendix B lists all).", "num")}
<h2>11.1 Reading the results</h2>
<ul>
<li><b>Outage hours.</b> LEO reduces outage hours through two channels: critical premises stay powered through faults and announced load
shedding ({n(rel['critical_premise_outage_hours_served'])} premise-hours a year), and the transformer's expected failure rate falls from
{n(rel['dt_failure_rate_pct_base'],1)}% to {n(rel['dt_failure_rate_pct'],1)}% a year, avoiding {n(rel['dt_failure_customer_hours_avoided'])} customer-hours of multi-day
replacement outages. M1 fuse trips did not occur in either arm on this feeder.</li>
<li><b>Supply during intermittency windows.</b> The evening ramp is exactly when solar disappears; LEO moves {n(rel['solar_absorbed_kwh']/1000,1)} MWh of
midday solar and {n(ph['pump_shift_kwh']/1000,1)} MWh of pump load across it each year and keeps the average evening peak below the rating.</li>
<li><b>Where storage matters and where DR matters.</b> Automated DR alone cuts the evening peak by about
{n(R['smart_only']['reliability']['evening_peak_kw_mean_base']-R['smart_only']['reliability']['evening_peak_kw_mean'])} kW at very low capex; storage is what
protects critical premises and the transformer; together they reach a {n(ph['verified_peak_kw'],1)} kW verified cut.</li>
</ul>
<div class='callout warn'><b>What LEO does not do, stated plainly.</b> It cannot prevent upstream faults or load shedding, only prepare for them and protect
registered premises; homes not on the backup circuit still lose supply. It cannot fix voltage drop along long LV lines from the
transformer end — the remaining undervoltage is escalated to the DISCOM as tap-change recommendations. The results come from a
synthetic world calibrated to public data and Indian pilots; field validation is the first step of the roadmap.</div>
"""))

    # ------------------------------------------------- 12 Ownership & O&M
    out.append(("Ownership, operations and maintenance model", f"""
<h2>12.1 Who owns and operates LEO locally</h2>
<p>A local cooperative, NGO or self-help-group federation registers with the DISCOM as a demand-flexibility aggregator — a role
recognised by the MERC 2024 and KERC 2026 regulations{cite('merc','mercom_kerc','aggregator')}. It owns the sensors, batteries, backup
circuit, devices and the LEO software licence, and runs a cluster of about 20 DTs — for example one BESCOM O&amp;M section or one gram
panchayat. Households are members: they consent, respond to offers, and receive payments; they own nothing and risk nothing.</p>
{table(["Actor", "Owns", "Controls", "Does not control", "Relationship"], [
        ["DISCOM", "LV network, DT, meters (via AMISP), meter data", "Switching, taps, crews, restoration", "Operator assets", "Buys verified flexibility under DFPO; receives recommendations; shares meter data with consent"],
        ["Operator (cooperative / NGO aggregator)", "Sensors, batteries, backup circuit, devices, LEO", "Its batteries, backup circuit, devices, messages, ledger", "DISCOM supply; household bills", "Runs LEO; earns DFPO payments; pays households"],
        ["Households (150 per DT)", "Appliances; consent", "Whether to respond or override", "Anything else", "Members; receive DR payments and pooled rewards"],
        ["Critical premises (6)", "Their loads", "—", "—", "Pay a backup subscription and per-kWh fee"],
        ["Independent verification agency", "—", "Verification", "—", "Reproduces LEO's baselines from DISCOM meter data"],
    ], "Actors, ownership and control.")}
<h2>12.2 Operations</h2>
<ul>
<li><b>Staff:</b> one skilled technician per cluster (Karnataka minimum wage for a skilled electrician, Zone 1: ₹32,145/month{cite('wages')}),
recruited locally — visual inspections, inverter filter cleaning, sensor and device swaps, enrolment drives, community relations.</li>
<li><b>Daily:</b> the operator reviews and approves tomorrow's plan in the console (about 10 minutes per DT), answers recommendations
status from the DISCOM, and handles household queries by phone/IVR.</li>
<li><b>Platform:</b> cloud hosting ≈₹5,000/month per cluster; DLT-registered SMS at ≈₹0.145 per message plus ₹5,900 one-time
registration{cite('research','dlt','sms_cost')}.</li>
</ul>
<h2>12.3 Maintenance</h2>
{table(["Asset", "Routine", "Life / replacement", "On failure"], [
        ["Second-life LFP packs + BMS", "Monthly BMS health and temperature review; temperature-aware depth of discharge in summer", "min(7 years, 2,500 equivalent full cycles) — about 7 years at 336 cycles/yr; replacement budgeted", "BMS limits or refuses commands; block isolated, others continue"],
        ["Hybrid inverters", "Quarterly filter cleaning, firmware updates", "10+ years", "Inverter reverts to safe default after a command timeout"],
        ["LoRa sensors and CTs", "Quarterly check; battery-backed for last-gasp", "5 years", "Gateway flags missing data; forecast-only operation"],
        ["Smart plugs / IR blasters", "Self-reporting; household-swappable", "5 years", "Household reverts to manual; excluded from events until fixed"],
        ["Backup circuit and premise meters", "Annual inspection with the DISCOM", "20+ years (cable)", "Premise falls back to grid only"],
        ["Edge gateway", "Remote monitoring; UPS", "5 years", "Cloud flags gateway offline; blocks run their last plan, then safe default"],
    ], "Maintenance plan.")}
<h2>12.4 Legal and regulatory basis</h2>
<ul>
<li><b>Demand flexibility:</b> KERC DF/DSM Regulations 2026 — targets, aggregator role, verification by empanelled agencies{cite('mercom_kerc','nie_kerc')}.</li>
<li><b>Backup circuit:</b> supplying other premises needs a licence unless exempt; Section 13 of the Electricity Act allows exemption for
cooperatives and NGOs, and Section 14 allows supply without a licence in notified rural areas — hence the preference for notified
rural or peri-urban sites{cite('ea2003')}.</li>
<li><b>Grid connection:</b> each block needs DISCOM connection approval and certified anti-islanding{cite('iec62116')}.</li>
<li><b>Data and messaging:</b> DPDP Act consent; TRAI DLT registration{cite('dpdp','trai')}.</li>
</ul>
<h2>12.5 Governance</h2>
<p>The household share α of revenue, the backup fee and the DR offer levels are set by the cooperative's members each year, within
the bounds the unit economics allow (Section 13). The ledger is open to members; payouts can never exceed realised revenue and are
never negative.</p>
"""))

    # ------------------------------------------------- 13 Unit economics
    capex_rows = [[k.replace("_", " ").capitalize(), lakh(v)] for k, v in op["capex"].items()] + [["<b>Total per DT</b>", f"<b>{lakh(op['capex_total'])}</b>"]]
    opex_rows = [[k.replace("_", " ").capitalize(), lakh(v)] for k, v in op["opex"].items()] + [["<b>Total per DT per year</b>", f"<b>{lakh(op['opex_total'])}</b>"]]
    rev_rows = [[k.replace("_", " ").capitalize(), lakh(v)] for k, v in op["revenue"].items()]
    pnl = rev_rows + [["<b>Revenue</b>", f"<b>{lakh(op['revenue_total'])}</b>"], ["Paid to households (DR first, then pooled share)", lakh(-op["payouts"])],
                      ["Operating costs", lakh(-op["opex_total"])], ["<b>Net cash per year</b>", f"<b>{lakh(op['net_cash_per_year'])}</b>"],
                      ["Payback on capex", f"{n(op['payback_years'],1)} years"], ["10-year NPV at 10% (battery replacement and salvage included)", lakh(op["npv"])]]
    discom_rows = [[k.replace("_", " ").capitalize(), lakh(v)] for k, v in dc.items() if k not in ("net", "max_rate_rs_per_kwh", "npv", "gross_saving")]
    discom_rows += [["<b>Gross saving</b>", f"<b>{lakh(gross)}</b>"], ["<b>Net after paying the operator</b>", f"<b>{lakh(dc['net'])}</b>"]]
    sens = [[s["case"], lakh(s["operator_npv"]), lakh(s["discom_net"]), n(s["payback_years"], 1) if s["payback_years"] else "—"] for s in rec["sensitivity"]]
    thr = [["DFPO shortfall penalty", f"≥ ₹{n(th['dfpo_penalty_rs_per_kw_year'])}/kW-yr", f"₹{n(A['dfpo']['penalty_rs_per_kw_year'])} (MERC)"],
           ["DISCOM evening power price", f"≥ ₹{th['peak_purchase_rs_per_kwh']:.2f}/kWh", f"₹{A['discom']['peak_purchase_rs_per_kwh']:.2f} (IEX cap)"],
           ["Second-life battery price", f"≤ ₹{n(th['battery_rs_per_kwh'])}/kWh", f"₹{n(A['capex']['battery_rs_per_kwh'])}"],
           ["Insurance + maintenance", f"≤ {th['om_insurance_pct_of_capex']:.1f}% of capex", f"{A['opex']['insurance_pct_of_capex']+A['opex']['maintenance_pct_of_capex']:.1f}%"]]
    out.append(("Unit economics and affordability", f"""
<p>Unit economics are computed by multiplying the simulated physical quantities (kW verified, kWh shifted, hourly grid import,
transformer ageing, backup minutes, DR offers and payouts) by prices in one assumptions file, each tagged with its source
(Appendix A). Recommended configuration: three 60 kWh blocks on 15 kW inverters plus automated DR, per transformer, per year, in a
cluster of {A['operating_model']['dts_per_operator']} DTs.</p>
<h2>13.1 Capital cost</h2>
{table(["Item (per DT)", "₹"], capex_rows, f"Capital cost. Battery at ₹{n(A['capex']['battery_rs_per_kwh'])}/kWh (second-life, 40–60% of new{cite('wri_2l','pvmag_2l')}); inverters ₹1.2 L per 7.5–15 kW unit; sensors + concentrator ₹30,000; gateway ₹25,000; backup meters + cabling ₹4,000 per premise{cite('research')}; IR blaster ₹999, smart plug bundle ₹2,090{cite('smart_devices')}.", "num")}
<h2>13.2 Operating cost</h2>
{table(["Item (per DT per year)", "₹"], opex_rows, "Operating cost. Technician and cloud shared across the cluster; M&amp;V agency cost sits in the DISCOM's DSM portfolio.", "num")}
<h2>13.3 Operator profit and loss</h2>
{table(["Line", "₹ per year"], pnl, f"Operator P&amp;L. Contract: ₹{n(A['dfpo']['payment_rs_per_kw_year'])}/kW-yr for {n(ph['verified_peak_kw'],1)} verified kW plus ₹{A['dfpo']['evening_energy_rs_per_kwh']:.2f} per verified evening kWh ({n(ph['flex_kwh'])} kWh/yr); energy settlement on the operator's ToD connection{cite('consumer_rules','sq_tod')}; backup fees.", "num")}
<h2>13.4 DISCOM</h2>
{table(["Line", "₹ per year"], discom_rows, f"DISCOM ledger vs no LEO. Power purchase is priced hour by hour — ₹10/kWh in 18:00–23:00, ₹1.91 at midday, ₹7.6 otherwise{cite('ranjith_iex','energymap')} — so round-trip losses, DR rebound and network losses are netted out. Failures valued at ₹5.05 L per 100 kVA DT{cite('research')}.", "num")}
<p>The DISCOM's <b>gross</b> saving is {lakh(gross)} per DT per year — {lakh(gross*1000)} per year across 1,000 overloaded DTs. It reduces
technical losses on this DT by {n(loss_saved)} kWh/yr ({n(100*loss_saved/rel['loss_kwh_base'],1)}%) and stops buying {n(ph['flex_kwh']/1000,1)} MWh of
evening power a year. Most of the gross saving flows to the operator under the contract because the operator carries the capital,
devices and household payments; the split is a negotiated point inside the deal zone below.</p>
{fig('05_money_flow.png', 'Money flow per transformer per year (₹ lakh): the DISCOM&apos;s gross saving, the contract payment to the operator and what the DISCOM keeps; the operator&apos;s revenue split into DR payments, the household pool, operating costs and capital recovery.')}
<h2>13.5 Is there a deal?</h2>
<p>Payments between the operator and the DISCOM cancel out, so a deal exists exactly when their combined value is positive. For the
recommended design the operator breaks even at ₹{op['break_even_rate_rs_per_kwh']:.2f} per verified evening kWh and the DISCOM gains up to
₹{dc['max_rate_rs_per_kwh']:.2f}; the contract rate of ₹{A['dfpo']['evening_energy_rs_per_kwh']:.2f} sits inside, and the combined 10-year value is
{lakh(rec['system_npv'])}. Smaller batteries and SMS-only DR do not close (Figure 11).</p>
{fig('09_deal_zone.png', 'Deal zone by configuration: the operator&apos;s break-even evening-energy rate (amber) against the most the DISCOM can pay (blue), on top of the ₹2,000/kW-yr DFPO capacity payment.')}
<h2>13.6 What has to stay true</h2>
{table(["Uncertain input", "Combined value stays positive if", "Assumed"], thr, "Thresholds, one at a time, at which the arrangement stops being worth doing.")}
{table(["Change", "Operator 10-yr NPV", "DISCOM net / yr", "Payback (yr)"], sens, "Sensitivity of the recommended design, one assumption at a time.", "num")}
<h2>13.7 Affordability for low-income households</h2>
{table(["Who", "Per year", "How"], [
        [f"Pump home ({hh['n_pump_homes']} homes)", lakh(hh['pump_home_fee_rs'] + hh['pump_home_tod_saving_rs'] + hh['streams_rs_per_household']), f"₹{A['smart_dr']['pump_fee_rs_per_month']}/month fee + {lakh(hh['pump_home_tod_saving_rs'])} ToD bill saving (if ToD reaches LT domestic) + pool share"],
        [f"AC home ({hh['n_ac_homes']} homes)", lakh(hh['ac_home_event_pay_rs'] + hh['streams_rs_per_household']), "₹75 per event ≈ 20 events + pool share"],
        ["Every other home", lakh(hh['streams_rs_per_household'] + hh['sms_dr_rs_per_household']), "Pool share (solar absorption and discharge rebates) + any SMS offers accepted"],
        ["Critical premise", f"backup at ₹{n(A['backup']['fee_rs_per_kwh'])}/kWh + ₹{A['backup']['subscription_rs_per_premise_month']}/month", "vs ₹15,000+ per kWh for a lead-acid home inverter replaced every 3–4 years, or a diesel genset"],
    ], "Household benefit. Upfront cost ₹0; electricity bills unchanged; no penalties.")}
<p>For comparison, Tata Power-DDL's pilot paid ₹250 per event (with ₹50/₹100 tiers) and ran 12–16 events a year{cite('tata_ddl')} — about
₹2,250 a year for a household attending nine events; LEO's participating homes earn in the same range. Households are paid from the
operator's realised revenue at a share α = {alpha:.0%}; a deal remains possible up to α ≈ {(op.get('max_alpha_for_deal') or 0):.0%}. A
low-income household that could not afford a ₹15,000 inverter gets reliability on its street and income, with nothing to buy{cite('research')}.</p>
"""))

    # ------------------------------------------------- 14 Expected impact
    out.append(("Expected impact", f"""
{table(["Per DT per year", "Per 1,000 overloaded DTs per year"], [
        [f"{lakh(gross)} DISCOM gross saving", lakh(gross * 1000)],
        [f"{n(ph['flex_kwh']/1000,1)} MWh of evening power not bought at ₹10/kWh", f"{n(ph['flex_kwh']/1000*1000/1000,1)} GWh"],
        [f"{n(ph['verified_peak_kw'],1)} kW verified flexibility", f"{n(ph['verified_peak_kw']*1000/1000,1)} MW towards DFPO"],
        [f"{n(loss_saved)} kWh technical losses avoided", f"{n(loss_saved*1000/1e6,2)} GWh"],
        [f"DT failure rate {n(rel['dt_failure_rate_pct_base'],1)}% → {n(rel['dt_failure_rate_pct'],1)}%", f"≈{n((rel['dt_failure_rate_pct_base']-rel['dt_failure_rate_pct'])*10)} fewer DT failures ({lakh((rel['dt_failure_rate_pct_base']-rel['dt_failure_rate_pct'])/100*1000*505000)})"],
        [f"{n(rel['dt_failure_customer_hours_avoided'])} customer-hours of failure outages avoided", f"{n(rel['dt_failure_customer_hours_avoided']*1000/1e3)} thousand customer-hours"],
        [f"{n(rel['critical_premise_outage_hours_served'])} premise-hours of critical supply kept", f"{n(rel['critical_premise_outage_hours_served']*1000/1e3)} thousand premise-hours"],
        [f"{n(rel['solar_absorbed_kwh']/1000,1)} MWh rooftop solar used locally", f"{n(rel['solar_absorbed_kwh']/1e6*1000,1)} GWh"],
        [f"≈{lakh(hh['pump_home_fee_rs'] + hh['pump_home_tod_saving_rs'] + hh['streams_rs_per_household'])} per participating household", f"{n(150*1000/1000)} thousand households reached"],
    ], "Impact, scaled linearly to 1,000 DTs similar to the simulated one.")}
<p><b>Context.</b> Overload drives about 29% of BESCOM's ~38,000 annual DT failures{cite('hindu_dt','dt_failure')}; Karnataka's DFPO target
is 0.5% of peak in FY 2026-27, rising to 2.0% by FY 2029-30{cite('mercom_kerc')}. Scaling assumes DTs as overloaded as the simulated one; on
healthier DTs the transformer-failure value is smaller, which is why we target overloaded peri-urban DTs first (Section 18).</p>
<p><b>Social.</b> Clinics, water supply, tuition centres and small shops keep running through cuts; households earn without buying
anything; the community's own rooftop solar powers its evening.</p>
"""))

    # ------------------------------------------------- 15 Innovation
    out.append(("Innovation", f"""
<ul>
<li><b>Built around day-late data.</b> Most designs assume live household data. LEO is engineered for the AMISP reality: plan a day
ahead from forecasts, correct live only from six cheap sensors and the batteries' own CTs{cite('bses_sla')}.</li>
<li><b>Per-phase everything.</b> Three single-phase second-life blocks, per-phase forecasts, per-phase safe limits from a three-phase
power flow — matched to unbalanced Indian LV networks.</li>
<li><b>One solver for plan and live.</b> The same water-filling problem produces the approved day-ahead plan and the 15-minute
correction, so the operator approves what actually runs and the battery cannot run empty before the peak.</li>
<li><b>DR designed for the DFPO.</b> Flexibility is bought where the regulation values it — kW at the peak instant — with automated
devices for reliable kW, daily pump shifting for energy, and a bandit that pays real money only where it buys a real cut.</li>
<li><b>A deal, not a subsidy.</b> A two-part contract (DFPO capacity + evening energy) derived from the DISCOM's own avoided costs, with
a deal zone the model computes and the operator and DISCOM can negotiate within.</li>
<li><b>Honest settlement.</b> Payouts sized backwards from realised revenue, random holdouts for causal verification, no blockchain
and no penalties.</li>
<li><b>Explainability as a feature.</b> Every predicted event has reasons; every action has a why; outcomes are compared with
forecasts — auditable by the operator, the DISCOM and the verification agency.</li>
<li><b>Evidence-first.</b> Every price is from a tariff order, market data, a regulator, a DISCOM report or an Indian pilot, or is flagged
for verification; the simulation is calibrated against Tata Power-DDL and BYPL pilot results{cite('tata_ddl','bypl')}.</li>
</ul>
"""))

    # ------------------------------------------------- 16 Prototype
    out.append(("Prototype: what we built and what we simplified", f"""
<p>The full system runs locally with one command (<code>docker compose up</code>): mock field devices, the edge gateway, the operator
cloud and the web consoles, against a closed-loop simulator. Recorded runs replay the year's peak day, an announced load-shedding day,
an unplanned outage and a sunny low-demand day, each with and without LEO.</p>
<h2>16.1 Technology stack</h2>
{table(["Layer", "Prototype", "Why"], [
        ["Simulation", "Python: numpy, pandas, pandapower (3-phase power flow), pvlib, osmnx, Open-Meteo history", "Open, Python-native, no commercial solver{0}".format(cite('pandapower','pvlib','osm','openmeteo'))],
        ["Forecasting", "LightGBM quantile models, pvlib with clear-sky calibration", "Fast, accurate on tabular data, quantiles for risk"],
        ["Planning", "Water-filling peak-shave planner; cvxpy/OSQP reference planner", "Closed form, explainable; convex upgrade path"],
        ["DR", "Disjoint LinUCB; persona simulator", "Learns with little data; personas never visible to LEO"],
        ["Field interfaces", "Mosquitto MQTT; pymodbus TCP/RTU mock battery and meters; mock SMS; mock IES", "Same protocols as the field"],
        ["Store and APIs", "PostgreSQL; FastAPI", "Relational ledger; simple REST"],
        ["Consoles", "Next.js; deck.gl map over satellite imagery{0}".format(cite('deckgl')), "One web app for operator and DISCOM"],
        ["Evaluation", "Year-sampled sweep across configurations; economics from one assumptions file", "Reproducible; prices change without re-simulation"],
    ], "Prototype technology stack.")}
<h2>16.2 Prototype versus real deployment</h2>
{table(["Area", "Prototype", "Real deployment"], [
        ["Household meter data", "Synthetic, released on the AMISP SLA schedule by a mock IES endpoint", "IES exchange or DISCOM export with consent"],
        ["Voltage sensors", "Simulated from power-flow voltages with noise and dropouts", "LoRa loggers (ESMI-style), hosted with consent"],
        ["Storage", "Battery models behind a pymodbus server", "Second-life packs, BMS and hybrid inverters over Modbus TCP"],
        ["Backup circuit", "Simulated premise meters", "Cable, changeover, dual-source meters; legal basis secured"],
        ["Network topology", "Generated from OSM street geometry", "DISCOM GIS / consumer indexing"],
        ["Household behaviour", "Hidden personas calibrated to pilots", "Real households"],
        ["Smart devices", "Modelled pump shift and AC cycling", "Commercial smart plugs and IR blasters via their APIs"],
        ["SMS / IVR", "Mock service", "DLT-registered provider; IVR in Kannada, Hindi, English"],
        ["Payouts", "Ledger only", "UPI"],
        ["DFPO revenue", "Contract rates in the assumptions file", "Contract under the DISCOM's DF programme"],
        ["Gateway", "Docker Compose on a laptop", "Raspberry Pi-class device on UPS at the battery site"],
    ], "What we simplified for the hackathon and what replaces it in the field.")}
<h2>16.3 Simplifications to be aware of</h2>
<ul>
<li>The neighbourhood is synthetic (real streets and weather, generated households); outage rates are literature-typical, not fitted to ESMI.</li>
<li>Load forecasts are scored on a synthetic world, so field accuracy will be lower.</li>
<li>DR responses come from calibrated personas; AC curtailment is modelled from appliance duty cycles.</li>
<li>Annual figures scale one week per month; load growth and battery fade over the horizon are modelled only through replacement.</li>
</ul>
"""))

    # ------------------------------------------------- 17 Data and model design
    out.append(("Data and model design", f"""
<p>All state lives in one PostgreSQL schema shared by the gateway and the cloud; every table carries a run identifier so recorded runs,
evaluations and the live system never mix. The ER overview (Figure 12) is generated directly from the schema definition.</p>
{table(["Group", "Tables", "Key fields", "Written by", "Read by"], [
        ["Network and registry", "neighbourhood, bus, line, household, appliance, sensor, battery_block, premise_backup, consent", "bus_id, phase, sanctioned load, PV kWp, critical class, per-purpose consent", "world builder / DISCOM GIS", "network model, DR engine, consoles"],
        ["Runs", "run", "run_id, LEO on/off, seed, period", "simulator / gateway", "every table (foreign key)"],
        ["Observations", "sensor_reading, meter_interval, battery_telemetry, premise_meter, event", "ts_end, voltage, supply present, import/export kWh (day-late), SoC, backup kWh, event kind", "sensors (MQTT), MDMS/IES, inverters (Modbus)", "gateway, settlement, consoles"],
        ["Forecast and network", "forecast, network_result, phase_limit", "P10/P50/P90 per phase, per-bus voltage and loading, is_forecast flag, safe kW", "forecasting, network model", "planner, explainability, recommendations"],
        ["Decisions", "plan, dispatch, mode_transition", "planned and actual kW, SoC, rule triggered, mode and owner, approval", "battery orchestrator, outage module", "settlement, consoles"],
        ["Demand response", "dr_event, dr_offer", "window, target kW, offer (₹), predicted/verified kWh, holdout flag", "DR engine, settlement", "bandit training, settlement"],
        ["Settlement", "ledger, period_revenue", "entry type, amount (paise, never negative), scaling factor, revenue lines, α", "settlement", "households, DISCOM, verifier"],
        ["Recommendations", "recommendation", "issue, action, phase, evidence, status (open → resolved)", "recommendation engine", "DISCOM dashboard"],
        ["Evaluation", "eval_sweep, eval_day, eval_economics", "configuration, day, metrics, priced results", "evaluation pipeline", "impact page, this document"],
    ], "Data model by group (PostgreSQL; contracts/ddl.sql).")}
{fig('10_data_model.png', caption='Entity-relationship overview of the core tables, generated from contracts/ddl.sql (the full 29-table diagram, 10_data_model_full.svg, is in the repository).')}
{table(["Model", "Inputs", "Output", "Validation"], [
        ["Load forecaster (LightGBM quantile)", "Weather forecast, calendar, holidays, lagged load (latency-respecting), phase static features", "P10/P50/P90 gross load per phase, 96 intervals", "Held-out quarterly folds: evening peak MAPE 1.9%, phase WAPE 7.6%, P90 exceedance 5.1%"],
        ["PV model (pvlib + calibration)", "Irradiance, temperature, installed kWp", "PV per home and phase", "Clear-sky midday fit on net-import data"],
        ["Network model (pandapower 3-phase)", "Bus loads, battery setpoints", "Voltages, loadings, violations, safe limits", "Power-flow convergence every interval"],
        ["Battery planner (water-filling)", "Forecast or measured phase load, SoC", "Setpoints per 15 minutes", "Energy conservation; never below reserve"],
        ["DR bandit (disjoint LinUCB)", "Household, engagement, event context", "Offer level and expected kWh per home", "Holdout comparison; offer-response cross-check against pilots"],
        ["Transformer ageing (IEEE C57.91)", "Phase loading, ambient temperature", "Hot-spot temperature, ageing hours", "Standard thermal model"],
    ], "Models.")}
"""))

    # ------------------------------------------------- 18 Roadmap
    out.append(("Implementation roadmap", f"""
{table(["Phase", "Timing", "Scope", "Exit criteria"], [
        ["0 — Prototype", "Done (hackathon)", "Full system on simulated world; year-sampled evaluation; unit economics", "This document"],
        ["1 — Shadow pilot", "Months 0–6", "One overloaded peri-urban DT in a notified rural/peri-urban BESCOM area; 6 sensors + CTs; consented meter data via MDMS/IES; LEO forecasts and recommends only", "Forecast accuracy on real data; sensor uptime; DISCOM acceptance of recommendations"],
        ["2 — Active pilot", "Months 6–15", "Install three second-life blocks, backup circuit (6 premises), smart plugs/IR blasters in enrolled homes; DR events in the April–September season; IPMVP verification with an empanelled agency", "Verified kW on DFPO days; critical-premise availability; payouts settled; safety approvals"],
        ["3 — Cluster", "Months 15–30", "20 DTs in one O&amp;M section under one cooperative operator; two-part DFPO contract with BESCOM; one technician", "Operator cash-positive; DT failures tracked against the section's history"],
        ["4 — Scale", "Year 3+", "Multiple clusters and DISCOMs; OpenADR/IEEE 2030.5 event signals from the DISCOM; convex planner; additional devices (geysers, EV chargers)", "DFPO portfolio share; replication playbook"],
    ], "Implementation roadmap.")}
<p><b>Site selection.</b> Start where the value is highest and the law clearest: DTs with a history of overload failures, in notified
rural or peri-urban areas where Section 14 allows the backup circuit, with high rooftop-solar or pump penetration{cite('ea2003')}.</p>
<p><b>Financing.</b> Capital can be raised against the DFPO contract; capital grants (state or central storage support, CSR, or DISCOM
co-funding from loss-reduction programmes) shorten payback further — a 30% grant raises the operator's 10-year NPV to
{lakh(next((s['operator_npv'] for s in rec['sensitivity'] if s['case'].startswith('30%')), None))}.</p>
"""))

    # ------------------------------------------------- 19 Future scope
    out.append(("Future scope and open items", f"""
<h2>19.1 What we propose happens in the real world next</h2>
<ul>
<li>Replace the synthetic world with real DT data: GIS topology, MDMS/IES meter history, measured sensor voltages; retrain and re-validate forecasts.</li>
<li>Field-validate DR: enrolment rates, override rates and kW per home for smart plugs and IR blasters, against the Tata Power-DDL and BYPL results{cite('tata_ddl','bypl')}.</li>
<li>Negotiate the two-part contract with BESCOM inside the computed deal zone; verify DFPO measurement rules under KERC.</li>
<li>Confirm the legal basis for the backup circuit (Section 13 exemption or Section 14 rural proviso; whether storage counts as generation){cite('ea2003')}.</li>
<li>Upgrade planning to the convex reference-trajectory planner with ToD costs and degradation{cite('huang','cvxpy')}; temperature-aware depth of discharge for second-life packs{cite('mdpi_degr')}.</li>
<li>Midday DR offers to absorb solar surplus (water heating, pumping) where overvoltage is the binding problem; EV charging via OCPP.</li>
<li>Receive DR events from the DISCOM over OpenADR / IEEE 2030.5; publish verified kW to the DISCOM's DSM cell automatically.</li>
<li>Multi-operator, cross-neighbourhood settlement — the point at which distributed-ledger guarantees might start to earn their cost{cite('proposal')}.</li>
<li>Federated learning for forecasting across DTs without moving household data.</li>
</ul>
<h2>19.2 Open items</h2>
<p>Assumptions still marked <i>to verify</i> in Appendix A — notably battery cycle and calendar life, insurance and maintenance cost,
aggregator registration cost, cluster size, pump fee and AC event payment, enrolment shares and AC curtailment depth — are the ones a
pilot measures first. Section 13.6 lists the thresholds each must stay within for the arrangement to remain worth doing.</p>
"""))

    # ------------------------------------------------- 20 Team
    out.append(("Team", """
<p><i>[Team member names, roles and backgrounds to be added.]</i></p>
<table><thead><tr><th>Name</th><th>Role in LEO</th><th>Background</th></tr></thead><tbody>
<tr><td>[Name]</td><td>[e.g. architecture, simulation and gateway]</td><td>[ ]</td></tr>
<tr><td>[Name]</td><td>[e.g. forecasting, DR engine, settlement and consoles]</td><td>[ ]</td></tr>
</tbody></table>
"""))

    # ------------------------------------------------- Appendices
    out.append(("Appendix A — Assumptions register", f"""
<p>Every price and cost used in Section 13, with its source or status. “Sourced” = research report, regulation, market data or web
source; “to verify” = an assumption a pilot should confirm. Physical quantities come from the simulation, not from this table.</p>
{table(["Section", "Assumption", "Value", "Source / note", "Status"], c['assumptions_rows'](), "Assumptions register (economics.yaml).")}
"""))
    allrows = []
    for r in E["results"]:
        rl, o, d, p = r["reliability"], r["operator"], r["discom"], r["physical"]
        allrows.append([r["label"], n(rl["evening_peak_kw_mean"]), n(p["verified_peak_kw"], 1), f"{n(rl['critical_premise_availability_pct'] or 0)}%",
                        lakh(o["capex_total"]), n(o["payback_years"], 1) if o["payback_years"] else "—", lakh(o["npv"]), lakh(d["gross_saving"]),
                        lakh(r["system_npv"])])
    out.append(("Appendix B — All configurations compared", f"""
{table(["Configuration", "Evening peak (kW)", "Verified kW", "Critical availability", "Capex", "Payback (yr)", "Operator NPV", "DISCOM gross / yr", "Combined NPV"], allrows,
        f"All configurations against the baseline (evening peak {n(rel['evening_peak_kw_mean_base'])} kW). Operator figures at the assumed contract rate and α = {alpha:.0%}.", "num")}
"""))
    return out
