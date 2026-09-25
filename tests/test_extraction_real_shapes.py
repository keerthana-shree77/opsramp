"""Extraction against the attribute names a live OpsRamp instance returns.

Taken from a diagnostic dump of an HPEONEVIEW_SERVERHARDWARE resource: the
firmware arrives as tags whose names combine a label and an internal key
("System ROM_SystemRomActive"), and the payload also contains decoys - the
redundant ROM, the ESXi iLO driver, the ilorest tool, power-supply, TPM and
processor microcode firmware.
"""
from firmware.extractor import build_asset, process_resource

DL360 = {
    "id": "c3706639",
    "hostName": "dc01aesx0001-ilo.acme01.loc",
    "resourceName": "dc01aesx0001-ilo.acme01.loc",
    "ipAddress": "192.0.2.171",
    "deviceType": "HPEONEVIEW_SERVERHARDWARE",
    "resourceType": "HPEONEVIEW_SERVERHARDWARE",
    "bios": {
        "biosVersion": "2.80",
        "systemModel": "ProLiant DL360 Gen11",
        "systemManufacturer": "HPE",
        "systemSerial": "CZA0000002",
    },
    "tags": [
        {"name": "FirmwareVersion", "value": "1.73 Dec 04 2025"},
        {"name": "iLO 6_SystemBMC", "value": "1.73 Dec 04 2025"},
        {"name": "MP Firmware Version", "value": "1.73 Dec 04 2025"},
        {"name": "System ROM_SystemRomActive", "value": "U54 v2.80 (01/29/2026)"},
        {"name": "Redundant System ROM_SystemRomBackup", "value": "U54 v2.72 (11/27/2025)"},
        {
            "name": "Server Platform Services (SPS) Firmware_SPSFirmwareVersionData",
            "value": "6.1.4.215.0",
        },
        {"name": "Intelligent Provisioning_Intelligent Provisioning", "value": "4.35.4"},
        {"name": "ilo-driver_ilo: ilo driver", "value": "800.10.9.1.4-1OEM.800.1.0.20613240"},
        {
            "name": "ilorest-component_ilorest-component: ilorest tool",
            "value": "800.7.1.0.25-1OEM.802.0.0.22380479",
        },
        {"name": "Power Supply Firmware_PowerSupplies", "value": "2.01"},
        {"name": "TPM Firmware_TrustedPlatformModule20", "value": "1.512"},
        {"name": "Processor 1 PUcode Firmware_IntelCFR", "value": "0x38000060"},
        {"name": "HPE Smart Storage Energy Pack 1 Firmware_SmartStorageEnergyPack",
         "value": "0.70"},
    ],
}


def extract(document, category="all"):
    asset, components, raws = process_resource(document, "SAP-acme01", "t1", category)
    return asset, {c.component_key: c for c in components}, raws


def test_oneview_server_hardware_is_a_dl_server_not_a_hypervisor():
    """The decoy that broke everything: tags mention VMware, the model does not.

    Misreading this as ESXi makes the tool look for an ESXi build and ignore
    the BIOS, iLO, SPS and IP tags that are present.
    """
    asset, _flat = build_asset(DL360, "SAP-acme01", "t1")
    assert asset.platform_key == "hpe_dl"
    assert asset.category == "server"
    assert asset.model == "ProLiant DL360 Gen11"
    assert asset.manufacturer == "HPE"


def test_all_four_dl_components_are_found():
    _asset, components, _raws = extract(DL360)
    assert components["bios"].installed_version == "U54 v2.80"
    assert components["ilo"].installed_version == "1.73"
    assert components["sps"].installed_version == "6.1.4.215.0"
    assert components["intelligent_provisioning"].installed_version == "4.35.4"


def test_redundant_rom_is_not_taken_as_the_bios():
    _asset, components, _raws = extract(DL360)
    assert components["bios"].installed_version != "U54 v2.72"
    assert "Backup" not in components["bios"].source_attribute


def test_ilo_driver_and_ilorest_are_not_taken_as_ilo_firmware():
    _asset, components, _raws = extract(DL360)
    assert not components["ilo"].installed_version.startswith("800.")


def test_decoy_firmware_tags_are_not_used():
    """Power supply, TPM, microcode and energy-pack firmware are not tracked."""
    _asset, components, _raws = extract(DL360)
    versions = {c.installed_version for c in components.values()}
    for decoy in ("2.01", "1.512", "0.70"):
        assert decoy not in versions


def test_sps_descriptor_loses_to_the_sps_firmware_version():
    """A host publishes both; the descriptor (1.2) is not a firmware version."""
    document = dict(DL360)
    document["tags"] = list(DL360["tags"]) + [
        {
            "name": "Server Platform Services (SPS) Descriptor_SPSFirmwareDescriptor",
            "value": "1.2 0",
        }
    ]
    _asset, components, _raws = extract(document)
    assert components["sps"].installed_version == "6.1.4.215.0"


def test_ilo5_and_ilo6_tag_names_both_resolve():
    for label in ("iLO 5_SystemBMC", "iLO 6_SystemBMC"):
        document = dict(DL360)
        document["tags"] = [{"name": label, "value": "3.18"}]
        _asset, components, _raws = extract(document)
        assert components["ilo"].installed_version == "3.18"


def test_model_is_found_deep_in_the_payload():
    """The model lives at bios.systemModel, not at the top level."""
    asset, _flat = build_asset(DL360, "SAP-acme01", "t1")
    assert asset.model_key == "dl360_gen11"


SUPERDOME_ONEVIEW = {
    "id": "1e53210d",
    "hostName": "192.0.2.184",
    "resourceName": "CZA0000001, Npar 0",
    "deviceType": "HPEONEVIEW_SERVERHARDWARE",
    "model": "HPE Superdome Flex 280",
    "manufacturer": "HPE",
    "tags": [{"name": "Bios Version_Unknown type", "value": "2.10.04"}],
}

VMWARE_HOST = {
    "id": "9c50437b",
    "resourceName": "dc01aesx1001.acme01.loc",
    "deviceType": "VMWAREHOST",
    "model": "Superdome Flex 280",
    "manufacturer": "HPE",
    "osName": "VMware ESXi 8.0.3 build-24784735",
}


def test_superdome_rmc_comes_from_the_bios_version_tag():
    """The only firmware value OpsRamp exposes for SD Flex.

    It is not the BIOS: the vendor matrix lists the SD Flex BIOS as
    8.160.2.2026xxxx, while this value is in the same x.yy.zz form the matrix
    uses for the RMC release.
    """
    asset, components, _raws = extract(SUPERDOME_ONEVIEW)
    assert asset.platform_key == "superdome_flex_280"
    assert components["rmc"].installed_version == "2.10.04"


def test_vmwarehost_is_a_hypervisor_not_the_superdome_it_runs_on():
    """deviceType VMWAREHOST outranks a model of "Superdome Flex 280"."""
    asset, components, _raws = extract(VMWARE_HOST)
    assert asset.platform_key == "vmware_esxi"
    assert components["esxi"].installed_version == "8.0.3"
    assert components["esxi"].installed_build == "24784735"


def test_esxi_build_decides_against_the_recipe():
    """8.0.3 build 25595708 vs the matrix's "8.0 U3k (Build 25595708)"."""
    from firmware.comparator import compare_versions
    from firmware.models import STATUS_UPDATED

    verdict = compare_versions("8.0.3", "8.0 U3k", "25595708", "25595708")
    assert verdict["status"] == STATUS_UPDATED


def test_a_real_esxi_resource_is_still_classified_as_esxi():
    document = {
        "id": "esx-1",
        "hostName": "dc01aesx0001.acme01.loc",
        "deviceType": "VMWARE_ESXI",
        "resourceType": "VMWARE_ESXI",
        "model": "ProLiant DL360 Gen11",
        "tags": [{"name": "ESXi Version", "value": "VMware ESXi 8.0.3 build-24022510"}],
    }
    asset, components, _raws = extract(document)
    assert asset.platform_key == "vmware_esxi"
    assert components["esxi"].installed_version == "8.0.3"
    assert components["esxi"].installed_build == "24022510"

# ----------------------------------------- saying which machine a row is about

# A Superdome Flex 280 nPartition is registered in OpsRamp under its
# management address with no hostname at all, so a rack of them lists as
# 192.0.2.181 ... 192.0.2.189 and nothing on the row says what they are.
# Read alongside the PDUs a few addresses further up the same subnet, they
# were taken for PDUs reported as servers. They are not: OpsRamp itself calls
# them "HPE Superdome Flex 280", and its resource name carries the chassis
# serial and the partition. That name is what makes the row readable, so it
# travels with the comparison.


def test_the_partition_is_named_even_without_a_hostname():
    from service import _compare_one
    from firmware.matcher import RecipeIndex

    asset, components, _raws = extract(SUPERDOME_ONEVIEW)
    assert asset.hostname == "192.0.2.184", "OpsRamp gives it no hostname"
    assert asset.resource_name == "CZA0000001, Npar 0"

    row = _compare_one(components["rmc"], RecipeIndex([]), "at_least")
    assert row.resource_name == "CZA0000001, Npar 0", (
        "without this the report identifies the machine by an IP alone"
    )


def test_a_power_distribution_unit_is_not_a_server():
    """The two sit in one subnet, so only the payload tells them apart."""
    pdu = {
        "id": "pdu-1",
        "hostName": "192.0.2.201",
        "resourceName": "192.0.2.201",
        "resourceType": "Power",
        "model": "P9R53A",
        "manufacturer": "HPE",
        "generalInfo": {"firmwareVersion": "2.0.0.U"},
    }
    asset, components, _raws = extract(pdu)
    assert asset.platform_key == "pdu"
    assert asset.category == "pdu"
    assert components["pdu_firmware"].installed_version == "2.0.0.U"

    superdome, _components, _raws = extract(SUPERDOME_ONEVIEW)
    assert superdome.platform_key == "superdome_flex_280"
    assert superdome.category == "server"


def test_neither_of_them_is_ever_discarded():
    """Both are equipment with firmware, whatever else the read drops."""
    from firmware.extractor import is_noise_resource

    assert not is_noise_resource(SUPERDOME_ONEVIEW)
    assert not is_noise_resource(
        {
            "id": "pdu-1",
            "hostName": "192.0.2.201",
            "resourceType": "Power",
            "model": "P9R53A",
            "generalInfo": {"firmwareVersion": "2.0.0.U"},
        }
    )
