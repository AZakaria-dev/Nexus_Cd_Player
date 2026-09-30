/*
 * Nexus CD Player -- touchscreen frontend.
 *
 * Frontend only. It holds no playback logic: every value shown comes from the
 * Python UI service over loopback HTTP, and every action is one of that
 * service's existing endpoints. The backend is frozen.
 *
 *     touch -> HTTP -> ui_service -> CDController -> icedax -> aplay
 *
 * Targets the stock Qt 5.12.9 stack on UBports. No PyQt, no Ubuntu UI Toolkit,
 * no WebView, no network beyond 127.0.0.1.
 */

import QtQuick 2.12
import QtQuick.Controls 2.5
import QtQuick.Layouts 1.12
import QtSensors 5.12

ApplicationWindow {
    id: root

    visible: true
    width: 1280
    height: 800
    title: "Nexus CD Player"
    color: colorBackground

    // ---- palette -------------------------------------------------------
    // Dark hi-fi appliance: near-black chassis, warm off-white type, a single
    // restrained amber accent used only for the active element.

    readonly property color colorBackground:   "#0E1013"
    readonly property color colorSurface:      "#181B21"
    readonly property color colorSurfaceHigh:  "#1F242B"
    readonly property color colorBorder:       "#2C323A"
    readonly property color colorPrimaryText:  "#F3EEE4"
    readonly property color colorSecondaryText:"#868D97"
    readonly property color colorAccent:       "#D9A441"
    readonly property color colorDanger:       "#C9614A"
    readonly property color colorOk:           "#6E9E6B"

    // ---- orientation ---------------------------------------------------
    //
    // The tablet is mounted in a 3D-printed enclosure that may face either
    // way, so the UI must read upright with EITHER long edge on top. That is
    // one layout rotated 180 degrees -- never a second, portrait design.
    //
    // Screen.orientation was tried first and physically DOES NOT update on
    // this device: Ubuntu Touch / Mir never delivers the change. QtSensors is
    // the confirmed-present replacement (qml-module-qtsensors 5.12.9), and it
    // is now the ONLY orientation mechanism here.

    // The whole physical-to-visual mapping, in one place. If the device turns
    // out to flip the wrong way, swap these two numbers and nothing else
    // changes.
    readonly property int rotationWhenLeftUp: 0
    readonly property int rotationWhenRightUp: 180

    property int contentRotation: 0

    // TEMPORARY development aid, surfaced in the title bar; see the debug
    // label below. Remove both once orientation is validated on hardware.
    property string sensorOrientation: "Undefined"

    OrientationSensor {
        id: orientationSensor
        active: true
        // Event-driven: the sensor notifies us, nothing polls it.
        onReadingChanged: root.applyOrientation()
    }

    function describeOrientation(value) {
        switch (value) {
        case OrientationReading.TopUp:    return "TopUp";
        case OrientationReading.TopDown:  return "TopDown";
        case OrientationReading.LeftUp:   return "LeftUp";
        case OrientationReading.RightUp:  return "RightUp";
        case OrientationReading.FaceUp:   return "FaceUp";
        case OrientationReading.FaceDown: return "FaceDown";
        }
        return "Undefined";
    }

    function applyOrientation() {
        // At startup there may be no reading at all; that is not an error,
        // it just means we keep the default rotation until one arrives.
        var reading = orientationSensor.reading;
        if (!reading)
            return;

        var value = reading.orientation;
        root.sensorOrientation = describeOrientation(value);

        if (value === OrientationReading.LeftUp)
            root.contentRotation = root.rotationWhenLeftUp;
        else if (value === OrientationReading.RightUp)
            root.contentRotation = root.rotationWhenRightUp;
        // Every other value -- TopUp, TopDown, FaceUp, FaceDown, Undefined --
        // deliberately leaves contentRotation untouched, so the last valid
        // landscape rotation persists while the device is being turned
        // through portrait or is lying flat on a bench.
    }

    // Popups are rendered in the window overlay, which is NOT a child of
    // contentRoot, so it needs the same rotation applied separately -- without
    // this the track selector would appear upside down after a flip.
    Binding {
        target: Overlay.overlay
        property: "rotation"
        value: root.contentRotation
    }

    // ---- service -------------------------------------------------------

    readonly property string serviceUrl: "http://127.0.0.1:8765"
    readonly property int pollIntervalMs: 250

    property bool backendConnected: false
    property var uiState: ({})

    // Presentation-only: which track the user just asked for. The service
    // legitimately keeps reporting the previous track while a direct jump
    // blocks on drive spin-up, so this is what lets the UI say
    // "Loading Track 04..." instead of showing a stale number. Cleared as soon
    // as the backend reports real playback. It never drives an API call.
    property int requestedTrack: 0

    readonly property string playerState: uiState.state !== undefined
                                          ? uiState.state : "UNKNOWN"
    readonly property bool busy: uiState.busy === true
    readonly property int totalTracks: uiState.total_tracks !== undefined
                                       ? uiState.total_tracks : 0
    readonly property int currentTrack: (uiState.current_track !== undefined
                                         && uiState.current_track !== null)
                                        ? uiState.current_track : 0
    readonly property real trackElapsed: uiState.track_elapsed !== undefined
                                         ? uiState.track_elapsed : 0
    readonly property real trackDuration: uiState.track_duration !== undefined
                                          ? uiState.track_duration : 0
    readonly property string errorText: (uiState.error !== undefined
                                         && uiState.error !== null)
                                        ? uiState.error : ""
    readonly property bool hasDisc: uiState.has_disc === true

    readonly property bool loading: busy || playerState === "LOADING"
    readonly property bool isPlaying: playerState === "PLAYING"
    readonly property bool isPaused: playerState === "PAUSED"
    readonly property bool isStopped: playerState === "STOPPED"
    readonly property bool isReady: playerState === "READY"

    // ---- enable rules --------------------------------------------------
    // One rule each, so every control's state is obvious and consistent.

    readonly property bool transportEnabled: backendConnected && !loading
                                             && hasDisc
    readonly property bool stopEnabled: transportEnabled
                                        && (isPlaying || isPaused)
    readonly property bool selectorEnabled: transportEnabled && totalTracks > 0
    readonly property bool scanEnabled: backendConnected && !loading

    // Scan is a recovery action, not part of normal playback, so it only
    // takes visual weight when there is nothing to play.
    readonly property bool scanIsPrimary: backendConnected
                                          && (playerState === "NO_DISC"
                                              || playerState === "NO_DRIVE"
                                              || playerState === "ERROR")
    readonly property bool showsPlayer: hasDisc && totalTracks > 0

    // ---- helpers -------------------------------------------------------

    function formatTime(seconds) {
        if (seconds === undefined || seconds === null || isNaN(seconds)
                || seconds < 0)
            return "0:00";
        var total = Math.floor(seconds);
        var minutes = Math.floor(total / 60);
        var rest = total % 60;
        return minutes + ":" + (rest < 10 ? "0" : "") + rest;
    }

    function pad2(number) {
        return (number < 10 ? "0" : "") + number;
    }

    // The track the display should talk about: the requested one while a jump
    // is in flight, otherwise whatever the backend reports.
    function displayTrack() {
        if (loading && requestedTrack > 0)
            return requestedTrack;
        return currentTrack;
    }

    function headlineText() {
        if (!backendConnected)
            return "Player service unavailable";
        switch (playerState) {
        case "NO_DRIVE": return "CD drive not connected";
        case "NO_DISC":  return "Insert a CD";
        case "ERROR":    return "Playback problem";
        }
        if (loading) {
            return requestedTrack > 0
                    ? "Loading Track " + pad2(requestedTrack) + "…"
                    : "Loading…";
        }
        if (showsPlayer)
            return "TRACK " + pad2(displayTrack());
        if (isReady)
            return "Disc ready";
        return "";
    }

    function subText() {
        if (!backendConnected)
            return "Waiting for the player service";
        if (loading)
            return "The drive is spinning up";
        if (isPaused)
            return "Paused";
        if (isStopped)
            return "Stopped";
        if (isReady && showsPlayer)
            return "Ready — " + totalTracks + " tracks";
        if (playerState === "NO_DISC")
            return "Insert a disc, then scan";
        if (playerState === "NO_DRIVE")
            return "Check the drive connection, then scan";
        return "";
    }

    // ---- HTTP ----------------------------------------------------------
    // Every failure mode -- service down, refused, timeout, malformed body --
    // must leave the UI polling rather than dead or spamming dialogs.

    function poll() {
        var request = new XMLHttpRequest();
        request.onreadystatechange = function () {
            if (request.readyState !== XMLHttpRequest.DONE)
                return;
            if (request.status === 200) {
                try {
                    var payload = JSON.parse(request.responseText);
                    if (payload && payload.ok && payload.state) {
                        root.uiState = payload.state;
                        root.backendConnected = true;
                        // Once real playback is reported the backend is
                        // authoritative again.
                        if (!root.loading && root.requestedTrack > 0)
                            root.requestedTrack = 0;
                        return;
                    }
                } catch (e) {
                    // Malformed body: treat exactly like a dead service.
                }
            }
            root.backendConnected = false;
        };
        try {
            request.open("GET", serviceUrl + "/state");
            request.send();
        } catch (e) {
            root.backendConnected = false;
        }
    }

    // 409 (busy) is expected and ignored -- the next poll shows the truth.
    function sendCommand(path, body) {
        if (!backendConnected)
            return;
        var request = new XMLHttpRequest();
        request.onreadystatechange = function () {
            if (request.readyState !== XMLHttpRequest.DONE)
                return;
            if (request.status === 0)
                root.backendConnected = false;
            root.poll();
        };
        try {
            request.open("POST", serviceUrl + path);
            request.setRequestHeader("Content-Type", "application/json");
            request.send(body !== undefined ? body : "");
        } catch (e) {
            root.backendConnected = false;
        }
    }

    // Direct track selection: the service already accepts {"track": N}, so a
    // jump from 2 to 9 is one request, not seven Next presses.
    function playTrack(trackNumber) {
        root.requestedTrack = trackNumber;
        sendCommand("/play", JSON.stringify({ "track": trackNumber }));
    }

    function playPauseTapped() {
        if (isPlaying) {
            sendCommand("/pause");
        } else if (isPaused) {
            sendCommand("/resume");
        } else {
            // No track argument: the service starts at the first audio track.
            root.requestedTrack = 0;
            sendCommand("/play");
        }
    }

    function nextTapped() {
        // The target is knowable, so the loading line can name it.
        if (currentTrack > 0 && currentTrack < totalTracks)
            root.requestedTrack = currentTrack + 1;
        else
            root.requestedTrack = 0;
        sendCommand("/next");
    }

    function previousTapped() {
        // Deliberately not predicted: the controller restarts the current
        // track if more than ~3 s has elapsed, so the target is ambiguous and
        // guessing would put a wrong number on screen.
        root.requestedTrack = 0;
        sendCommand("/previous");
    }

    Timer {
        interval: root.pollIntervalMs
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.poll()
    }

    // Never leave a popup open over a UI that has stopped accepting input.
    onLoadingChanged: if (loading) trackPopup.close()
    onBackendConnectedChanged: if (!backendConnected) trackPopup.close()

    // ---- chrome --------------------------------------------------------

    // The single content root. Everything visible lives in here, so both
    // landscape orientations are the same UI at a different rotation.
    // Rotating an Item also rotates hit-testing, so touches keep landing on
    // the controls where they are drawn.
    Item {
        id: contentRoot
        anchors.fill: parent

        rotation: root.contentRotation
        // Item.Center is the default; stated explicitly because the whole
        // approach depends on it. A 180 flip about the centre covers exactly
        // the same area, so no width/height swap is needed.
        transformOrigin: Item.Center

        // Crossfade rather than a spin: animating the rotation itself would
        // sweep through 90 degrees and momentarily look like a portrait
        // layout, which this feature is specifically not supposed to do.
        Behavior on rotation {
            SequentialAnimation {
                NumberAnimation {
                    target: contentRoot; property: "opacity"
                    to: 0; duration: 90
                }
                PropertyAction { }      // applies the new rotation
                NumberAnimation {
                    target: contentRoot; property: "opacity"
                    to: 1; duration: 120
                }
            }
        }

        // Subtle disc motif behind the readout. Static, two rectangles, no
        // animation -- presence without distraction.
        Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            y: parent.height * 0.30
            width: Math.min(parent.width, parent.height) * 0.62
            height: width
            radius: width / 2
            color: "transparent"
            border.width: 1
            border.color: root.colorPrimaryText
            opacity: 0.05
            visible: root.showsPlayer
        }
        Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            y: parent.height * 0.30 + Math.min(parent.width, parent.height) * 0.155
            width: Math.min(parent.width, parent.height) * 0.31
            height: width
            radius: width / 2
            color: "transparent"
            border.width: 1
            border.color: root.colorAccent
            opacity: 0.07
            visible: root.showsPlayer
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.leftMargin: 40
            anchors.rightMargin: 40
            anchors.topMargin: 22
            anchors.bottomMargin: 26
            spacing: 0

            // ---- title bar --------------------------------------------

            RowLayout {
                Layout.fillWidth: true
                spacing: 12

                Label {
                    text: "NEXUS CD PLAYER"
                    color: root.colorSecondaryText
                    font.pixelSize: 15
                    font.letterSpacing: 3
                    font.bold: true
                }

                Item { Layout.fillWidth: true }

                // ---- TEMPORARY orientation debug ----------------------
                // Development build only, to make physical validation of the
                // sensor easy. Delete this Label and the sensorOrientation
                // property once the flip is confirmed on hardware.
                Label {
                    text: "Sensor: " + root.sensorOrientation
                    color: root.colorSecondaryText
                    font.pixelSize: 11
                    opacity: 0.45
                }
                // ---- end TEMPORARY -----------------------------------

                // Subtle connection dot; the only developer-ish affordance left.
                Rectangle {
                    width: 9
                    height: 9
                    radius: 4.5
                    color: root.backendConnected ? root.colorOk : root.colorDanger
                    opacity: 0.75
                }
            }

            Item { Layout.fillHeight: true; Layout.preferredHeight: 1 }

            // ---- readout ----------------------------------------------

            Label {
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                text: root.headlineText()
                color: root.colorPrimaryText
                font.pixelSize: root.showsPlayer && !root.loading ? 64 : 42
                font.letterSpacing: root.showsPlayer && !root.loading ? 6 : 0
                font.bold: true
                elide: Text.ElideRight
            }

            Label {
                Layout.fillWidth: true
                Layout.topMargin: 6
                horizontalAlignment: Text.AlignHCenter
                text: root.subText()
                color: root.colorSecondaryText
                font.pixelSize: 19
                visible: text !== ""
            }

            // Elapsed / duration. Hidden while loading: there is no honest
            // progress to show during spin-up, so none is invented.
            Label {
                Layout.fillWidth: true
                Layout.topMargin: 14
                horizontalAlignment: Text.AlignHCenter
                text: root.formatTime(root.trackElapsed) + "  /  "
                      + root.formatTime(root.trackDuration)
                color: root.colorPrimaryText
                font.pixelSize: 30
                opacity: 0.92
                visible: root.showsPlayer && !root.loading
            }

            // ---- progress ---------------------------------------------

            Item {
                Layout.fillWidth: true
                Layout.topMargin: 18
                Layout.preferredHeight: 6
                Layout.maximumWidth: 720
                Layout.alignment: Qt.AlignHCenter
                visible: root.showsPlayer

                Rectangle {
                    id: progressTrack
                    anchors.fill: parent
                    radius: height / 2
                    color: root.colorSurfaceHigh
                }

                Rectangle {
                    height: parent.height
                    radius: height / 2
                    color: root.colorAccent
                    opacity: root.loading ? 0.25 : 1.0
                    // Display only -- there is no MouseArea, so no scrubbing.
                    width: {
                        if (root.loading || root.trackDuration <= 0)
                            return 0;
                        var fraction = root.trackElapsed / root.trackDuration;
                        if (fraction < 0) fraction = 0;
                        if (fraction > 1) fraction = 1;
                        return progressTrack.width * fraction;
                    }
                    // Smooths the 250 ms polling into continuous movement.
                    Behavior on width {
                        NumberAnimation { duration: root.pollIntervalMs }
                    }
                }
            }

            BusyIndicator {
                Layout.alignment: Qt.AlignHCenter
                Layout.topMargin: 16
                running: root.loading
                visible: root.loading
                implicitWidth: 44
                implicitHeight: 44
            }

            // ---- error -------------------------------------------------

            Label {
                Layout.fillWidth: true
                Layout.topMargin: 12
                Layout.maximumWidth: 860
                Layout.alignment: Qt.AlignHCenter
                horizontalAlignment: Text.AlignHCenter
                text: root.errorText
                color: root.colorDanger
                font.pixelSize: 16
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
                visible: root.errorText !== "" && root.backendConnected
            }

            Item { Layout.fillHeight: true; Layout.preferredHeight: 1 }

            // ---- transport --------------------------------------------

            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: 34
                visible: root.showsPlayer || root.backendConnected

                TransportButton {
                    icon: "prev"
                    diameter: 84
                    enabled: root.transportEnabled
                    accent: root.colorAccent
                    iconColor: root.colorPrimaryText
                    surfaceColor: root.colorSurface
                    borderColor: root.colorBorder
                    onClicked: root.previousTapped()
                }

                TransportButton {
                    icon: root.isPlaying ? "pause" : "play"
                    primary: true
                    diameter: 116
                    enabled: root.transportEnabled
                    accent: root.colorAccent
                    iconColor: root.colorPrimaryText
                    surfaceColor: root.colorSurface
                    borderColor: root.colorBorder
                    onClicked: root.playPauseTapped()
                }

                TransportButton {
                    icon: "next"
                    diameter: 84
                    enabled: root.transportEnabled
                    accent: root.colorAccent
                    iconColor: root.colorPrimaryText
                    surfaceColor: root.colorSurface
                    borderColor: root.colorBorder
                    onClicked: root.nextTapped()
                }
            }

            // ---- selector + stop --------------------------------------

            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                Layout.topMargin: 26
                spacing: 20

                // Track selector: opens the direct-jump list.
                Rectangle {
                    id: selectorButton
                    implicitWidth: 260
                    implicitHeight: 60
                    radius: 12
                    visible: root.showsPlayer
                    color: selectorMouse.pressed && root.selectorEnabled
                           ? root.colorSurfaceHigh : root.colorSurface
                    border.width: 1
                    border.color: root.colorBorder
                    opacity: root.selectorEnabled ? 1.0 : 0.38
                    Behavior on color { ColorAnimation { duration: 90 } }

                    RowLayout {
                        anchors.centerIn: parent
                        spacing: 10
                        Label {
                            text: "Track " + root.pad2(root.displayTrack())
                                  + " of " + root.totalTracks
                            color: root.colorPrimaryText
                            font.pixelSize: 21
                        }
                        Label {
                            text: "▾"
                            color: root.colorAccent
                            font.pixelSize: 18
                        }
                    }

                    MouseArea {
                        id: selectorMouse
                        anchors.fill: parent
                        enabled: root.selectorEnabled
                        onClicked: trackPopup.open()
                    }
                }

                TransportButton {
                    icon: "stop"
                    diameter: 60
                    enabled: root.stopEnabled
                    visible: root.showsPlayer
                    accent: root.colorAccent
                    iconColor: root.colorPrimaryText
                    surfaceColor: root.colorSurface
                    borderColor: root.colorBorder
                    onClicked: root.sendCommand("/stop")
                }

                // Scan: prominent only when there is nothing to play,
                // secondary once a disc is loaded, hidden during playback.
                Rectangle {
                    implicitWidth: root.scanIsPrimary ? 260 : 150
                    implicitHeight: 60
                    radius: 12
                    visible: root.backendConnected
                             && (root.scanIsPrimary || root.isReady
                                 || root.isStopped)
                    color: {
                        if (root.scanIsPrimary)
                            return scanMouse.pressed ? Qt.lighter(root.colorAccent, 1.2)
                                                     : root.colorAccent;
                        return scanMouse.pressed ? root.colorSurfaceHigh
                                                 : "transparent";
                    }
                    border.width: root.scanIsPrimary ? 0 : 1
                    border.color: root.colorBorder
                    opacity: root.scanEnabled ? 1.0 : 0.38
                    Behavior on color { ColorAnimation { duration: 90 } }

                    Label {
                        anchors.centerIn: parent
                        text: root.scanIsPrimary ? "Scan for disc" : "Rescan"
                        color: root.scanIsPrimary ? "#141619"
                                                  : root.colorSecondaryText
                        font.pixelSize: root.scanIsPrimary ? 22 : 19
                        font.bold: root.scanIsPrimary
                    }

                    MouseArea {
                        id: scanMouse
                        anchors.fill: parent
                        enabled: root.scanEnabled
                        onClicked: {
                            root.requestedTrack = 0;
                            root.sendCommand("/scan");
                        }
                    }
                }
            }
        }
    }

    // ---- track selector popup ------------------------------------------

    Popup {
        id: trackPopup

        readonly property int rowHeight: 64

        width: 420
        height: Math.min(root.height * 0.82,
                         110 + Math.max(1, root.totalTracks) * rowHeight)
        anchors.centerIn: Overlay.overlay
        modal: true
        focus: true
        padding: 0
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        background: Rectangle {
            color: root.colorSurface
            radius: 16
            border.width: 1
            border.color: root.colorBorder
        }

        Overlay.modal: Rectangle {
            color: "#CC000000"
        }

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            Label {
                Layout.fillWidth: true
                Layout.margins: 22
                text: "Select Track"
                color: root.colorPrimaryText
                font.pixelSize: 24
                font.bold: true
            }

            Rectangle {
                Layout.fillWidth: true
                height: 1
                color: root.colorBorder
            }

            ListView {
                id: trackList
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: root.totalTracks
                currentIndex: root.currentTrack - 1
                boundsBehavior: Flickable.StopAtBounds

                // Open with the current track in view on a long disc.
                onVisibleChanged: if (visible) positionViewAtIndex(
                                      Math.max(0, root.currentTrack - 1),
                                      ListView.Center)

                delegate: Item {
                    width: trackList.width
                    height: trackPopup.rowHeight

                    readonly property int trackNumber: index + 1
                    readonly property bool isCurrent:
                        trackNumber === root.currentTrack

                    Rectangle {
                        anchors.fill: parent
                        color: rowMouse.pressed ? root.colorSurfaceHigh
                                                : "transparent"
                    }

                    // Accent bar marks the current track without shouting.
                    Rectangle {
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        width: 3
                        height: parent.height * 0.5
                        color: root.colorAccent
                        visible: isCurrent
                    }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 24
                        anchors.rightMargin: 24
                        spacing: 12

                        Label {
                            text: "Track " + root.pad2(trackNumber)
                            color: isCurrent ? root.colorAccent
                                             : root.colorPrimaryText
                            font.pixelSize: 22
                            font.bold: isCurrent
                            Layout.fillWidth: true
                        }

                        // Per-track durations are deliberately absent: the
                        // service exposes a duration only for the CURRENT
                        // track, and inventing the rest is not an option.
                        // This slot is where they would go.
                        Label {
                            text: isCurrent ? "Now playing" : ""
                            color: root.colorSecondaryText
                            font.pixelSize: 15
                            visible: isCurrent
                        }
                    }

                    Rectangle {
                        anchors.bottom: parent.bottom
                        width: parent.width
                        height: 1
                        color: root.colorBorder
                        opacity: 0.5
                        visible: index < root.totalTracks - 1
                    }

                    MouseArea {
                        id: rowMouse
                        anchors.fill: parent
                        onClicked: {
                            trackPopup.close();
                            // Re-selecting the current track would restart it
                            // and cost another spin-up for no benefit.
                            if (!isCurrent)
                                root.playTrack(trackNumber);
                        }
                    }
                }

                ScrollIndicator.vertical: ScrollIndicator {}
            }

            Rectangle {
                Layout.fillWidth: true
                height: 1
                color: root.colorBorder
            }

            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 64

                Label {
                    anchors.centerIn: parent
                    text: "Close"
                    color: root.colorSecondaryText
                    font.pixelSize: 20
                }

                MouseArea {
                    anchors.fill: parent
                    onClicked: trackPopup.close()
                }
            }
        }
    }
}
