"""A stand-in for the real DotConnector.

Provides two things:
  * load_findings(): read the JSON fixture directly (used by the POC run).
  * a FastAPI app exposing the same shape the real tool would, plus a webhook
    endpoint to mimic event-driven triggering ("a new finding appeared").

Run the API:  uvicorn mock_dotconnector.server:app --reload --port 8800
Then:         GET http://localhost:8800/applications/600004364/dependencies
"""
from __future__ import annotations

import json
from pathlib import Path

from src.models import Finding

FIXTURE = Path(__file__).parent / "findings.json"


def load_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def load_findings() -> list[Finding]:
    payload = load_payload()
    return [Finding.model_validate(f) for f in payload["findings"]]


# --- optional HTTP surface (only needed for the webhook/API demo) ---------- #
try:
    from fastapi import FastAPI, Request

    app = FastAPI(title="Mock DotConnector")

    @app.get("/applications/{app_id}/dependencies")
    def dependencies(app_id: str) -> dict:
        payload = load_payload()
        return payload

    @app.get("/applications/{app_id}/findings")
    def findings(app_id: str, only_actionable: bool = False) -> list[dict]:
        payload = load_payload()
        items = payload["findings"]
        if only_actionable:
            items = [f for f in items
                     if not f.get("waived") and f.get("recommended_version")]
        return items

    @app.post("/webhook")
    async def webhook(request: Request) -> dict:
        """Mimic DotConnector firing on a new finding. In prod this would
        trigger the GitHub Action via repository_dispatch."""
        body = await request.json()
        return {"received": True, "finding_id": body.get("finding_id"),
                "action": "would trigger remediation workflow"}

except ModuleNotFoundError:  # fastapi optional for the pure-CLI path
    app = None
