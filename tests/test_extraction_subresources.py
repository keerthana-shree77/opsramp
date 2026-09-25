"""Extraction from the sub-resource endpoints.

Firmware is not on the resource document. It arrives from
/resources/{id}/{appAttributes,customAttributes,nativeAttributes,components,
tags,assets}, and custom attributes use a nested shape where the name and the
value sit at different levels:

    {"customAttribute": {"name": "..."}, "value": "..."}

SPS firmware in particular is published as a tag.
"""
from firmware.extractor import flatten, is_noise_resource, process_resource

# What inventory.fetch_detail produces once the sub-resources are merged.
DL380_MERGED = {
    "id": "res-1",
    "hostName": "srv001",
    "ipAddress": "10.20.30.40",
    "deviceType": "SERVER",
    "manufacturer": "HPE",
    "model": "ProLiant DL380 Gen11",
    "_extra": {
        "tags": [
            {
                "customAttribute": {
                    "name": "Server Platform Services (SPS) Firmware_"
                            "SPSFirmwareVersionData"
                },
                "value": "6.1.4.215.0",
            },
            {
                "customAttribute": {"name": "Asset Owner"},
                "value": "platform-team",
            },
        ],
        "nativeAttributes": [
            {"attributeName": "System ROM", "attributeValue": "U54 v3.00 (08/20/2026)"},
            {"attributeName": "iLO Firmware Version", "attributeValue": "1.77"},
            {"attributeName": "iLO IP Address", "attributeValue": "10.20.30.41"},
        ],
        "components": [
            {"name": "Intelligent Provisioning", "value": "4.31.5"},
        ],
    },
}

SDFLEX_MERGED = {
    "id": "res-2",
    "hostName": "sdflex01",
    "model": "Superdome Flex 280",
    "_extra": {
        "customAttributes": [
            {
                "customAttribute": {"name": "RMC Firmware Version"},
                "customAttributeValue": "2.16.04",
            }
        ]
    },
}


def extract(document, category="all"):
    asset, components, raws = process_resource(document, "SAP01", "t1", category)
    return asset, {c.component_key: c for c in components}, raws


def test_nested_custom_attribute_is_flattened():
    flat = flatten(DL380_MERGED)
    names = {key for _path, key, _value in flat}
    assert any("SPSFirmwareVersionData" in n for n in names)
    assert "System ROM" in names


def test_sps_is_read_from_a_tag():
    _asset, components, _raws = extract(DL380_MERGED)
    assert components["sps"].installed_version == "6.1.4.215.0"


def test_bios_and_ilo_from_native_attributes():
    _asset, components, _raws = extract(DL380_MERGED)
    assert components["bios"].installed_version == "U54 v3.00"
    assert components["ilo"].installed_version == "1.77"


def test_intelligent_provisioning_from_components():
    _asset, components, _raws = extract(DL380_MERGED)
    assert components["intelligent_provisioning"].installed_version == "4.31.5"


def test_ilo_ip_is_still_rejected():
    _asset, components, _raws = extract(DL380_MERGED)
    assert components["ilo"].installed_version != "10.20.30.41"


def test_rmc_from_nested_custom_attribute_value():
    _asset, components, _raws = extract(SDFLEX_MERGED)
    assert components["rmc"].installed_version == "2.16.04"


def test_source_attribute_names_the_subresource():
    _asset, components, _raws = extract(DL380_MERGED)
    assert "_extra" in components["sps"].source_attribute


def test_noise_resources_are_skipped():
    for device_type in ("port", "volume", "disk", "vi_datastore", "cluster"):
        assert is_noise_resource({"deviceType": device_type, "resourceName": "x"})
    assert is_noise_resource({"deviceType": "OTHER", "resourceName": "Logical Drive 1"})


def test_real_assets_are_not_treated_as_noise():
    assert not is_noise_resource(DL380_MERGED)
    assert not is_noise_resource({"deviceType": "SERVER", "resourceName": "srv001"})
    # A switch or PDU keeps its place even when the type looks generic.
    assert not is_noise_resource({"deviceType": "port", "resourceName": "core-switch01"})
    assert not is_noise_resource({"deviceType": "service", "resourceName": "ilo-srv001"})
