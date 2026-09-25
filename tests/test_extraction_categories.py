"""Switch, storage, PDU and guest-VM handling, from live attribute names.

Switches, storage arrays and PDUs all report through a plain
``generalInfo.firmwareVersion`` or a ``FirmwareVersion`` tag, which is far too
generic to accept for any platform - hence the platform-scoped aliases.
"""
import pytest

from firmware.extractor import extract_version_text, is_noise_resource, process_resource
from firmware.normalizer import CATEGORY_PDU, SELECTABLE_CATEGORIES

FC_SWITCH = {
    "id": "sw-1",
    "resourceName": "dc01ar1a-fcsw01",
    "deviceType": "Switch",
    "resourceType": "Switch",
    "model": "G620",
    "generalInfo": {"firmwareVersion": "9.2.2c1", "softwareVersion": "9.2.2c1",
                    "snmpVersion": "V1"},
}

ARUBA_SWITCH = {
    "id": "sw-2",
    "resourceName": "dc01ar1a-sw01",
    "deviceType": "Switch",
    "model": "8325-32C (JL636A)",
    "generalInfo": {"firmwareVersion": "10.13.1000"},
}

# A storage cage identifies itself only through its tags: deviceType is
# "Other" and the model ("DCN6") names no vendor. The management-profile
# adapter and a "Storage" tag are what place it in the storage category.
STORAGE_CAGE = {
    "id": "st-1",
    "resourceName": "cage0",
    "aliasName": "cage0",
    "deviceType": "Other",
    "resourceType": "Other",
    "model": "DCN6",
    "managementProfile": "hpe-alletra-gloc_mgmtprof_1d914d7b_globex01",
    "tags": [
        {"name": "FirmwareVersion", "value": "0906"},
        {"name": "Category", "value": "Storage"},
        {"name": "SerialNumber", "value": "CZA0000004"},
    ],
    "installedApp": {"version": "13.2.0"},
    "properties": {"appVersion": "13.2.0"},
}

PDU = {
    "id": "pdu-1",
    "resourceName": "192.0.2.203",
    "hostName": "192.0.2.203",
    "deviceType": "Power",
    "resourceType": "Power",
    "model": "P9R53A",
    "generalInfo": {"firmwareVersion": "2.0.0.U", "snmpVersion": "V2"},
}

GUEST_VM = {
    "id": "vm-1",
    "resourceName": "CGStestsles",
    "deviceType": "Linux",
    "resourceType": "Linux",
    "model": "VirtualMachine",
    "tags": [{"name": "VMware Tools", "value": "13.1.0.0"}],
}


def extract(document, category="all"):
    asset, components, raws = process_resource(document, "SAP01", "t1", category)
    return asset, {c.component_key: c for c in components}, raws


def test_fc_switch_firmware():
    asset, components, _raws = extract(FC_SWITCH)
    assert asset.platform_key == "switch"
    assert asset.category == "switch"
    assert components["switch_firmware"].installed_version == "9.2.2c1"


def test_aruba_switch_firmware():
    _asset, components, _raws = extract(ARUBA_SWITCH)
    assert components["switch_firmware"].installed_version == "10.13.1000"


def test_a_cage_is_excluded_even_though_it_looks_like_storage():
    """It carries a Storage tag and a FirmwareVersion, but it is an enclosure.

    The storage OS version is taken from the array instead - see
    test_3par_array_os_version and test_alletra_array_prefers_system_base_version.
    """
    assert is_noise_resource(STORAGE_CAGE)
    asset, components, raws = extract(STORAGE_CAGE)
    assert asset is None
    assert components == {}
    assert raws == []


def test_pdu_is_recognised_and_its_firmware_read():
    asset, components, _raws = extract(PDU)
    assert asset.platform_key == "pdu"
    assert asset.category == CATEGORY_PDU
    assert components["pdu_firmware"].installed_version == "2.0.0.U"


def test_pdu_is_a_selectable_category():
    assert CATEGORY_PDU in {value for value, _label in SELECTABLE_CATEGORIES}


def test_pdu_only_appears_under_its_own_category():
    asset, _components, _raws = extract(PDU, category="server")
    assert asset is None
    asset, components, _raws = extract(PDU, category="pdu")
    assert asset is not None
    assert components["pdu_firmware"].installed_version == "2.0.0.U"


def test_guest_vms_are_excluded():
    """A guest has no firmware; left in, it pollutes the ESXi category."""
    assert is_noise_resource(GUEST_VM)
    asset, components, raws = extract(GUEST_VM)
    assert asset is None
    assert components == {}
    assert raws == []


def test_esxi_hosts_are_not_excluded_as_guests():
    host = {
        "id": "h-1",
        "resourceName": "dc01aesx1001",
        "deviceType": "VMWAREHOST",
        "model": "Superdome Flex 280",
        "osName": "VMware ESXi 8.0.3 build-24784735",
    }
    assert not is_noise_resource(host)
    asset, components, _raws = extract(host)
    assert asset.platform_key == "vmware_esxi"
    assert components["esxi"].installed_build == "24784735"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("9.2.2c1", "9.2.2c1"),      # Brocade
        ("2.0.0.U", "2.0.0.U"),      # HPE PDU
        ("10.13.1000", "10.13.1000"),
        ("0906", "0906"),
        ("1.73 Dec 04 2025", "1.73"),  # trailing date dropped
    ],
)
def test_vendor_suffixes_are_preserved(value, expected):
    assert extract_version_text("switch_firmware", value) == expected


# --------------------------------------------------- storage arrays and cages

# Both array families publish "System Base Version"; Alletra also exposes
# osName ("9.6.5.19"). System Base Version is preferred because it is the
# granularity the recipe states its target in and the one both families share.
THREEPAR_ARRAY = {
    "id": "arr-1",
    "resourceName": "dc01astor01.acme01.loc",
    "deviceType": "Storage",
    "resourceType": "Storage",
    "model": "HP_3PAR",
    "tags": [{"name": "System Base Version", "value": "9.6.5"}],
    "installedApp": {"version": "13.2.0"},
}

ALLETRA_ARRAY = {
    "id": "arr-2",
    "resourceName": "dc01astor02.acme01.loc",
    "deviceType": "Storage",
    "resourceType": "Storage",
    "model": "HPE Alletra 9060",
    "osName": "9.6.5.19",
    "tags": [{"name": "System Base Version", "value": "9.6.5"}],
    "installedApp": {"version": "13.2.0"},
}

DRIVE_CAGE = {
    "id": "cage-1",
    "resourceName": "cage0",
    "aliasName": "cage0",
    "deviceType": "Other",
    "model": "DCN6",
    "tags": [{"name": "FirmwareVersion", "value": "0906"}],
}


def test_3par_array_os_version():
    asset, components, _raws = extract(THREEPAR_ARRAY)
    assert asset.category == "storage"
    assert components["os_version"].installed_version == "9.6.5"


def test_alletra_array_prefers_system_base_version():
    _asset, components, _raws = extract(ALLETRA_ARRAY)
    component = components["os_version"]
    assert component.installed_version == "9.6.5"
    assert "System Base Version" in component.source_attribute


def test_array_os_version_is_not_the_opsramp_agent_version():
    for document in (THREEPAR_ARRAY, ALLETRA_ARRAY):
        _asset, components, _raws = extract(document)
        assert components["os_version"].installed_version != "13.2.0"


def test_drive_cages_are_skipped_entirely():
    """A cage's FirmwareVersion is the enclosure's, not the array's OS."""
    assert is_noise_resource(DRIVE_CAGE)
    asset, components, raws = extract(DRIVE_CAGE)
    assert asset is None
    assert components == {}
    assert raws == []


@pytest.mark.parametrize(
    "name,expected",
    [
        ("cage0", True),
        ("cage 2", True),
        ("Cage12", True),
        ("storage-cage3", True),
        ("dc01astor01.acme01.loc", False),
        ("carriage-sw01", False),   # "carriage" is not a cage
        ("cagey01", False),
    ],
)
def test_cage_name_matching_has_boundaries(name, expected):
    # A real device type, so this measures the name rule and nothing else:
    # "Other" is itself ignored now, which would make every case come out true.
    assert is_noise_resource({"resourceName": name, "deviceType": "Storage"}) is expected


@pytest.mark.parametrize(
    "device_type",
    ["CLOUD_PROVIDER", "cloud_account", "RESOURCE_GROUP", "VI_RESOURCE_POOL",
     "vi_folder", "NAMESPACE"],
)
def test_account_and_container_objects_are_not_assets(device_type):
    """These have no model and were appearing as empty rows in a category."""
    assert is_noise_resource({"deviceType": device_type, "resourceName": ""})


@pytest.mark.parametrize(
    "document",
    [
        {"deviceType": "Storage", "resourceName": "dc01astor01", "model": "HP_3PAR"},
        {"deviceType": "Switch", "resourceName": "dc01ar1a-fcsw01", "model": "G620"},
        {"deviceType": "Power", "resourceName": "192.0.2.203", "model": "P9R53A"},
        {"deviceType": "HPEONEVIEW_SERVERHARDWARE", "resourceName": "srv01",
         "model": "ProLiant DL360 Gen11"},
    ],
)
def test_real_hardware_survives_the_noise_gate(document):
    assert not is_noise_resource(document)


def test_unidentified_asset_with_no_firmware_is_dropped():
    """It only matched the catch-all "server" word and exposed nothing.

    Kept, it produces a BIOS row and an iLO row with every column empty -
    which was the bulk of the noise in the server category.
    """
    stub = {"id": "x1", "resourceName": "some-server-stub", "deviceType": "Other"}
    asset, components, raws = process_resource(stub, "T", "t", "all")
    assert asset is None
    assert components == []
    assert raws == []


def test_a_generic_asset_that_does_expose_firmware_is_kept():
    document = {
        "id": "x2",
        "resourceName": "srv-generic",
        "deviceType": "SERVER",
        "tags": [{"name": "System ROM_SystemRomActive", "value": "U30 v2.90"}],
    }
    asset, components, _raws = extract(document)
    assert asset is not None
    assert asset.platform_key == "server_generic"
    assert components["bios"].installed_version == "U30 v2.90"


def test_a_generic_asset_with_a_model_is_kept_for_recipe_matching():
    document = {
        "id": "x3",
        "resourceName": "srv-known-model",
        "deviceType": "SERVER",
        "model": "ProLiant DL999 Gen9",
    }
    asset, _components, _raws = extract(document)
    assert asset is not None


ALLETRA_SERVER = {
    "id": "as-1",
    "resourceName": "dc01aco01-ilo.acme01.loc",
    "deviceType": "HPEONEVIEW_SERVERHARDWARE",
    "model": "Alletra Storage Server 4120",
    "manufacturer": "HPE",
    "tags": [
        {"name": "System ROM_SystemRomActive", "value": "U58 v2.22"},
        {"name": "iLO 6_SystemBMC", "value": "1.59 Apr 17 2024"},
        {"name": "Server Platform Services (SPS) Firmware_SPSFirmwareVersionData",
         "value": "6.1.4.47.0"},
        {"name": "Intelligent Provisioning_Intelligent Provisioning", "value": "4.33.5"},
    ],
}


def test_alletra_storage_server_is_a_server():
    asset, _components, _raws = extract(ALLETRA_SERVER)
    assert asset.platform_key == "alletra_server"
    assert asset.category == "server"


def test_alletra_server_tracks_bios_and_sps():
    _asset, components, _raws = extract(ALLETRA_SERVER)
    assert components["bios"].installed_version == "U58 v2.22"
    assert components["sps"].installed_version == "6.1.4.47.0"


def test_alletra_server_does_not_track_its_ilo():
    """The machine has an iLO and reports a version for it; it is not tracked.

    The vendor recipe states no iLO target for this platform. Its iLO used to
    be compared against the Gen11 servers' target instead, which the operator
    has since said not to do - so the component is not looked for at all,
    rather than being read and then reported as having nothing to compare
    against.
    """
    _asset, components, raws = extract(ALLETRA_SERVER)
    assert "ilo" not in components
    assert not any(raw.component == "iLO" for raw in raws)


def test_alletra_server_does_not_track_intelligent_provisioning():
    """The vendor recipe states no IP target for this platform."""
    _asset, components, _raws = extract(ALLETRA_SERVER)
    assert "intelligent_provisioning" not in components
