"""Static checks on the QML frontend and Click packaging.

There is no Qt on the development machine, so the QML cannot be executed or
linted here -- it must be run on the device. These checks cover what is
verifiable statically and guard the rules that would otherwise be easy to
break silently:

  * the Click app requests ONLY what a frontend needs (no device or filesystem
    permissions borrowed from the Python service),
  * the QML stays inside the stock Qt 5.12 stack, with no PyQt and no WebView,
  * it talks only to the loopback service, and
  * the frozen backend contract (endpoints, direct-track payload) is honoured.

Assertions deliberately target wiring and behaviour, not labels, spacing or
pixel values, so ordinary visual iteration does not break the suite.
"""

import json
import os
import re
import sys
import unittest
from xml.etree import ElementTree

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# click/ is the package ROOT: everything that ships lives inside it, so
# `click build click` works directly. The Python service stays outside.
CLICK = os.path.join(ROOT, "click")
QML_DIR = os.path.join(CLICK, "qml")
QML = os.path.join(QML_DIR, "Main.qml")
TRANSPORT = os.path.join(QML_DIR, "TransportButton.qml")
ICON = os.path.join(CLICK, "assets", "icon.svg")
MANIFEST = os.path.join(CLICK, "manifest.json")
DESKTOP = os.path.join(CLICK, "nexus-cd-player.desktop")
APPARMOR = os.path.join(CLICK, "apparmor.json")

PACKAGED_FILES = (MANIFEST, APPARMOR, DESKTOP, QML, TRANSPORT, ICON)

# The frozen service API. The frontend must not invent routes.
SERVICE_ENDPOINTS = ("/state", "/scan", "/play", "/pause", "/resume", "/stop",
                     "/next", "/previous")


def read(path):
    handle = open(path)
    try:
        return handle.read()
    finally:
        handle.close()


def load_json(path):
    return json.loads(read(path))


def strip_qml_comments(text):
    """Remove // and /* */ comments.

    Prose explaining what is banned ("no PyQt, no WebView") must not itself
    trip the ban.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    # The lookbehind protects "http://..." from being eaten as a comment.
    text = re.sub(r"(?<!:)//[^\n]*", " ", text)
    return text


def declaration_of(code, name, span=220):
    """The text of one property binding.

    Bounded on purpose: a DOTALL search for a term "somewhere after the
    property name" matches the whole rest of the file and proves nothing.
    """
    marker = name + ":"
    index = code.find(marker)
    if index < 0:
        return ""
    return code[index:index + span]


def string_literals(code):
    """Double-quoted literals: the user-visible text and identifiers.

    Feature checks run against these rather than the whole file, so ordinary
    QML property names (Timer's `repeat: true`, `Repeater`) are not mistaken
    for playback features.
    """
    return re.findall(r'"([^"\n]*)"', code)


class QmlAssertions(object):
    """Short, readable failures instead of dumping the whole file."""

    def assertHas(self, needle, why):
        if needle not in self.code:
            self.fail("{0}: {1!r} not found in {2}".format(
                why, needle, os.path.basename(self.path)))

    def assertLacks(self, needle, why):
        if needle in self.code:
            self.fail("{0}: {1!r} present in {2}".format(
                why, needle, os.path.basename(self.path)))


class PackageLayoutTest(unittest.TestCase):
    """click/ must be self-contained so `click build click` works directly."""

    def test_every_packaged_file_is_present(self):
        for path in PACKAGED_FILES:
            self.assertTrue(os.path.exists(path),
                            "missing from the package: " + path)

    def test_no_stale_top_level_qml_copy(self):
        stale = os.path.join(ROOT, "qml", "Main.qml")
        self.assertFalse(os.path.exists(stale),
                         "old top-level qml/Main.qml still exists; there must "
                         "not be two independent copies")

    def test_only_one_main_qml_in_the_tree(self):
        found = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__", "logs")]
            if "Main.qml" in filenames:
                found.append(os.path.join(dirpath, "Main.qml"))
        self.assertEqual([QML], found)

    def test_no_python_is_packaged(self):
        """The Python service runs OUTSIDE Click confinement."""
        packaged = []
        for dirpath, dirnames, filenames in os.walk(CLICK):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith((".py", ".pyc")):
                    packaged.append(os.path.join(dirpath, name))
        self.assertEqual([], packaged,
                         "backend/service code must not ship in the Click app")

    def test_no_backend_directories_copied_in(self):
        for name in ("backend", "ui_service", "tools", "tests"):
            self.assertFalse(os.path.exists(os.path.join(CLICK, name)),
                             name + "/ must not be inside the Click package")

    def test_package_files_never_escape_the_package_root(self):
        offenders = []
        for path in PACKAGED_FILES:
            for number, line in enumerate(read(path).splitlines(), 1):
                if "../" in line:
                    offenders.append("{0}:{1}".format(
                        os.path.relpath(path, ROOT), number))
        self.assertEqual([], offenders, "package references a parent path")

    def test_qml_components_sit_beside_main(self):
        """Sibling .qml files are implicitly importable by qmlscene."""
        self.assertEqual(os.path.dirname(QML), os.path.dirname(TRANSPORT))


class ManifestTest(unittest.TestCase):

    def setUp(self):
        self.manifest = load_json(MANIFEST)

    def test_required_keys(self):
        for key in ("name", "title", "version", "framework", "hooks",
                    "maintainer", "architecture"):
            self.assertIn(key, self.manifest)

    def test_targets_the_generic_xenial_framework(self):
        """Generic, not a point release.

        `click framework list` on the device offers ubuntu-sdk-16.04 through
        16.04.8; UBports documents the generic name, and pinning a point
        release would needlessly narrow which images accept the package.
        """
        self.assertEqual("ubuntu-sdk-16.04", self.manifest["framework"])

    def test_hook_names_match_the_shipped_files(self):
        hooks = self.manifest["hooks"]
        self.assertEqual(1, len(hooks))
        hook = list(hooks.values())[0]
        for key in ("apparmor", "desktop"):
            self.assertIn(key, hook)
            self.assertTrue(
                os.path.exists(os.path.join(CLICK, hook[key])),
                "manifest references a missing file: " + hook[key])

    def test_architecture_is_all_for_a_qml_only_app(self):
        self.assertEqual("all", self.manifest["architecture"])


class IconTest(unittest.TestCase):

    def test_icon_is_well_formed_svg(self):
        tree = ElementTree.parse(ICON)
        self.assertTrue(tree.getroot().tag.endswith("svg"))

    def test_icon_is_self_contained(self):
        text = read(ICON)
        self.assertNotIn("xlink:href", text)
        for raster in (".png", ".jpg", ".jpeg", ".gif"):
            self.assertNotIn(raster, text,
                             "icon must not depend on a raster asset")

    def test_icon_has_no_external_resources(self):
        remote = re.findall(r"https?://(?!www\.w3\.org)", read(ICON))
        self.assertEqual([], remote)

    def test_icon_declares_a_viewbox(self):
        self.assertIsNotNone(ElementTree.parse(ICON).getroot().get("viewBox"))


class AppArmorTest(unittest.TestCase):

    def setUp(self):
        self.policy = load_json(APPARMOR)

    def test_policy_version_matches_the_device(self):
        self.assertEqual(16.04, self.policy["policy_version"])

    def test_requests_networking_for_loopback_http(self):
        self.assertIn("networking", self.policy["policy_groups"])

    def test_requests_nothing_beyond_networking(self):
        """The frontend must not inherit the service's device needs."""
        self.assertEqual(["networking"], self.policy["policy_groups"])

    def test_no_device_or_filesystem_groups(self):
        forbidden = ("audio", "video", "content_exchange", "camera",
                     "location", "usermetrics", "history", "sensors")
        for group in forbidden:
            self.assertNotIn(group, self.policy["policy_groups"])

    def test_no_extra_template_or_read_paths(self):
        for key in ("template", "read_path", "write_path", "abstractions"):
            self.assertNotIn(key, self.policy,
                             "frontend must not widen its profile via " + key)


class DesktopFileTest(unittest.TestCase):

    def setUp(self):
        self.text = read(DESKTOP)
        self.entries = {}
        for line in self.text.splitlines():
            if "=" in line and not line.startswith("["):
                key, value = line.split("=", 1)
                self.entries[key] = value

    def test_is_a_desktop_entry(self):
        self.assertIn("[Desktop Entry]", self.text)
        self.assertEqual("Application", self.entries["Type"])

    def test_exec_is_exactly_qmlscene_on_the_packaged_qml(self):
        self.assertEqual("qmlscene qml/Main.qml", self.entries["Exec"])

    def test_icon_points_at_the_packaged_svg(self):
        self.assertEqual("assets/icon.svg", self.entries["Icon"])

    def test_referenced_paths_resolve_inside_the_package(self):
        qml_path = self.entries["Exec"].split()[-1]
        self.assertTrue(os.path.exists(os.path.join(CLICK, qml_path)),
                        "Exec references a missing file: " + qml_path)
        self.assertTrue(
            os.path.exists(os.path.join(CLICK, self.entries["Icon"])),
            "Icon references a missing file: " + self.entries["Icon"])

    def test_terminal_is_false(self):
        self.assertEqual("false", self.entries["Terminal"])

    def test_requests_landscape(self):
        self.assertIn("X-Ubuntu-Supported-Orientations=landscape", self.text)

    def test_marked_as_touch(self):
        self.assertIn("X-Ubuntu-Touch=true", self.text)

    def test_does_not_launch_python(self):
        exec_line = self.entries["Exec"]
        self.assertNotIn("python", exec_line.lower())
        self.assertNotIn("ui_service", exec_line)


class QmlEnvironmentTest(unittest.TestCase, QmlAssertions):
    """Stays inside the stack that actually exists on the device."""

    def setUp(self):
        self.path = QML
        self.text = read(QML)
        self.code = strip_qml_comments(self.text)

    def test_uses_the_stock_qt_512_stack(self):
        for line in ("import QtQuick 2.12",
                     "import QtQuick.Controls 2.5",
                     "import QtQuick.Layouts 1.12"):
            self.assertHas(line, "required import missing")

    def test_imports_nothing_unsupported(self):
        # QtQuick.Window is deliberately NOT allowed: it existed only for
        # Screen.orientation, which was physically disproven on this device.
        # Keeping it out stops the dead path being reintroduced alongside
        # the sensor.
        allowed = ("QtQuick", "QtQuick.Controls", "QtQuick.Layouts",
                   "QtSensors")
        for match in re.finditer(r"^\s*import\s+([\w.]+)", self.code,
                                 re.MULTILINE):
            module = match.group(1)
            self.assertIn(module, allowed,
                          "unsupported QML module: " + module)

    def test_does_not_use_ubuntu_ui_toolkit(self):
        self.assertLacks("Ubuntu.Components", "frontend must stay generic Qt")

    def test_does_not_reference_pyqt(self):
        lowered = self.code.lower()
        found = [b for b in ("pyqt", "pyotherside", "import python")
                 if b in lowered]
        self.assertEqual([], found, "QML reaches outside the stock Qt stack")

    def test_no_webview_or_browser(self):
        lowered = self.code.lower()
        found = [b for b in ("webview", "webengine", "webkit", "browser")
                 if b in lowered]
        self.assertEqual([], found, "this UI must not embed a browser")

    def test_no_playback_logic_leaked_into_the_frontend(self):
        """Timing and track logic belong to the controller, not the UI."""
        lowered = self.code.lower()
        found = [b for b in ("icedax", "aplay", "/dev/sg", "sector", "75.0")
                 if b in lowered]
        self.assertEqual([], found, "playback logic leaked into the frontend")

    def test_brackets_are_balanced(self):
        for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
            self.assertEqual(
                self.text.count(opener), self.text.count(closer),
                "unbalanced {0}{1} in Main.qml".format(opener, closer))


class QmlNetworkingTest(unittest.TestCase, QmlAssertions):
    """Loopback only. No metadata services, no album art lookups."""

    def setUp(self):
        self.path = QML
        self.code = strip_qml_comments(read(QML))

    def test_talks_to_the_loopback_service(self):
        self.assertHas("http://127.0.0.1:8765", "service URL")
        self.assertLacks("0.0.0.0", "must not bind or call a public address")

    def test_no_external_urls(self):
        urls = re.findall(r"https?://[^\s\"')]+", self.code)
        external = [u for u in urls if not u.startswith("http://127.0.0.1")]
        self.assertEqual([], external, "frontend must not call the internet")

    def test_no_metadata_services(self):
        lowered = self.code.lower()
        for service in ("musicbrainz", "discogs", "cddb", "gracenote",
                        "coverart", "last.fm", "spotify"):
            self.assertNotIn(service, lowered,
                             "metadata lookup is out of scope")

    def test_uses_xmlhttprequest(self):
        self.assertHas("XMLHttpRequest", "localhost calls use XMLHttpRequest")

    def test_polls_state_on_a_timer(self):
        self.assertHas("Timer", "state polling timer")
        self.assertHas('"GET", serviceUrl + "/state"', "state polling request")

    def test_poll_interval_is_reasonable(self):
        match = re.search(r"pollIntervalMs\s*:\s*(\d+)", self.code)
        self.assertIsNotNone(match, "poll interval not declared")
        interval = int(match.group(1))
        self.assertGreaterEqual(interval, 200, "polling faster than needed")
        self.assertLessEqual(interval, 500, "polling too slow to feel live")

    def test_only_known_endpoints_are_used(self):
        used = set(re.findall(r'"(/[a-z]+)"', self.code))
        unknown = used - set(SERVICE_ENDPOINTS)
        self.assertEqual(set(), unknown,
                         "frontend invented endpoints: " + str(unknown))

    def test_every_transport_endpoint_is_wired(self):
        for endpoint in SERVICE_ENDPOINTS:
            self.assertHas('"' + endpoint + '"', "endpoint not wired")


class DirectTrackSelectionTest(unittest.TestCase, QmlAssertions):
    """The headline feature: jump straight to any track."""

    def setUp(self):
        self.path = QML
        self.code = strip_qml_comments(read(QML))

    def test_sends_a_track_number_in_the_play_body(self):
        """POST /play with {"track": N} -- the existing service contract."""
        self.assertHas("JSON.stringify", "play body must be JSON")
        self.assertTrue(
            re.search(r'JSON\.stringify\(\s*\{\s*"track"\s*:', self.code),
            'direct play must post {"track": N}')

    def test_has_a_play_track_function(self):
        self.assertHas("function playTrack(", "direct-jump helper")

    def test_selector_popup_exists(self):
        self.assertHas("Popup", "track selector popup")
        self.assertHas("ListView", "track list")

    def test_list_is_driven_by_the_backend_track_count(self):
        self.assertTrue(re.search(r"model:\s*root\.totalTracks", self.code),
                        "track list must come from total_tracks")

    def test_does_not_use_a_default_combobox(self):
        self.assertLacks("ComboBox", "use the custom popup, not a ComboBox")

    def test_selecting_the_current_track_does_not_restart_it(self):
        self.assertHas("if (!isCurrent)",
                       "re-selecting the current track should be a no-op")

    def test_popup_closes_on_selection(self):
        self.assertHas("trackPopup.close()", "selection must close the popup")

    def test_current_track_is_indicated(self):
        self.assertHas("isCurrent", "current track must be marked in the list")

    def test_rows_are_finger_sized(self):
        match = re.search(r"rowHeight\s*:\s*(\d+)", self.code)
        self.assertIsNotNone(match, "row height not declared")
        self.assertGreaterEqual(int(match.group(1)), 48,
                                "track rows must be finger-friendly")

    def test_list_scrolls(self):
        self.assertHas("clip: true", "long track lists must scroll")


class LoadingPresentationTest(unittest.TestCase, QmlAssertions):
    """requestedTrack is presentation metadata, never control flow."""

    def setUp(self):
        self.path = QML
        self.code = strip_qml_comments(read(QML))

    def test_requested_track_exists(self):
        self.assertHas("property int requestedTrack",
                       "frontend-only requested-track hint")

    def test_requested_track_is_cleared_once_playing(self):
        self.assertTrue(
            re.search(r"requestedTrack\s*=\s*0", self.code),
            "requestedTrack must be cleared when the backend takes over")

    def test_loading_names_the_requested_track(self):
        self.assertHas("Loading Track ", "loading line should name the track")

    def test_loading_state_is_derived_from_backend_busy(self):
        self.assertTrue(
            re.search(r"property bool loading:\s*busy", self.code),
            "loading must follow the service's busy flag")

    def test_busy_disables_transport(self):
        declaration = declaration_of(self.code, "transportEnabled")
        self.assertIn("!loading", declaration,
                      "transport controls must be disabled while loading")
        self.assertIn("hasDisc", declaration,
                      "transport needs a disc")

    def test_busy_disables_the_track_selector(self):
        declaration = declaration_of(self.code, "selectorEnabled")
        self.assertIn("transportEnabled", declaration,
                      "the selector must inherit the transport enable rule, "
                      "so loading disables it too")

    def test_popup_closes_when_loading_starts(self):
        """No stale tap queue behind a spin-up."""
        self.assertHas("onLoadingChanged", "popup must close when busy")

    def test_no_invented_progress_during_loading(self):
        self.assertTrue(
            re.search(r"if \(root\.loading \|\| root\.trackDuration <= 0\)",
                      self.code),
            "progress must not be fabricated while loading")

    def test_shows_a_busy_indicator(self):
        self.assertHas("BusyIndicator", "loading needs a visible indicator")


class StatePresentationTest(unittest.TestCase, QmlAssertions):
    """State is expressed in human language, not raw enum labels."""

    def setUp(self):
        self.path = QML
        self.code = strip_qml_comments(read(QML))

    def test_no_raw_state_debug_label(self):
        self.assertLacks('"State: "', "raw state labels are developer UI")
        self.assertLacks('text: "backend: "', "debug readout removed")

    def test_human_readable_state_messages(self):
        for message in ("CD drive not connected", "Insert a CD", "Disc ready",
                        "Player service unavailable"):
            self.assertHas(message, "missing human-readable state message")

    def test_handles_a_disconnected_backend(self):
        self.assertHas("backendConnected", "connection tracking")
        self.assertHas("Player service unavailable", "disconnected message")
        self.assertIn("backendConnected",
                      declaration_of(self.code, "transportEnabled"),
                      "controls must be disabled when the service is "
                      "unreachable")

    def test_polling_continues_while_disconnected(self):
        """The Timer is unconditional, so it recovers on its own."""
        self.assertTrue(
            re.search(r"Timer\s*\{[^}]*running:\s*true", self.code,
                      re.DOTALL),
            "polling must keep running so the UI reconnects by itself")

    def test_errors_are_shown_inline_not_in_dialogs(self):
        self.assertHas("errorText", "error surface")
        self.assertLacks("MessageDialog", "no modal dialog spam")
        self.assertLacks("Dialog {", "no modal dialog spam")

    def test_scan_is_prominent_only_when_there_is_nothing_to_play(self):
        self.assertHas("scanIsPrimary", "scan prominence rule")
        declaration = declaration_of(self.code, "scanIsPrimary", 320)
        self.assertIn("NO_DISC", declaration,
                      "scan should be primary when no disc is present")
        self.assertIn("NO_DRIVE", declaration)

    def test_track_numbers_are_zero_padded_for_display(self):
        self.assertHas("function pad2(", "zero-padded track display")

    def test_time_is_formatted_not_raw_seconds(self):
        self.assertHas("function formatTime(", "time formatting helper")


class TransportControlsTest(unittest.TestCase, QmlAssertions):

    def setUp(self):
        self.path = QML
        self.code = strip_qml_comments(read(QML))

    def test_all_transport_controls_exist(self):
        for icon in ("prev", "next", "stop", "play", "pause"):
            self.assertHas('"' + icon + '"', "missing transport icon: " + icon)

    def test_play_pause_swaps_by_state(self):
        self.assertTrue(
            re.search(r'icon:\s*root\.isPlaying\s*\?\s*"pause"\s*:\s*"play"',
                      self.code),
            "the centre control must show pause while playing")

    def test_stop_only_enabled_during_playback(self):
        declaration = declaration_of(self.code, "stopEnabled")
        self.assertIn("isPlaying", declaration)
        self.assertIn("isPaused", declaration)
        self.assertIn("transportEnabled", declaration,
                      "stop must also respect the loading/disc rules")

    def test_touch_targets_are_large(self):
        sizes = [int(n) for n in re.findall(r"diameter:\s*(\d+)", self.code)]
        self.assertTrue(sizes, "no transport buttons found")
        self.assertGreaterEqual(min(sizes), 48,
                                "touch targets must be finger-sized")

    def test_no_out_of_scope_controls(self):
        """Checked against visible text, not raw source.

        `repeat: true` on the poll Timer is a Qt property, not repeat
        playback -- scanning the whole file would flag it.
        """
        visible = " ".join(string_literals(self.code)).lower()
        found = [f for f in ("shuffle", "repeat", "playlist", "volume",
                             "eject")
                 if f in visible]
        self.assertEqual([], found, "out-of-scope controls in the UI")

    def test_progress_bar_is_display_only(self):
        """No scrubbing in this phase."""
        self.assertHas("progressTrack", "progress bar")
        progress = self.code[self.code.find("Item {", self.code.find(
            "progressTrack")) - 400:]
        self.assertNotIn("onPositionChanged", progress[:1500],
                         "progress bar must not be draggable")
        self.assertLacks("Slider", "a Slider would imply seeking")


class OrientationTest(unittest.TestCase, QmlAssertions):
    """Both landscape orientations, one layout, no portrait.

    Screen.orientation was tried first and physically does not update on this
    Nexus -- Ubuntu Touch / Mir never delivers the change. QtSensors is the
    confirmed-present replacement. These checks are static; whether the sensor
    actually reports LeftUp/RightUp can only be proven on the device.
    """

    def setUp(self):
        self.path = QML
        self.text = read(QML)
        self.code = strip_qml_comments(self.text)

    def function_body(self, name):
        """Source of one QML function, brace-matched."""
        start = self.code.find("function " + name)
        self.assertNotEqual(-1, start, "function " + name + " not found")
        depth = 0
        for index in range(self.code.find("{", start), len(self.code)):
            if self.code[index] == "{":
                depth += 1
            elif self.code[index] == "}":
                depth -= 1
                if depth == 0:
                    return self.code[start:index + 1]
        self.fail("unbalanced braces in " + name)

    # -- the sensor mechanism -------------------------------------------

    def test_imports_qtsensors(self):
        self.assertHas("import QtSensors 5.12", "QtSensors import")

    def test_declares_an_orientation_sensor(self):
        self.assertHas("OrientationSensor", "orientation sensor")

    def test_sensor_is_active(self):
        sensor = self.code[self.code.find("OrientationSensor"):]
        self.assertIn("active: true", sensor[:300],
                      "the sensor must be started")

    def test_reacts_to_reading_changes(self):
        self.assertHas("onReadingChanged", "sensor change notification")

    def test_handles_left_up(self):
        self.assertHas("OrientationReading.LeftUp", "LeftUp handling")

    def test_handles_right_up(self):
        self.assertHas("OrientationReading.RightUp", "RightUp handling")

    def test_startup_guards_against_a_null_reading(self):
        """There may be no reading at all when the app launches."""
        body = self.function_body("applyOrientation")
        self.assertIn("if (!reading)", body,
                      "a missing initial reading must not throw")

    # -- the dead Screen path is gone ------------------------------------

    def test_screen_orientation_logic_is_removed(self):
        self.assertLacks("Screen.orientation",
                         "the disproven Screen path must be gone")

    def test_orientation_update_mask_is_removed(self):
        self.assertLacks("orientationUpdateMask",
                         "the disproven Screen path must be gone")

    def test_qtquick_window_import_is_removed(self):
        self.assertLacks("import QtQuick.Window",
                         "QtQuick.Window existed only for Screen.orientation")

    def test_only_one_orientation_mechanism(self):
        """Exactly one source of physical orientation, not two competing."""
        mechanisms = []
        if "OrientationSensor" in self.code:
            mechanisms.append("QtSensors")
        if "Screen.orientation" in self.code:
            mechanisms.append("Screen")
        self.assertEqual(["QtSensors"], mechanisms)

    # -- mapping rules ---------------------------------------------------

    def test_mapping_is_centralised_for_a_one_line_swap(self):
        """If the device flips the wrong way, only these two change."""
        self.assertHas("rotationWhenLeftUp", "centralised mapping")
        self.assertHas("rotationWhenRightUp", "centralised mapping")
        self.assertTrue(
            re.search(r"rotationWhenLeftUp:\s*\d+", self.code)
            and re.search(r"rotationWhenRightUp:\s*\d+", self.code),
            "the mapping must be plain numbers, trivially swappable")

    def test_the_two_landscape_rotations_are_0_and_180(self):
        values = sorted(int(v) for v in re.findall(
            r"rotationWhen(?:Left|Right)Up:\s*(\d+)", self.code))
        self.assertEqual([0, 180], values)

    def test_only_landscape_readings_change_rotation(self):
        """TopUp / TopDown / FaceUp / FaceDown / Undefined must not rotate."""
        body = self.function_body("applyOrientation")
        assignments = re.findall(r"contentRotation\s*=", body)
        self.assertEqual(2, len(assignments),
                         "exactly two readings may set rotation")
        for reading in ("TopUp", "TopDown", "FaceUp", "FaceDown"):
            self.assertNotIn(
                "OrientationReading." + reading, body,
                reading + " must not influence rotation")

    def test_non_landscape_readings_are_still_described(self):
        """They are reported for debugging, just not acted on."""
        body = self.function_body("describeOrientation")
        for reading in ("TopUp", "TopDown", "FaceUp", "FaceDown"):
            self.assertIn("OrientationReading." + reading, body)

    def test_no_portrait_layout(self):
        for banned in ("Qt.PortraitOrientation",
                       "Qt.InvertedPortraitOrientation"):
            self.assertLacks(banned, "portrait is explicitly out of scope")

    def test_no_width_height_swap(self):
        """A 180 rotation covers the same area; swapping would be a bug."""
        self.assertNotIn("width = root.height", self.code)
        self.assertNotIn("height = root.width", self.code)

    def test_orientation_is_not_polled(self):
        """Exactly one Timer, and it is the state poller."""
        timers = re.findall(r"\bTimer\s*\{", self.code)
        self.assertEqual(1, len(timers),
                         "orientation must use sensor notifications")
        timer_block = self.code[self.code.find("Timer {"):]
        self.assertIn("root.poll()", timer_block[:400],
                      "the only Timer must be the state poller")

    # -- rendering -------------------------------------------------------

    def test_rotation_is_applied_to_a_single_content_root(self):
        self.assertEqual(1, self.code.count("id: contentRoot"),
                         "there must be exactly one content root")
        self.assertTrue(
            re.search(r"rotation:\s*root\.contentRotation", self.code),
            "the content root must take its rotation from contentRotation")

    def test_rotation_is_about_the_centre(self):
        self.assertHas("transformOrigin: Item.Center",
                       "a 180 flip must pivot on the centre")

    def test_overlay_is_rotated_too(self):
        """Popups live in the window overlay, outside contentRoot."""
        self.assertHas("Overlay.overlay", "overlay rotation target")
        self.assertTrue(
            re.search(r"target:\s*Overlay\.overlay\s*\n\s*property:\s*"
                      r'"rotation"', self.code),
            "the overlay must be rotated alongside the content root")
        self.assertTrue(
            re.search(r'property:\s*"rotation"\s*\n\s*value:\s*'
                      r"root\.contentRotation", self.code),
            "the overlay must follow the same contentRotation value")

    def test_transition_does_not_sweep_through_portrait(self):
        behaviour = self.code[self.code.find("Behavior on rotation"):]
        head = behaviour[:600]
        self.assertIn("opacity", head, "the flip should crossfade, not spin")
        self.assertNotIn('property: "rotation"', head,
                         "rotation itself must not be animated")

    def test_desktop_launcher_stays_landscape_only(self):
        desktop = read(DESKTOP)
        self.assertIn("X-Ubuntu-Supported-Orientations=landscape", desktop)
        self.assertNotIn("portrait", desktop.lower())


class TemporaryDebugTest(unittest.TestCase, QmlAssertions):
    """The sensor readout is a development aid and must stay easy to remove."""

    def setUp(self):
        self.path = QML
        self.text = read(QML)
        self.code = strip_qml_comments(self.text)

    def test_debug_label_exists_for_physical_validation(self):
        self.assertHas('"Sensor: "', "temporary sensor readout")

    def test_debug_label_is_clearly_marked_temporary(self):
        self.assertIn("TEMPORARY", self.text,
                      "the debug readout must be marked for removal")

    def test_debug_label_is_unobtrusive(self):
        block = self.code[self.code.find('"Sensor: "'):]
        size = re.search(r"font\.pixelSize:\s*(\d+)", block[:400])
        self.assertIsNotNone(size)
        self.assertLessEqual(int(size.group(1)), 14,
                             "the debug readout must stay small")

    def test_no_other_debug_output(self):
        self.assertLacks("console.log", "no stray debug logging")


class TransportButtonComponentTest(unittest.TestCase, QmlAssertions):

    def setUp(self):
        self.path = TRANSPORT
        self.code = strip_qml_comments(read(TRANSPORT))

    def test_is_plain_qtquick(self):
        self.assertHas("import QtQuick 2.12", "component import")

    def test_draws_icons_without_external_assets(self):
        self.assertHas("Canvas", "icons are drawn, not loaded")
        for raster in (".png", ".jpg", ".svg"):
            self.assertLacks(raster, "no image assets in the component")

    def test_gives_pressed_feedback(self):
        self.assertHas("mouseArea.pressed", "pressed visual feedback")

    def test_does_not_shadow_the_builtin_enabled_property(self):
        """Item.enabled already gates input for an item and its children."""
        self.assertLacks("property bool enabled",
                         "redeclaring enabled would shadow Item.enabled")

    def test_supports_every_icon_the_ui_asks_for(self):
        for icon in ("play", "pause", "stop", "prev", "next"):
            self.assertHas('"' + icon + '"', "icon not drawn: " + icon)

    def test_brackets_are_balanced(self):
        text = read(TRANSPORT)
        for opener, closer in (("{", "}"), ("(", ")")):
            self.assertEqual(text.count(opener), text.count(closer),
                             "unbalanced brackets in TransportButton.qml")


if __name__ == "__main__":
    unittest.main()
