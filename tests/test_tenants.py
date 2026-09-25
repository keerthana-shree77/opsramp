"""Tenant listing, including the endpoint-shape fallback.

Different OpsRamp versions expose the client listing at different paths, and a
bare /clients answers GET with HTTP 405 because it is the create endpoint.
"""
import pytest

from opsramp.client import OpsRampError
from opsramp.tenants import list_tenants


class FakeConfig:
    OPSRAMP_PARTNER_TENANT_ID = "partner-1"
    OPSRAMP_CLIENTS_PATH = "/api/v2/tenants/{partner_id}/clients/search"
    OPSRAMP_CLIENTS_FALLBACK_PATHS = [
        "/api/v2/tenants/{partner_id}/clients/minimal",
        "/api/v2/tenants/{partner_id}/clients",
    ]
    TENANT_NAME_FILTER = ""


class FakeClient:
    """Answers each path from ``responses``; values may be an exception."""

    def __init__(self, responses, config=None):
        self.responses = responses
        self.config = config or FakeConfig()
        self.calls = []

    def paginate(self, path, **kwargs):
        self.calls.append(path)
        outcome = self.responses.get(path, OpsRampError("not found", status=404))
        if isinstance(outcome, Exception):
            raise outcome
        return iter(outcome)


CLIENTS = [
    {"uniqueId": "t-1", "name": "SAP Tenant 001", "activeStatus": "ACTIVE"},
    {"uniqueId": "t-2", "name": "Other Tenant", "activeStatus": "ACTIVE"},
]

SEARCH = "/api/v2/tenants/partner-1/clients/search"
MINIMAL = "/api/v2/tenants/partner-1/clients/minimal"
BARE = "/api/v2/tenants/partner-1/clients"


def test_primary_path_is_used_when_it_works():
    client = FakeClient({SEARCH: CLIENTS})
    tenants = list_tenants(client)
    assert [t["id"] for t in tenants] == ["t-2", "t-1"] or len(tenants) == 2
    assert client.calls == [SEARCH]


def test_405_falls_through_to_the_next_candidate():
    client = FakeClient(
        {SEARCH: OpsRampError("method not allowed", status=405), MINIMAL: CLIENTS}
    )
    tenants = list_tenants(client)
    assert len(tenants) == 2
    assert client.calls == [SEARCH, MINIMAL]


def test_400_falls_through_too():
    """A 400 means the endpoint did not accept our request shape."""
    client = FakeClient(
        {
            SEARCH: OpsRampError("bad request", status=400, detail="Invalid page size"),
            MINIMAL: CLIENTS,
        }
    )
    assert len(list_tenants(client)) == 2
    assert client.calls == [SEARCH, MINIMAL]


def test_failure_message_carries_the_server_explanation():
    client = FakeClient(
        {
            SEARCH: OpsRampError("bad", status=400, detail="Invalid page size"),
            MINIMAL: OpsRampError("bad", status=405),
            BARE: OpsRampError("bad", status=405),
        }
    )
    with pytest.raises(OpsRampError) as excinfo:
        list_tenants(client)
    message = str(excinfo.value)
    assert "Invalid page size" in message
    assert "HTTP 400" in message
    assert "HTTP 405" in message


def test_404_also_falls_through():
    client = FakeClient(
        {
            SEARCH: OpsRampError("nope", status=404),
            MINIMAL: OpsRampError("nope", status=405),
            BARE: CLIENTS,
        }
    )
    assert len(list_tenants(client)) == 2
    assert client.calls == [SEARCH, MINIMAL, BARE]


def test_real_errors_are_not_masked_by_the_fallback():
    """A 401 must surface immediately, not be retried against other paths."""
    client = FakeClient({SEARCH: OpsRampError("unauthorized", status=401)})
    with pytest.raises(OpsRampError) as excinfo:
        list_tenants(client)
    assert excinfo.value.status == 401
    assert client.calls == [SEARCH]


def test_all_candidates_rejected_gives_an_actionable_message():
    client = FakeClient(
        {
            SEARCH: OpsRampError("no", status=405),
            MINIMAL: OpsRampError("no", status=404),
            BARE: OpsRampError("no", status=405),
        }
    )
    with pytest.raises(OpsRampError) as excinfo:
        list_tenants(client)
    message = str(excinfo.value)
    assert "OPSRAMP_CLIENTS_PATH" in message
    assert SEARCH in message


def test_name_filter_is_applied():
    config = FakeConfig()
    config.TENANT_NAME_FILTER = "SAP"
    client = FakeClient({SEARCH: CLIENTS}, config=config)
    tenants = list_tenants(client)
    assert [t["name"] for t in tenants] == ["SAP Tenant 001"]


def test_missing_partner_id_is_reported():
    config = FakeConfig()
    config.OPSRAMP_PARTNER_TENANT_ID = ""
    with pytest.raises(OpsRampError, match="OPSRAMP_PARTNER_TENANT_ID"):
        list_tenants(FakeClient({}, config=config))


def test_duplicate_records_are_collapsed():
    client = FakeClient({SEARCH: CLIENTS + [dict(CLIENTS[0])]})
    assert len(list_tenants(client)) == 2
