"""Extraction tests against synthetic OpsRamp resource documents.

The payload shapes below mirror what the OpsRamp v2 resource endpoints return:
a flat summary plus ``attributes`` / ``customAttributes`` name-value lists and
nested hardware blocks.  Adjust them if your instance differs - they are the
fixture the extraction rules are tuned against.
"""
from firmware.extractor import (
    build_asset,
    extract_components,
    flatten,
    matches_category,
    process_resource,
)

DL380 = {
    "id": "res-1",
    "hostName": "srv001",
    "ipAddress": "10.20.30.40",
    "resourceType": "DEVICE",
    "deviceType": "SERVER",
    "manufacturer": "HPE",
    "model": "ProLiant DL380 Gen11",
    "attributes": [
        {"name": "System ROM", "value": "U54 v2.10 (03/09/2024)"},
        {"name": "iLO Firmware Version", "value": "6.10"},
        {"name": "SPS Firmware Version", "value": "1.90"},
        {"name": "Intelligent Provisioning Version", "value": "4.30"},
        {"name": "iLO IP Address", "value": "10.20.30.41"},
        {"name": "BIOS Date", "value": "2024-03-09"},
        {"name": "Serial Number", "value": "CZ12345678"},
    ],
}

SDFLEX = {
    "id": "res-2",
    "hostName": "sd-flex01",
    "resourceType": "DEVICE",
    "model": "Superdome Flex 280",
    "customAttributes": [
        {"attributeName": "RMC Firmware", "attributeValue": "3.10.5"},
        {"attributeName": "RMC IP", "attributeValue": "10.1.1.9"},
    ],
}

CSUS = {
    "id": "res-3",
    "hostName": "csus01",
    "model": "Compute Scale-up Server 3200",
    "hardware": {"managementController": {"Resource Management Controller": "2.5.1"}},
}

SWITCH = {
    "id": "res-4",
    "hostName": "switch01",
    "deviceType": "NETWORK_SWITCH",
    "manufacturer": "Aruba",
    "model": "Aruba CX 8325",
    "attributes": [{"name": "Firmware Version", "value": "10.4.1"}],
}

STORAGE = {
    "id": "res-5",
    "hostName": "stor01",
    "deviceType": "STORAGE_ARRAY",
    "model": "Alletra 9000",
    "attributes": [{"name": "Array OS Version", "value": "6.4.2"}],
}

ESXI = {
    "id": "res-6",
    "hostName": "esxi01",
    "nativeResourceType": "HostSystem",
    "model": "ProLiant DL380 Gen11",
    "attributes": [
        {"name": "Full Name", "value": "VMware ESXi 8.0.2 build-22380479"},
        {"name": "Build", "value": "22380479"},
    ],
}

NO_VERSION = {
    "id": "res-7",
    "hostName": "srv009",
    "model": "ProLiant DL380 Gen11",
    "attributes": [{"name": "Serial Number", "value": "CZ999"}],
}


def extract(document, category="all"):
    asset, components, raws = process_resource(document, "SAP01", "t1", category)
    return asset, {c.component_key: c for c in components}, raws


def test_flatten_handles_name_value_attribute_lists():
    flat = flatten(DL380)
    names = {key for _path, key, _value in flat}
    assert "System ROM" in names
    assert "iLO Firmware Version" in names


def test_dl_server_identification():
    asset, _flat = build_asset(DL380, "SAP01", "t1")
    assert asset.platform_key == "hpe_dl"
    assert asset.category == "server"
    assert asset.hostname == "srv001"
    assert asset.ip_address == "10.20.30.40"
    assert asset.model_key == "dl380_gen11"


def test_dl_server_components():
    _asset, components, _raws = extract(DL380)
    assert components["bios"].installed_version == "U54 v2.10"
    assert components["ilo"].installed_version == "6.10"
    assert components["sps"].installed_version == "1.90"
    assert components["intelligent_provisioning"].installed_version == "4.30"


def test_ilo_ip_address_is_not_mistaken_for_firmware():
    _asset, components, _raws = extract(DL380)
    assert components["ilo"].source_value == "6.10"
    assert "10.20.30.41" not in (components["ilo"].source_value or "")


def test_bios_date_is_not_mistaken_for_a_version():
    _asset, components, _raws = extract(DL380)
    assert "2024" not in components["bios"].installed_version


def test_superdome_flex_rmc():
    asset, components, _raws = extract(SDFLEX)
    assert asset.platform_key == "superdome_flex_280"
    assert components["rmc"].installed_version == "3.10.5"


def test_csus_rmc_from_a_nested_hardware_block():
    asset, components, _raws = extract(CSUS)
    assert asset.platform_key == "csus_3200"
    assert components["rmc"].installed_version == "2.5.1"


def test_switch_firmware():
    asset, components, _raws = extract(SWITCH)
    assert asset.platform_key == "switch"
    assert asset.category == "switch"
    assert components["switch_firmware"].installed_version == "10.4.1"


def test_storage_os_version():
    asset, components, _raws = extract(STORAGE)
    assert asset.platform_key == "storage"
    assert components["os_version"].installed_version == "6.4.2"


def test_esxi_version_and_build():
    asset, components, _raws = extract(ESXI)
    assert asset.platform_key == "vmware_esxi"
    assert asset.category == "esxi"
    assert components["esxi"].installed_version == "8.0.2"
    assert components["esxi"].installed_build == "22380479"


def test_missing_version_is_reported_not_invented():
    _asset, components, raws = extract(NO_VERSION)
    bios = components["bios"]
    assert bios.installed_version is None
    assert bios.detected is False
    assert "No attribute" in bios.detection_note
    assert any(record.raw_attribute_name == "(none)" for record in raws)


def test_raw_records_preserve_the_source_attribute():
    _asset, _components, raws = extract(DL380)
    selected = [r for r in raws if r.selected and r.component == "BIOS"]
    assert selected
    assert selected[0].raw_attribute_value == "U54 v2.10 (03/09/2024)"
    assert selected[0].installed_version == "U54 v2.10"


def test_category_filter():
    asset, _flat = build_asset(SWITCH, "SAP01", "t1")
    assert matches_category(asset, "all")
    assert matches_category(asset, "switch")
    assert not matches_category(asset, "server")


def test_filtered_out_resource_returns_nothing():
    asset, components, raws = extract(SWITCH, category="storage")
    assert asset is None
    assert components == {}
    assert raws == []


def test_unrecognised_platform_is_skipped():
    asset, _flat = build_asset({"id": "x", "hostName": "mystery01"}, "SAP01", "t1")
    assert asset.platform_key == ""
    assert not matches_category(asset, "all")


def test_detail_errors_are_carried_into_the_note():
    document = dict(NO_VERSION)
    document["_extraction_errors"] = ["detail endpoint returned HTTP 404"]
    _asset, components, _raws = extract(document)
    assert "HTTP 404" in components["bios"].detection_note
