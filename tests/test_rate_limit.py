"""Being rate-limited must not look like missing firmware.

A PDU publishes its firmware on the detail endpoint and nowhere else, so a
detail call lost to a 429 is the whole row. Written off, it reads "VERSION
NOT DETECTED" - which sends somebody to look at a PDU that was fine, and hides
the fact that nobody ever managed to ask.

The failure is a statement about how fast we asked, not about the resource:
a sweep runs up to MAX_WORKERS requests per account across
OVERVIEW_TENANT_WORKERS accounts at once, so when one worker is refused the
rest are about to be. Left alone they each burn their own retry budget inside
the same few seconds.
"""
import threading
import time

import pytest

from opsramp import inventory
from opsramp.client import RATE_LIMIT, OpsRampRateLimited, _RateLimitGate


@pytest.fixture(autouse=True)
def quiet_gate():
    """Each test gets its own pause state, and leaves none behind."""
    RATE_LIMIT.__init__()
    yield
    RATE_LIMIT.__init__()


# ------------------------------------------------------------------ the gate


def test_a_refusal_holds_every_request_back():
    gate = _RateLimitGate()
    delay = gate.hit()
    assert delay >= gate.FLOOR
    started = time.monotonic()
    gate.wait()
    assert time.monotonic() - started >= delay - 0.2


def test_retry_after_is_honoured_over_our_own_guess():
    gate = _RateLimitGate()
    assert gate.hit("3") == 3.0


def test_a_nonsense_retry_after_falls_back_to_the_floor():
    gate = _RateLimitGate()
    assert gate.hit("in a little while") >= gate.FLOOR


def test_a_sustained_limit_is_met_with_a_longer_pause():
    """A fixed drumbeat into a limit that has not lifted is just more load."""
    gate = _RateLimitGate()
    first = gate.hit()
    second = gate.hit()
    assert second > first


def test_the_pause_never_grows_without_bound():
    gate = _RateLimitGate()
    for _ in range(20):
        gate.hit()
    assert gate.hit() <= gate.CEILING


def test_one_worker_being_refused_holds_the_others():
    """The point of sharing it: forty-seven more refusals help nobody."""
    gate = _RateLimitGate()
    gate.hit("1")
    waited = []

    def worker():
        started = time.monotonic()
        gate.wait()
        waited.append(time.monotonic() - started)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(waited) == 4
    assert all(seen >= 0.5 for seen in waited), waited


def test_a_request_that_gets_through_resets_the_pause():
    gate = _RateLimitGate()
    gate.hit()
    gate.hit()
    time.sleep(0.01)
    gate._resume_at = 0.0  # the pause has passed
    gate.clear()
    assert gate.hit() == gate.FLOOR


# ------------------------------------------- a second pass at what was refused


class FakeClient:
    """Refuses the detail call the first time it is asked for each resource."""

    class config:
        MAX_WORKERS = 4
        DETAIL_FETCH_ENABLED = True
        OPSRAMP_RESOURCE_DETAIL_PATH = "/r/{tenant_id}/{resource_id}"
        OPSRAMP_RESOURCE_EXTRA_PATHS = ()

    def __init__(self, refuse_first=True, refuse_always=False):
        self.refuse_first = refuse_first
        self.refuse_always = refuse_always
        self.seen = {}
        self.lock = threading.Lock()

    def get(self, path, allow_missing=False):
        with self.lock:
            self.seen[path] = self.seen.get(path, 0) + 1
            count = self.seen[path]
        if self.refuse_always or (self.refuse_first and count == 1):
            raise OpsRampRateLimited(status=429, detail="Too Many Requests")
        return {"generalInfo": {"firmwareVersion": "2.0.0.U"}}


PDUS = [
    {"id": f"pdu-{n}", "hostName": f"hec55r1pdu0{n}", "resourceType": "Power",
     "model": "P9R53A"}
    for n in range(1, 5)
]


def test_a_rate_limited_detail_is_asked_for_again():
    client = FakeClient()
    results = inventory.fetch_details(client, "t1", PDUS)

    assert len(results) == len(PDUS)
    for document in results:
        assert document.get("generalInfo", {}).get("firmwareVersion") == "2.0.0.U", (
            "the firmware was there to be had; it was the asking that failed"
        )
        assert not document.get("_extraction_errors")


def test_each_one_is_only_retried_once():
    client = FakeClient()
    inventory.fetch_details(client, "t1", PDUS)
    assert all(count == 2 for count in client.seen.values()), client.seen


def test_a_resource_that_keeps_refusing_still_says_why():
    """It must not disappear, and it must not claim the PDU has no firmware."""
    client = FakeClient(refuse_always=True)
    results = inventory.fetch_details(client, "t1", PDUS)

    assert len(results) == len(PDUS)
    for document in results:
        errors = " ".join(document.get("_extraction_errors") or [])
        assert "429" in errors or "rate" in errors.lower(), errors


def test_a_clean_read_is_not_retried():
    client = FakeClient(refuse_first=False)
    inventory.fetch_details(client, "t1", PDUS)
    assert all(count == 1 for count in client.seen.values()), client.seen


def test_nothing_is_lost_or_duplicated_by_the_retry():
    client = FakeClient()
    results = inventory.fetch_details(client, "t1", PDUS)
    ids = sorted(str(d.get("id")) for d in results)
    assert ids == sorted(str(p["id"]) for p in PDUS)
