"""Storing what was read from OpsRamp.

The reading is the expensive half of the tool and the half that does not move
when a recipe does, so it is kept on disk. What matters here is that it comes
back exactly as it went in - a reading that quietly loses a component would
produce a compliance figure nobody could trust - and that a damaged one is
refused rather than half-read.
"""
import zlib

import pytest

from firmware import serialise
from firmware.models import Asset, InstalledComponent, RawAttribute
from service import Extraction


def asset(asset_id="a1", model="DL380 Gen11"):
    return Asset(
        tenant="SAP-one", tenant_id="t1", asset_id=asset_id,
        hostname=f"{asset_id}.example", ip_address="10.0.0.1",
        manufacturer="HPE", platform="HPE DL", platform_key="hpe_dl",
        model=model, model_key="dl380_gen11", category="server",
        resource_name=asset_id, resource_type="DEVICE",
    )


def extraction(components=None, raw_rows=None, host=None):
    host = host or asset()
    return Extraction(
        tenant_id="t1",
        tenant_name="SAP-one",
        category=["all"],
        components=components if components is not None else [
            InstalledComponent(
                asset=host, component_key="bios", component="BIOS",
                installed_version="U54 v3.00", source_attribute="System ROM",
                source_value="U54 v3.00 08/20/2026", confidence=140,
            ),
            InstalledComponent(
                asset=host, component_key="ilo", component="iLO",
                installed_version="1.74", installed_build="",
                detection_note="",
            ),
        ],
        raw_rows=raw_rows if raw_rows is not None else [
            RawAttribute(tenant="SAP-one", asset_id="a1", component="BIOS",
                         raw_attribute_name="System ROM", raw_attribute_value="U54 v3.00",
                         installed_version="U54 v3.00", selected=True, score=140),
        ],
        assets_total=9,
        assets_matched=1,
    )


# ------------------------------------------------------------- round tripping


def test_a_reading_comes_back_as_it_went_in():
    original = extraction()
    restored = serialise.decode(serialise.encode(original))

    assert restored.tenant_id == "t1"
    assert restored.tenant_name == "SAP-one"
    assert restored.category == ["all"]
    assert restored.assets_total == 9
    assert restored.assets_matched == 1
    assert [c.component_key for c in restored.components] == ["bios", "ilo"]
    assert [c.installed_version for c in restored.components] == ["U54 v3.00", "1.74"]
    assert restored.components[0].source_value == "U54 v3.00 08/20/2026"
    assert restored.components[0].confidence == 140


def test_the_asset_behind_each_component_survives():
    restored = serialise.decode(serialise.encode(extraction()))
    first = restored.components[0].asset
    assert first.hostname == "a1.example"
    assert first.model == "DL380 Gen11"
    assert first.platform_key == "hpe_dl"
    assert first.category == "server"


def test_components_of_one_asset_still_share_it():
    """Stored once and rehydrated once: fifty components, one asset."""
    restored = serialise.decode(serialise.encode(extraction()))
    assert restored.components[0].asset is restored.components[1].asset


def test_raw_attributes_survive_so_the_export_still_works():
    restored = serialise.decode(serialise.encode(extraction()))
    assert len(restored.raw_rows) == 1
    assert restored.raw_rows[0].raw_attribute_name == "System ROM"
    assert restored.raw_rows[0].selected is True


def test_an_empty_reading_round_trips():
    restored = serialise.decode(serialise.encode(extraction(components=[], raw_rows=[])))
    assert restored.components == [] and restored.raw_rows == []


def test_it_is_plain_json_underneath():
    """Anyone can read back what the tool believed was installed."""
    import json

    document = json.loads(zlib.decompress(serialise.encode(extraction())))
    assert document["tenant_name"] == "SAP-one"
    assert document["components"][0]["v"] == "U54 v3.00"


# ------------------------------------------------------------------ refusals


def test_a_damaged_reading_is_refused_not_half_read():
    with pytest.raises(ValueError, match="decompressed"):
        serialise.decode(b"this is not compressed json")


def test_a_reading_from_another_format_is_refused():
    import json

    blob = zlib.compress(json.dumps({"format": 99, "components": []}).encode())
    with pytest.raises(ValueError, match="format"):
        serialise.decode(blob)


def test_a_component_pointing_at_no_asset_is_dropped():
    import json

    document = json.loads(zlib.decompress(serialise.encode(extraction())))
    document["components"].append({"a": 99, "k": "sps", "c": "SPS", "v": "1.0"})
    restored = serialise.decode(zlib.compress(json.dumps(document).encode()))
    assert [c.component_key for c in restored.components] == ["bios", "ilo"]


def test_an_unknown_field_does_not_stop_an_older_reading_loading():
    import json

    document = json.loads(zlib.decompress(serialise.encode(extraction())))
    document["assets"][0]["something_new"] = "ignored"
    restored = serialise.decode(zlib.compress(json.dumps(document).encode()))
    assert restored.components[0].asset.hostname == "a1.example"


# --------------------------------------------------------------- fingerprints


def test_the_same_reading_fingerprints_the_same():
    assert serialise.fingerprint(extraction()) == serialise.fingerprint(extraction())


def test_a_changed_version_changes_the_fingerprint():
    moved = extraction()
    moved.components[1].installed_version = "1.75"
    assert serialise.fingerprint(moved) != serialise.fingerprint(extraction())


def test_a_new_component_changes_the_fingerprint():
    host = asset()
    more = extraction(host=host)
    more.components.append(
        InstalledComponent(asset=host, component_key="sps", component="SPS",
                           installed_version="6.1.4")
    )
    assert serialise.fingerprint(more) != serialise.fingerprint(extraction())


def test_the_order_things_were_found_in_does_not_count_as_a_change():
    """A re-read that happens to return assets in another order has not moved."""
    shuffled = extraction()
    shuffled.components.reverse()
    assert serialise.fingerprint(shuffled) == serialise.fingerprint(extraction())


def test_a_changed_raw_attribute_is_not_a_change():
    """Only what a verdict depends on counts, not the audit trail behind it."""
    noisy = extraction()
    noisy.raw_rows[0].score = 7
    assert serialise.fingerprint(noisy) == serialise.fingerprint(extraction())
