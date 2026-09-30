"""TOC parsing tests. No hardware required.

The real-hardware fixture (icedax_toc_real.txt) is captured on the Nexus by
tools/capture_fixtures.sh. Until it exists those tests skip loudly rather
than being written against an imagined output format.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import config  # noqa: E402
from backend import disc  # noqa: E402
from backend import errors  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

REAL_TOC = os.path.join(FIXTURES, "icedax_toc_real.txt")

# Start sectors observed on the physical 11-track test disc.
REAL_STARTS = [0, 23143, 39953, 40875, 63496, 65149, 82562, 100181, 107383,
               129223, 151830]
REAL_LEADOUT = 178442

# Durations icedax reported for that disc, as M:SS.FF.
REAL_DURATIONS = ["5:08.43", "3:44.10", "0:12.22", "5:01.46", "0:22.03",
                  "3:52.13", "3:54.69", "1:36.02", "4:51.15", "5:01.32",
                  "5:54.62"]


def load(name):
    handle = open(os.path.join(FIXTURES, name))
    try:
        return handle.read()
    finally:
        handle.close()


def real_disc():
    """The physical test disc, built from measured sectors.

    Deliberately does NOT go through parse_toc: the real icedax line format
    is not yet observed, and normalised stand-in text must not masquerade as
    a format fixture. This exercises the production sector arithmetic
    (build_disc) against numbers actually read off the Nexus.
    """
    entries = [(index + 1, start, True)
               for index, start in enumerate(REAL_STARTS)]
    return disc.build_disc(entries, REAL_LEADOUT)


class MsfFormattingTest(unittest.TestCase):

    def test_frames_not_decimals(self):
        # 23143 sectors / 75 = 308.573 s, which is 5:08.43 in MSF.
        self.assertEqual("5:08.43", disc.format_msf(23143 / 75.0))

    def test_total_disc_time(self):
        self.assertEqual("39:39.17", disc.format_msf(REAL_LEADOUT / 75.0))

    def test_plain_minutes_seconds(self):
        self.assertEqual("1:23", disc.format_ms(83.9))
        self.assertEqual("0:00", disc.format_ms(0))


class RealValuesTest(unittest.TestCase):
    """Sector arithmetic checked against numbers measured on the Nexus."""

    def setUp(self):
        self.disc = real_disc()

    def test_track_count(self):
        self.assertEqual(11, self.disc.track_count)
        self.assertEqual(1, self.disc.first_track)
        self.assertEqual(11, self.disc.last_track)

    def test_start_sectors(self):
        self.assertEqual(REAL_STARTS,
                         [t.start_sector for t in self.disc.tracks])

    def test_leadout(self):
        self.assertEqual(REAL_LEADOUT, self.disc.leadout_sector)

    def test_end_sectors_abut_next_start(self):
        for index, track in enumerate(self.disc.tracks[:-1]):
            self.assertEqual(self.disc.tracks[index + 1].start_sector,
                             track.end_sector + 1)
        self.assertEqual(REAL_LEADOUT - 1, self.disc.tracks[-1].end_sector)

    def test_durations_match_icedax(self):
        """The strongest check: our sector maths reproduces icedax's own
        per-track times exactly, for all 11 tracks."""
        computed = [disc.format_msf(t.duration_seconds)
                    for t in self.disc.tracks]
        self.assertEqual(REAL_DURATIONS, computed)

    def test_total_duration(self):
        self.assertEqual("39:39.17",
                         disc.format_msf(self.disc.total_duration_seconds))

    def test_get_track_and_has_track(self):
        self.assertEqual(40875, self.disc.get_track(4).start_sector)
        self.assertEqual(63496, self.disc.get_track(5).start_sector)
        self.assertTrue(self.disc.has_track(11))
        self.assertFalse(self.disc.has_track(12))
        self.assertFalse(self.disc.has_track(0))
        self.assertRaises(errors.InvalidTrackError, self.disc.get_track, 12)


class TrackAtOffsetTest(unittest.TestCase):
    """Automatic track advance: mapping elapsed time onto track boundaries
    without restarting icedax."""

    def setUp(self):
        self.disc = real_disc()

    def test_start_of_run(self):
        self.assertEqual(1, self.disc.track_at_offset(0.0, 1))

    def test_just_before_first_boundary(self):
        # Track 1 is 23143 sectors = 308.573 s.
        self.assertEqual(1, self.disc.track_at_offset(308.0, 1))

    def test_just_after_first_boundary(self):
        self.assertEqual(2, self.disc.track_at_offset(309.0, 1))

    def test_crosses_two_boundaries(self):
        # Track 3 is only 0:12.22 long, so 2 and 3 pass quickly.
        offset = (39953 + 10) / 75.0
        self.assertEqual(3, self.disc.track_at_offset(offset, 1))

    def test_run_starting_mid_disc(self):
        """Starting at track 6, elapsed time is relative to track 6's start."""
        self.assertEqual(6, self.disc.track_at_offset(0.0, 6))
        track6_len = (82562 - 65149) / 75.0
        self.assertEqual(7, self.disc.track_at_offset(track6_len + 1.0, 6))

    def test_past_end_clamps_to_last_track(self):
        self.assertEqual(11, self.disc.track_at_offset(99999.0, 1))


class SyntheticFixtureTest(unittest.TestCase):

    def test_icedax_verbose_track_lines(self):
        parsed = disc.parse_toc(load("icedax_toc_single_track.txt"))
        self.assertEqual(1, parsed.track_count)
        self.assertEqual(0, parsed.tracks[0].start_sector)
        self.assertEqual(14399, parsed.tracks[0].end_sector)
        self.assertTrue(parsed.tracks[0].is_audio)
        self.assertEqual("3:12.00", disc.format_msf(
            parsed.total_duration_seconds))

    def test_missing_cd_text_is_not_an_error(self):
        result = disc.classify_toc_output(load("icedax_toc_single_track.txt"))
        self.assertEqual(disc.AUDIO_DISC, result.status)

    def test_data_disc_is_unsupported(self):
        result = disc.classify_toc_output(load("icedax_toc_data_disc.txt"))
        self.assertEqual(disc.UNSUPPORTED_DISC, result.status)
        self.assertIn("CD-DA", result.message)

    def test_known_no_disc_phrase_with_nonzero_exit(self):
        result = disc.classify_toc_output(load("icedax_toc_no_disc.txt"), 1)
        self.assertEqual(disc.NO_DISC, result.status)

    def test_no_disc_detected_even_on_zero_exit(self):
        result = disc.classify_toc_output(load("icedax_toc_no_disc.txt"), 0)
        self.assertEqual(disc.NO_DISC, result.status)

    def test_unknown_failure_is_error_not_no_disc(self):
        """An unrecognised failure must not be guessed to be an empty tray.

        Calling a SCSI/cabling/firmware problem NO_DISC would hide a real
        fault behind a benign-looking state.
        """
        text = ("icedax: Input/output error. read toc header: "
                "scsi sendcmd: retryable error\n"
                "icedax: Cannot get next writable address\n")
        result = disc.classify_toc_output(text, 3)
        self.assertEqual(disc.ERROR, result.status)
        self.assertIn("3", result.message, "return code must be reported")
        self.assertIn("retryable error", result.raw,
                      "complete raw output must be preserved")

    def test_unknown_failure_message_names_the_tool(self):
        result = disc.classify_toc_output("total gibberish\n", 42)
        self.assertEqual(disc.ERROR, result.status)
        self.assertIn("icedax exited 42", result.message)

    def test_empty_output_with_nonzero_exit_is_error(self):
        result = disc.classify_toc_output("", 1)
        self.assertEqual(disc.ERROR, result.status)

    def test_malformed_raises_parse_error_with_raw_text(self):
        text = load("icedax_toc_malformed.txt")
        try:
            disc.parse_toc(text)
        except errors.ParseError as exc:
            self.assertIn("lead-out", str(exc))
            self.assertIn("T02", str(exc), "raw output must be reported")
        else:
            self.fail("expected ParseError")

    def test_malformed_with_zero_exit_is_error_not_no_disc(self):
        result = disc.classify_toc_output(load("icedax_toc_malformed.txt"), 0)
        self.assertEqual(disc.ERROR, result.status)

    def test_permission_failure(self):
        text = "icedax: Permission denied. Cannot open or use SCSI driver.\n"
        result = disc.classify_toc_output(text, 1)
        self.assertEqual(disc.ERROR, result.status)
        self.assertIn("permission", result.message.lower())

    def test_empty_output_is_not_silently_ok(self):
        self.assertRaises(errors.ParseError, disc.parse_toc, "")

    def test_unobserved_layouts_fail_loudly(self):
        """Only the one icedax track-line format is accepted.

        A plain '1:  23143' layout was previously tolerated on speculation;
        it must now fail rather than quietly produce a disc.
        """
        text = "1:      0\n2:  23143\nlead-out:178442\n"
        try:
            disc.parse_toc(text)
        except errors.ParseError as exc:
            self.assertIn("23143", str(exc),
                          "raw output must be preserved for debugging")
        else:
            self.fail("unobserved TOC layout must not parse")

    def test_tracks_without_leadout_rejected(self):
        self.assertRaises(errors.ParseError, disc.parse_toc,
                          "T01:       0  audio\n")

    def test_decreasing_sectors_rejected(self):
        text = ("T01:   1000  audio\n"
                "T02:    500  audio\n"
                "Leadout: 2000\n")
        self.assertRaises(errors.ParseError, disc.parse_toc, text)

    def test_leadout_before_first_track_rejected(self):
        self.assertRaises(errors.ParseError, disc.parse_toc,
                          "T01:   1000  audio\nLeadout: 10\n")


class TocRetryTest(unittest.TestCase):
    """One controlled retry, for timeouts only.

    A TOC read timed out once on the real Nexus and succeeded immediately on
    the next attempt. Everything else that can go wrong is an answer, not a
    transient, so retrying it would only delay the truth.
    """

    def setUp(self):
        self.calls = []
        self.slept = []

    def reader(self, results):
        def attempt(address, icedax=None, timeout=None):
            self.calls.append(address)
            return results.pop(0)
        return attempt

    def sleeper(self, seconds):
        self.slept.append(seconds)

    def run_read(self, results, **kwargs):
        return disc.read_toc("2,0,0", attempt=self.reader(results),
                             sleep=self.sleeper, **kwargs)

    def timeout_result(self):
        return disc.DiscResult(disc.ERROR, message="icedax TOC read timed out",
                               timed_out=True)

    def test_success_first_time_is_a_single_attempt(self):
        good = disc.classify_toc_output(load("icedax_toc_single_track.txt"))
        result = self.run_read([good])
        self.assertEqual(disc.AUDIO_DISC, result.status)
        self.assertEqual(1, len(self.calls))
        self.assertEqual([], self.slept, "must not sleep on success")

    def test_timeout_then_success(self):
        good = disc.classify_toc_output(load("icedax_toc_single_track.txt"))
        result = self.run_read([self.timeout_result(), good])
        self.assertEqual(disc.AUDIO_DISC, result.status)
        self.assertEqual(2, len(self.calls))
        self.assertEqual(1, len(self.slept), "should pause before retrying")

    def test_timeout_twice_is_error_and_stops(self):
        result = self.run_read([self.timeout_result(), self.timeout_result()])
        self.assertEqual(disc.ERROR, result.status)
        self.assertEqual(2, len(self.calls), "must not retry indefinitely")
        self.assertIn("2 attempts", result.message)

    def test_no_disc_is_not_retried(self):
        result = self.run_read(
            [disc.classify_toc_output(load("icedax_toc_no_disc.txt"), 1)])
        self.assertEqual(disc.NO_DISC, result.status)
        self.assertEqual(1, len(self.calls))

    def test_permission_error_is_not_retried(self):
        text = "icedax: Permission denied. Cannot open or use SCSI driver.\n"
        result = self.run_read([disc.classify_toc_output(text, 1)])
        self.assertEqual(disc.ERROR, result.status)
        self.assertEqual(1, len(self.calls))

    def test_malformed_output_is_not_retried(self):
        result = self.run_read(
            [disc.classify_toc_output(load("icedax_toc_malformed.txt"), 0)])
        self.assertEqual(disc.ERROR, result.status)
        self.assertEqual(1, len(self.calls),
                         "a complete but unparseable read is an answer")

    def test_retry_count_is_configurable(self):
        results = [self.timeout_result() for _ in range(4)]
        self.run_read(results, retries=3)
        self.assertEqual(4, len(self.calls))

    def test_retries_can_be_disabled(self):
        self.run_read([self.timeout_result()], retries=0)
        self.assertEqual(1, len(self.calls))

    def test_retry_delay_is_configurable(self):
        good = disc.classify_toc_output(load("icedax_toc_single_track.txt"))
        self.run_read([self.timeout_result(), good], retry_delay=2.5)
        self.assertEqual([2.5], self.slept)

    def test_only_timeouts_carry_the_flag(self):
        good = disc.classify_toc_output(load("icedax_toc_single_track.txt"))
        self.assertFalse(good.timed_out)
        self.assertFalse(
            disc.classify_toc_output(load("icedax_toc_no_disc.txt"), 1)
            .timed_out)

    def test_defaults_are_one_retry_and_a_short_delay(self):
        self.assertEqual(1, config.TOC_TIMEOUT_RETRIES)
        self.assertTrue(0.0 < config.TOC_RETRY_DELAY <= 5.0)


class CommandTest(unittest.TestCase):

    def test_toc_argv_uses_machine_oriented_output(self):
        self.assertEqual(
            ["icedax", "-D", "2,0,0", "-I", "generic_scsi",
             "-g", "-J", "-H", "-v", "toc,sectors"],
            disc.toc_argv("2,0,0", icedax="icedax"),
        )

    def test_no_block_device_anywhere(self):
        argv = " ".join(disc.toc_argv("2,0,0"))
        self.assertNotIn("/dev/sr0", argv)
        self.assertNotIn("/dev/cdrom", argv)


@unittest.skipUnless(
    os.path.exists(REAL_TOC),
    "tests/fixtures/icedax_toc_real.txt not captured yet -- "
    "run tools/capture_fixtures.sh on the Nexus")
class RealFixtureTest(unittest.TestCase):
    """Locks the parser to icedax's actual line format on this hardware."""

    def setUp(self):
        handle = open(REAL_TOC)
        try:
            self.text = handle.read()
        finally:
            handle.close()

    def test_classified_as_audio_disc(self):
        result = disc.classify_toc_output(self.text, 0)
        self.assertEqual(disc.AUDIO_DISC, result.status, result.message)

    def test_matches_measured_geometry(self):
        parsed = disc.parse_toc(self.text)
        self.assertEqual(11, parsed.track_count)
        self.assertEqual(REAL_STARTS, [t.start_sector for t in parsed.tracks])
        self.assertEqual(REAL_LEADOUT, parsed.leadout_sector)

    def test_matches_measured_durations(self):
        parsed = disc.parse_toc(self.text)
        self.assertEqual(
            REAL_DURATIONS,
            [disc.format_msf(t.duration_seconds) for t in parsed.tracks])


if __name__ == "__main__":
    unittest.main()
