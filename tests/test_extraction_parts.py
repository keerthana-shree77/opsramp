"""Parts of a machine are not machines.

OpsRamp types a server's power supply "Power" and its drive backplane and
drives "Storage", so left in they land in the PDU and Storage categories
beside the real rack PDUs and arrays. No recipe names them - nobody tracks
the firmware of a backplane - so they can never be scored, and a whole
category then reads as unscored because of parts that should not be in it.

Every document below is the real shape seen on a live account.
"""
import pytest

from firmware.extractor import is_noise_resource

# ------------------------------------------------------ parts, to be skipped

PARTS = [
    # A server power supply, named by its position in the chassis.
    {"resourceName": "rack1/chassis_u1/psu0", "resourceType": "Power",
     "model": "CRPS: Common Redundant Power Supply"},
    # The same thing named another way, with a part number for a model.
    {"resourceName": "PowerSupply_1", "resourceType": "Power",
     "model": "P44712-B21"},
    # A drive backplane.
    {"resourceName": "rack1/chassis_u1/storage_backplane3", "resourceType": "Other"},
    {"resourceName": "rack1/chassis_u1/storage_backplane1", "resourceType": "Other",
     "model": "8 SFF 24G x4NVMe/SAS UBM6 BC BP"},
    # Individual drives, named after their capacity.
    {"resourceName": "960GB SATA SSD", "resourceType": "Storage",
     "model": "MK000960GXPTH"},
    {"resourceName": "960GB 6G SATA SSD", "resourceType": "Storage",
     "model": "MK000960SXNXC"},
    {"resourceName": "1.92TB NVMe SSD", "resourceType": "Storage"},
    {"resourceName": "rack1/chassis_u1/fan2", "resourceType": "Other"},
    {"resourceName": "DIMM 5", "resourceType": "Other"},
]


@pytest.mark.parametrize("document", PARTS, ids=lambda d: d["resourceName"])
def test_a_part_of_a_machine_is_not_scanned(document):
    assert is_noise_resource(document) is True


def test_a_part_is_found_whichever_field_carries_its_name():
    """An id for the resource name, its position for the host name."""
    assert is_noise_resource(
        {"resourceName": "a1b2c3d4", "hostName": "rack1/chassis_u1/psu0",
         "resourceType": "Power"}
    ) is True


# --------------------------------------------------- real assets, to be kept

KEPT = [
    # A rack PDU. Typed "Power" exactly like a server power supply, so only
    # the name and model tell them apart.
    {"resourceName": "192.0.2.201", "resourceType": "Power",
     "model": "Enlogic PDU"},
    {"resourceName": "rack1/pdu1", "resourceType": "Power", "model": "Enlogic PDU"},
    {"resourceName": "P9R53A-01", "resourceType": "Power", "model": "P9R53A"},
    # Storage arrays.
    {"resourceName": "dc01astor01.example.local", "resourceType": "SAN",
     "model": "HP_3PAR"},
    {"resourceName": "HPE Alletra 9060", "resourceType": "SAN",
     "model": "HPE Alletra 9060"},
    # Servers and switches.
    {"resourceName": "dc01aesx0001-ilo", "resourceType": "Server",
     "model": "ProLiant DL380 Gen11"},
    {"resourceName": "sw-core-01", "resourceType": "Switch", "model": "8325-32C"},
]


@pytest.mark.parametrize("document", KEPT, ids=lambda d: d["resourceName"])
def test_a_real_asset_survives(document):
    assert is_noise_resource(document) is False


def test_an_array_is_not_mistaken_for_a_drive():
    """Only a name that *starts* with a capacity reads as one drive."""
    assert is_noise_resource(
        {"resourceName": "MSA 2060 SAS 12TB", "resourceType": "SAN"}
    ) is False


def test_a_power_supply_word_inside_a_longer_name_is_not_enough():
    """"psu" has to stand alone, or a host called "psunet01" would vanish."""
    assert is_noise_resource(
        {"resourceName": "psunet01.example.local", "resourceType": "Server"}
    ) is False


# ------------------------------------------------------- the "Other" catch-all

# OpsRamp types parts it has no better word for as "Other", and what lands
# there is DIMMs, processors, network adapters, HBAs, drive backplanes and
# entries whose "model" is a firmware version. Measured over a real estate of
# fifteen accounts, 564 components came from resources typed this way and not
# one could be scored. Left in they are counted as assets, drag every
# percentage down and fill the pending count.

OTHER_TYPED = [
    {"resourceName": "Processors", "resourceType": "Other",
     "model": "Intel(R) Xeon(R) Gold 6448H"},
    {"resourceName": "Network Adapter", "resourceType": "Other",
     "model": "BCM57504"},
    {"resourceName": "Broadcom NetXtreme-E Quad 25Gb SFP28 PCIe Etherne",
     "resourceType": "Other", "model": "230.1.123.0"},
    {"resourceName": "HPE SN1610Q 32Gb 2p FC HBA", "resourceType": "Other",
     "model": "2.10.00"},
    {"resourceName": "Complex Firmware", "resourceType": "Other",
     "model": "1.50.120-20241209_064202"},
    {"resourceName": "8 SFF 24G x4NVMe/SAS UBM6 BC BP", "resourceType": "Other",
     "model": "1.04"},
    {"resourceName": "DIMM slot 3", "resourceType": "Other",
     "model": "M321R8GA0PB0-CWMKH"},
]


@pytest.mark.parametrize("document", OTHER_TYPED, ids=lambda d: d["resourceName"][:28])
def test_the_catch_all_type_is_not_an_asset(document):
    assert is_noise_resource(document) is True


@pytest.mark.parametrize(
    "device_type", ["DOCKER_CONTAINER", "docker_container", "HPEONEVIEW_RESOURCE"]
)
def test_container_and_management_objects_are_not_assets(device_type):
    assert is_noise_resource(
        {"resourceName": "something", "resourceType": device_type}
    ) is True


# The types that carry everything this tool actually scores. If one of these
# were ever filtered, whole categories would vanish from the matrix in
# silence, which is the one failure worse than an unscored cell.
SCORING_TYPES = [
    "HPEONEVIEW_SERVERHARD",   # servers
    "VMWAREHOST",              # ESXi hosts
    "Switch",                  # switches
    "Power",                   # rack PDUs
    "Storage",                 # arrays
    "SAN",                     # arrays
    "VMware",
    "Server",
]


@pytest.mark.parametrize("device_type", SCORING_TYPES)
def test_a_type_that_carries_real_assets_is_never_filtered(device_type):
    assert is_noise_resource(
        {"resourceName": "dc01ar1a-sw1lf", "resourceType": device_type,
         "model": "8325-32C (JL636A)"}
    ) is False


# ----------------------------------------------------- drives named by position

# An array names the drives inside it by enclosure and slot - "1:11", "41:2" -
# and types them "Storage" like the array itself. Thirty-six of them in one
# account were being counted as arrays.


@pytest.mark.parametrize("name", ["1:1", "1:11", "41:2", "41:10", "12:9"])
def test_a_drive_named_by_its_slot_is_not_an_array(name):
    assert is_noise_resource(
        {"resourceName": name, "resourceType": "Storage",
         "model": "KCM77680P5xnETRI"}
    ) is True


def test_an_ip_address_is_not_a_slot():
    """The rule reads the raw name: normalising "192.0.2.201" would make it
    look like a slot, and that is a real PDU."""
    assert is_noise_resource(
        {"resourceName": "192.0.2.201", "resourceType": "Power",
         "model": "Enlogic PDU"}
    ) is False


@pytest.mark.parametrize("name", ["dc01stor01", "dc01astor01.other01.local", "sw-01"])
def test_a_hostname_is_not_a_slot(name):
    assert is_noise_resource(
        {"resourceName": name, "resourceType": "Storage",
         "model": "HPE Alletra 9060"}
    ) is False

# ------------------------------------------- things that carry no firmware

# The report looked as though it were missing firmware data on a huge scale:
# 431 rows saying a version could not be read. 392 of them were resources
# that have no firmware to read. Each one was counted as an asset, dragged
# its category's percentage down, and gave a reader nothing to act on.


def test_a_lun_is_not_a_storage_array():
    """An array presents hundreds of them; none has a version of its own."""
    assert is_noise_resource(
        {
            "id": "lun-1",
            "resourceType": "STORAGE_ARRAY_LUN",
            "resourceName": "eto_hana_data_301",
        }
    ), "360 LUNs were counted as arrays whose OS version could not be read"


def test_the_array_itself_is_still_read():
    assert not is_noise_resource(
        {
            "id": "arr-1",
            "hostName": "dc01astor01.acme01.loc",
            "resourceType": "Storage",
            "model": "HPE Alletra Storage MP",
        }
    )


def test_a_container_is_not_a_power_strip():
    """Its name contains "pdu", which used to be enough to rescue it."""
    assert is_noise_resource(
        {
            "id": "c-1",
            "resourceType": "DOCKER_CONTAINER",
            "resourceName": "pdu_metrics",
            "hostName": "pdu_metrics",
        }
    )


def test_a_switch_typed_as_a_port_is_still_rescued_by_its_name():
    """The reason the name rescue exists, and it has to keep working."""
    assert not is_noise_resource(
        {"deviceType": "port", "resourceName": "core-switch01"}
    )


def test_a_real_power_distribution_unit_is_untouched():
    assert not is_noise_resource(
        {
            "id": "pdu-1",
            "resourceType": "Power",
            "hostName": "dc01ar1a-pdu01.globex01.loc",
            "model": "P9R53A",
        }
    )


# ------------------------------------------------ a guest OS is not a host

NSX_EDGE = {
    "id": "nsx-1",
    "hostName": "dc01-w01-vc06-nsx01en04",
    "resourceType": "Linux",
    "model": "Other",
    "manufacturer": "Unknown",
    "generalInfo": {"resourceType": "Linux", "osName": "Ubuntu Linux (64-bit)"},
    "tags": [{"name": "vmware_pool", "value": "VCF-edge_dc01-w01-vc06"}],
}


def test_an_ubuntu_virtual_machine_is_not_an_esxi_host():
    """One tag reading "vmware_pool" was enough to make it one.

    31 NSX edge nodes were filed as hypervisors and then reported as ESXi
    hosts whose build could not be read - which is true, because an Ubuntu
    guest does not have one.
    """
    assert is_noise_resource(NSX_EDGE)


def test_a_real_esxi_host_is_not_caught_by_that_rule():
    """Its OS names no guest, and it has a model."""
    assert not is_noise_resource(
        {
            "id": "esx-1",
            "hostName": "dc01aesx1001.acme01.loc",
            "resourceType": "VMWAREHOST",
            "model": "ProLiant DL380 Gen11",
            "generalInfo": {"osName": "VMware ESXi 8.0.3 build-24784735"},
        }
    )


def test_a_physical_machine_running_linux_keeps_its_model():
    """The rule needs both: a guest OS *and* nothing identifying the hardware."""
    assert not is_noise_resource(
        {
            "id": "srv-1",
            "hostName": "dc01db01",
            "resourceType": "Linux",
            "model": "ProLiant DL380 Gen10 Plus",
            "generalInfo": {"osName": "Red Hat Enterprise Linux 8"},
        }
    ), "a ProLiant is a ProLiant whatever it boots"
