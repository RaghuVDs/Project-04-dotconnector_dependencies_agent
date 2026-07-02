"""Functional: the mock DotConnector loader + its FastAPI surface."""
import pytest
from fastapi.testclient import TestClient

from mock_dotconnector import server
from src.models import Finding

pytestmark = pytest.mark.functional


def test_load_payload_shape():
    payload = server.load_payload()
    assert payload["application_id"] == "600004364"
    assert isinstance(payload["findings"], list) and payload["findings"]


def test_load_findings_normalizes_to_models():
    findings = server.load_findings()
    assert len(findings) == 10
    assert all(isinstance(f, Finding) for f in findings)
    waived = [f for f in findings if f.waived]
    assert waived and waived[0].package == "pandas"


@pytest.fixture()
def client():
    return TestClient(server.app)


def test_dependencies_endpoint(client):
    r = client.get("/applications/600004364/dependencies")
    assert r.status_code == 200
    assert r.json()["application_name"] == "Enterprise Redaction Utility"


def test_findings_endpoint_all(client):
    r = client.get("/applications/600004364/findings")
    assert r.status_code == 200 and len(r.json()) == 10


def test_findings_endpoint_only_actionable(client):
    r = client.get("/applications/600004364/findings", params={"only_actionable": True})
    items = r.json()
    # actionable = not waived AND has a recommended_version
    assert items and all(not i["waived"] and i["recommended_version"] for i in items)
    assert len(items) < 10


def test_webhook_echoes_finding_id(client):
    r = client.post("/webhook", json={"finding_id": "F-0005"})
    assert r.status_code == 200
    body = r.json()
    assert body["received"] is True and body["finding_id"] == "F-0005"
