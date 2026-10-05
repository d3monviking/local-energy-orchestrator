# Diagrams for the write-up

Each figure has an editable source next to its rendered `.svg` and `.png`.
Box-and-arrow diagrams are Mermaid (`.mmd`); the data figures are generated
from the simulation and economics outputs, so their numbers are real.

| # | Figure | Source | Shows |
|---|---|---|---|
| 1 | `01_system_context` | `.mmd` | Actors and the operator boundary (LEO commands only its own assets) |
| 2 | `02_layered_architecture` | `.mmd` | Field / edge gateway / cloud, C1–C15, protocol on each link |
| 3 | `03_data_flow_latency` | `.mmd` | Timing on every arrow: why LEO plans day-ahead and corrects live |
| 4 | `04_energy_day` | `charts.py` (Postgres runs) | Peak day load with/without LEO: battery, pump shift, DR event |
| 5 | `05_money_flow` | `charts.py` → `.mmd` (econ_year.json) | Who pays whom, ₹ lakh per transformer per year |
| 6 | `06a/b/c_sequence_*` | `.mmd` | Day-ahead plan, load shedding, unplanned fault |
| 7 | `07_mode_state_machine` | `.mmd` | Modes, triggers and owning component |
| 8 | `08_decision_pipeline` | `.mmd` | Forecast → network → plan → DR → approval → live → settle |
| 9 | `09_deal_zone` | `charts.py` (econ_year.json) | Operator break-even vs DISCOM maximum, per configuration |
| 10 | `10_data_model` | `gen_er.py` (contracts/ddl.sql) | Database schema |

Regenerate:

```
python docs/diagrams/gen_er.py
POSTGRES_PORT=5433 PYTHONPATH=. ../.venv/bin/python docs/diagrams/charts.py   # needs the recorded runs + eval/results/econ_year.json
node docs/diagrams/render.mjs                                                 # all .mmd -> .svg + .png (headless Chrome, Mermaid from CDN)
```
