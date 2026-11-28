"""cloud/api.py — FastAPI app serving the web consoles and the household app.

Endpoints live in cloud/routes/, one router per owner so the two work
streams never edit the same file:

    shared.py     feeder, runs, network results, events, DR events, ledger,
                  households (changed only by agreement)
    operator.py   operator console: forecast/explanations, plan approval,
                  dispatch, sensors, backup circuit
    discom.py     DISCOM dashboard: recommendations, impact and economics
    household.py  household app: consent, DR eligibility, community, summary
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cloud import db
from cloud.routes import discom, household, operator, shared


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool()
    try:
        yield
    finally:
        await db.close_pool()


app = FastAPI(title="leo-cloud", lifespan=lifespan)

# Dev-mode CORS: the web app (localhost:3000) and this API (localhost:8030)
# are different origins. Access is read-only recorded-run data, nothing
# sensitive enough to warrant per-origin configuration in the prototype.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # GET for every read endpoint, POST for the real write actions
    # (plan approval, recommendation status advance, consent) — curl
    # doesn't enforce CORS preflight, so testing exclusively with curl
    # would never have caught this being GET-only.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "cloud"}


for module in (shared, operator, discom, household):
    app.include_router(module.router)
