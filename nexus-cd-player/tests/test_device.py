"""Device-discovery tests. No hardware required."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import config  # noqa: E402
from backend import device  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    handle = open(os.path.join(FIXTURES, name))
    try:
        return handle.read()
    finally:
        handle.close()


class ScanbusParsingTest(unittest.TestCase):

    def test_real_nexus_line(self):
        drives = device.parse_scanbus(load("icedax_scanbus_real.txt"))
        self.assertEqual(1, len(drives))
        drive = drives[0]
        self.assertEqual("2,0,0", drive.address)
        self.assertEqual("PLDS", drive.vendor)
        self.assertEqual("DVD+-RW DH-16ABS", drive.model)
        self.assertEqual("PD11", drive.revision)
        self.assertEqual("Removable CD-ROM", drive.kind)
        self.assertTrue(device.is_optical(drive))

    def test_full_cdrtools_layout(self):
        """Slot numbers, tabs, padded vendor fields and '*' empty slots."""
        drives = device.parse_scanbus(load("icedax_scanbus_full_layout.txt"))
        self.assertEqual(1, len(drives), "empty '*' slots must be skipped")
        self.assertEqual("2,0,0", drives[0].address)
        self.assertEqual("PLDS", drives[0].vendor,
                         "padded vendor field must be stripped")
        self.assertEqual("DVD+-RW DH-16ABS", drives[0].model)

    def test_no_optical_device(self):
        drives = device.parse_scanbus(load("icedax_scanbus_no_drive.txt"))
        self.assertEqual(1, len(drives))
        self.assertFalse(device.is_optical(drives[0]))
        self.assertEqual([], device.select_optical(drives))

    def test_empty_output(self):
        self.assertEqual([], device.parse_scanbus(""))

    def test_selection_prefers_lowest_address(self):
        text = (
            "\t3,0,0\t300) 'ASUS' 'BW-16D1HT' '3.02' Removable CD-ROM\n"
            "\t2,0,0\t200) 'PLDS' 'DVD+-RW DH-16ABS' 'PD11' Removable CD-ROM\n"
        )
        optical = device.select_optical(device.parse_scanbus(text))
        self.assertEqual(["2,0,0", "3,0,0"], [d.address for d in optical])

    def test_dvd_only_model_still_counts_as_optical(self):
        text = "\t1,0,0\t100) 'HL-DT-ST' 'DVDRAM GH24NSD1' 'LN00' Removable\n"
        drives = device.parse_scanbus(text)
        self.assertTrue(device.is_optical(drives[0]))


class ClassifyScanbusTest(unittest.TestCase):
    """Discovery decisions, testable without a drive attached."""

    def test_optical_drive_present(self):
        result = device.classify_scanbus(load("icedax_scanbus_real.txt"))
        self.assertEqual(device.DRIVE_PRESENT, result.status)
        self.assertEqual("2,0,0", result.drive.address)
        self.assertTrue(result.drive.discovered)

    def test_no_optical_drive_is_no_drive_by_default(self):
        """Fallback is off by default, so a scan finding nothing means
        NO_DRIVE -- not a guessed address reported as DRIVE_PRESENT."""
        result = device.classify_scanbus(load("icedax_scanbus_no_drive.txt"))
        self.assertEqual(device.NO_DRIVE, result.status)
        self.assertIsNone(result.drive)

    def test_empty_scan_is_no_drive(self):
        self.assertEqual(device.NO_DRIVE,
                         device.classify_scanbus("").status)

    def test_fallback_is_used_only_when_explicitly_enabled(self):
        text = load("icedax_scanbus_no_drive.txt")
        self.assertEqual(device.NO_DRIVE,
                         device.classify_scanbus(text).status)
        result = device.classify_scanbus(text, allow_fallback=True,
                                         fallback_address="2,0,0")
        self.assertEqual(device.DRIVE_PRESENT, result.status)
        self.assertEqual("2,0,0", result.drive.address)
        self.assertFalse(result.drive.discovered)
        self.assertIn("not actually detected", result.message)

    def test_malformed_fallback_falls_through_to_no_drive(self):
        result = device.classify_scanbus(
            load("icedax_scanbus_no_drive.txt"),
            allow_fallback=True, fallback_address="garbage")
        self.assertEqual(device.NO_DRIVE, result.status)

    def test_permission_problem_is_error_when_the_node_exists(self):
        result = device.classify_scanbus(
            "icedax: Permission denied. Cannot open or use SCSI driver.\n", 1,
            sg_nodes=["/dev/sg0"], is_accessible=lambda path: False)
        self.assertEqual(device.ERROR, result.status)
        self.assertIn("permission", result.message.lower())

    def test_real_drive_wins_over_fallback(self):
        result = device.classify_scanbus(load("icedax_scanbus_real.txt"),
                                         allow_fallback=True,
                                         fallback_address="9,9,9")
        self.assertEqual("2,0,0", result.drive.address)
        self.assertTrue(result.drive.discovered)

    def test_raw_output_preserved_for_diagnosis(self):
        text = load("icedax_scanbus_no_drive.txt")
        self.assertIn("USB Flash Disk", device.classify_scanbus(text).raw)


class ConfigDefaultTest(unittest.TestCase):

    def test_fallback_is_disabled_by_default(self):
        """Guards the default itself: enabling it would make NO_DRIVE
        unreachable whenever a fallback address is configured."""
        self.assertFalse(
            config.ALLOW_FALLBACK_ADDRESS,
            "NEXUS_CD_ALLOW_FALLBACK must default to off "
            "(is it set in this environment?)")


class FallbackTest(unittest.TestCase):

    def test_fallback_address_parsed(self):
        drive = device._make_fallback("2,0,0")
        self.assertEqual("2,0,0", drive.address)
        self.assertFalse(drive.discovered)
        self.assertIn("fallback", drive.describe())

    def test_malformed_fallback_rejected(self):
        self.assertIsNone(device._make_fallback("sr0"))
        self.assertIsNone(device._make_fallback("2,0"))
        self.assertIsNone(device._make_fallback("a,b,c"))


# icedax prints the same generic tail whether the node is missing or merely
# unreadable, which is exactly why wording alone must not decide.
SCAN_FAILED_MISSING = (
    "icedax: No such file or directory. Cannot open or use SCSI driver.\n"
    "icedax: For possible targets try 'icedax -scanbus'. "
    "Make sure you are root.\n")

SCAN_FAILED_EACCES = (
    "icedax: Permission denied. Cannot open or use SCSI driver.\n"
    "icedax: For possible targets try 'icedax -scanbus'. "
    "Make sure you are root.\n")

SCAN_FAILED_UNKNOWN = (
    "icedax: Input/output error. Cannot send SCSI cmd via ioctl\n")


class DriveAbsenceVsPermissionTest(unittest.TestCase):
    """Regression for a real misdiagnosis on the Nexus.

    After a hot eject the USB/SCSI stack lost the drive completely: /dev/sg*
    vanished, yet the user was told they lacked permission on a device that no
    longer existed. Filesystem evidence, not icedax's wording, decides.
    """

    def test_A_no_sg_nodes_is_no_drive(self):
        result = device.classify_scanbus(
            SCAN_FAILED_MISSING, returncode=1, sg_nodes=[])
        self.assertEqual(device.NO_DRIVE, result.status)
        self.assertIn("not attached", result.message)
        self.assertNotIn("permission", result.message.lower())

    def test_B_existing_but_unreadable_node_is_permission_error(self):
        result = device.classify_scanbus(
            SCAN_FAILED_EACCES, returncode=1,
            sg_nodes=["/dev/sg0"], is_accessible=lambda path: False)
        self.assertEqual(device.ERROR, result.status)
        self.assertIn("permission", result.message.lower())
        self.assertIn("/dev/sg0", result.message)

    def test_C_unknown_failure_is_error(self):
        result = device.classify_scanbus(
            SCAN_FAILED_UNKNOWN, returncode=1,
            sg_nodes=["/dev/sg0"], is_accessible=lambda path: True)
        self.assertEqual(device.ERROR, result.status)
        self.assertIn("not recognised", result.message)

    def test_permission_wording_without_a_node_is_still_no_drive(self):
        """The exact misdiagnosis: EACCES wording, but nothing to open."""
        result = device.classify_scanbus(
            SCAN_FAILED_EACCES, returncode=1, sg_nodes=[])
        self.assertEqual(device.NO_DRIVE, result.status)
        self.assertNotIn("permission", result.message.lower())

    def test_make_sure_you_are_root_alone_proves_nothing(self):
        result = device.classify_scanbus(
            "icedax: Cannot open or use SCSI driver. Make sure you are root.\n",
            returncode=1, sg_nodes=["/dev/sg0"],
            is_accessible=lambda path: True)
        self.assertEqual(device.ERROR, result.status)
        self.assertNotIn("no permission", result.message.lower())

    def test_a_working_drive_beats_warning_text(self):
        text = load("icedax_scanbus_real.txt") + SCAN_FAILED_UNKNOWN
        result = device.classify_scanbus(text, returncode=1, sg_nodes=[])
        self.assertEqual(device.DRIVE_PRESENT, result.status)
        self.assertEqual("2,0,0", result.drive.address)

    def test_clean_scan_is_not_treated_as_a_failure(self):
        self.assertFalse(device.looks_like_scan_failure(
            load("icedax_scanbus_real.txt"), 0))

    def test_sg_node_discovery_is_a_glob_not_a_hardcoded_path(self):
        """After a USB reconnect the drive moved 2,0,0 -> 8,0,0; node
        numbering moves too, so /dev/sg0 must never be assumed."""
        self.assertIn("*", device.SG_NODE_GLOB)
        found = device.find_sg_nodes("/nonexistent/sg*")
        self.assertEqual([], found)


if __name__ == "__main__":
    unittest.main()
