"""Composition entry point: the UNCHANGED Guardian app + the drift add-on.

This imports the existing FastAPI app exactly as-is and attaches the drift
routes onto it — no existing file is modified. Run the server with this module
instead of guardian.main to get agent/model drift alongside everything else:

    uvicorn guardian.app_drift:app --port 8090

Everything the normal server serves (dashboard, /agent, /task, /cluster,
/runtime, all /v1/* endpoints) keeps working identically; drift adds:

    GET /drift                 the drift dashboard section
    GET /v1/drift              agents + models + summary
    GET /v1/drift/agents       per-agent behavioural drift
    GET /v1/drift/models       per-model drift
    GET /v1/drift/summary      fleet drift rollup
"""
from __future__ import annotations

from .main import app          # the existing app, untouched
from .drift import register

register(app)
