"""Whether the app believes the forwarding headers.

Behind a TLS-terminating proxy the connection to the app is plain HTTP and
the real scheme is in X-Forwarded-Proto. Reading that header is necessary
there and dangerous everywhere else - anything that can reach the app could
otherwise claim, in a header, to have arrived over HTTPS from another
address. So it is off unless the operator has counted the proxies.

The setting is read once, when the module is imported, so each case runs in
its own interpreter, and against a real request rather than a request
context - the middleware is only in the path of the former.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# What the app is asked, and what it reports back.
PROBE = """
import json, sys
sys.path.insert(0, %r)
import app as portal
from flask import request

# ProxyFix is WSGI middleware, so it only runs for a request that goes
# through the whole stack - which means a real one, not a request context.
@portal.app.route("/__probe")
def probe():
    return json.dumps({
        "scheme": request.scheme,
        "remote_addr": request.remote_addr,
        "host": request.host,
        "wrapped": type(portal.app.wsgi_app).__name__,
    })

response = portal.app.test_client().get(
    "/__probe",
    headers={
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "203.0.113.9",
        "X-Forwarded-Host": "firmware.example.invalid",
    },
    environ_base={"REMOTE_ADDR": "10.0.0.5"},
)
print(response.get_data(as_text=True))
""" % str(ROOT)


def ask(hops):
    """Import the app with this many trusted hops and report what it sees."""
    environment = dict(os.environ)
    environment.update(
        SECRET_KEY="test-secret",
        APP_USERNAME="tester",
        APP_PASSWORD="hunter2",
        OPSRAMP_BASE_URL="https://opsramp.invalid",
        OPSRAMP_PARTNER_TENANT_ID="partner",
        OPSRAMP_OAUTH_CLIENT_ID="id",
        OPSRAMP_OAUTH_CLIENT_SECRET="secret",
        DEFAULT_RECIPE_PATH="",
        OVERVIEW_REFRESH_ON_START="0",
        OVERVIEW_REFRESH_MINUTES="0",
        TRUSTED_PROXY_HOPS=str(hops),
    )
    finished = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True, text=True, env=environment, cwd=str(ROOT), timeout=120,
    )
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def unset():
    return ask(0)


@pytest.fixture(scope="module")
def one_hop():
    return ask(1)


def test_without_the_setting_the_headers_are_ignored(unset):
    """The default has to be the safe one: most deployments have no proxy."""
    assert unset["scheme"] == "http"
    assert unset["remote_addr"] == "10.0.0.5"
    assert unset["wrapped"] != "ProxyFix"


def test_a_forged_host_header_is_ignored_without_the_setting(unset):
    assert unset["host"] != "firmware.example.invalid"


def test_one_hop_reads_the_scheme_the_client_actually_used(one_hop):
    """Which is what makes a Secure cookie and an https:// redirect correct."""
    assert one_hop["scheme"] == "https"
    assert one_hop["wrapped"] == "ProxyFix"


def test_one_hop_reads_the_client_address_rather_than_the_proxy(one_hop):
    """Otherwise every line in the log is the proxy's address."""
    assert one_hop["remote_addr"] == "203.0.113.9"


def test_one_hop_reads_the_name_the_client_asked_for(one_hop):
    assert one_hop["host"] == "firmware.example.invalid"
