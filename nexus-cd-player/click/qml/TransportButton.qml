/*
 * A large, touch-friendly transport control.
 *
 * Icons are drawn with Canvas rather than glyphs or image files: the device
 * font is not guaranteed to carry media symbols, and the Click package must
 * stay self-contained with no raster assets.
 *
 * Uses the inherited Item.enabled -- setting it false disables input for this
 * item and its children, so no separate flag is needed.
 */

import QtQuick 2.12

Item {
    id: control

    // "play" | "pause" | "stop" | "prev" | "next" | "scan"
    property string icon: "play"

    // Primary = filled accent (the play/pause control). Secondary = outlined.
    property bool primary: false

    property color accent: "#D9A441"
    property color iconColor: "#F3EEE4"
    property color surfaceColor: "#1A1E24"
    property color borderColor: "#2C323A"
    property color disabledColor: "#495059"

    property real diameter: 96

    signal clicked()

    implicitWidth: diameter
    implicitHeight: diameter

    opacity: enabled ? 1.0 : 0.38
    Behavior on opacity { NumberAnimation { duration: 120 } }

    Rectangle {
        id: background
        anchors.fill: parent
        radius: control.primary ? width / 2 : Math.min(width, height) * 0.28
        color: {
            var base = control.primary ? control.accent : control.surfaceColor;
            return (mouseArea.pressed && control.enabled)
                    ? Qt.lighter(base, 1.22) : base;
        }
        border.width: control.primary ? 0 : 1
        border.color: control.primary ? "transparent" : control.borderColor
        Behavior on color { ColorAnimation { duration: 90 } }
    }

    // Pressed feedback: a slight shrink reads clearly on a small screen.
    scale: (mouseArea.pressed && control.enabled) ? 0.94 : 1.0
    Behavior on scale { NumberAnimation { duration: 90 } }

    Canvas {
        id: canvas
        anchors.fill: parent
        antialiasing: true

        onPaint: {
            var ctx = getContext("2d");
            ctx.reset();

            var w = width;
            var h = height;
            var cx = w / 2;
            var cy = h / 2;
            var unit = Math.min(w, h);

            // On the filled accent button the icon must be dark to stay legible.
            ctx.fillStyle = control.enabled
                    ? (control.primary ? "#141619" : control.iconColor)
                    : control.disabledColor;

            if (control.icon === "play") {
                var pr = unit * 0.17;
                ctx.beginPath();
                ctx.moveTo(cx - pr * 0.8, cy - pr);
                ctx.lineTo(cx + pr, cy);
                ctx.lineTo(cx - pr * 0.8, cy + pr);
                ctx.closePath();
                ctx.fill();
            } else if (control.icon === "pause") {
                var bw = unit * 0.10;
                var bh = unit * 0.34;
                var gap = unit * 0.08;
                ctx.fillRect(cx - gap / 2 - bw, cy - bh / 2, bw, bh);
                ctx.fillRect(cx + gap / 2, cy - bh / 2, bw, bh);
            } else if (control.icon === "stop") {
                var side = unit * 0.28;
                ctx.fillRect(cx - side / 2, cy - side / 2, side, side);
            } else if (control.icon === "prev" || control.icon === "next") {
                var dir = (control.icon === "next") ? 1 : -1;
                var tr = unit * 0.15;
                var offset = unit * 0.055;
                // Two stacked triangles plus the end bar.
                for (var i = 0; i < 2; i++) {
                    var ox = cx + dir * (offset + i * tr * 1.15) - dir * tr * 0.55;
                    ctx.beginPath();
                    ctx.moveTo(ox - dir * tr * 0.55, cy - tr);
                    ctx.lineTo(ox + dir * tr * 0.75, cy);
                    ctx.lineTo(ox - dir * tr * 0.55, cy + tr);
                    ctx.closePath();
                    ctx.fill();
                }
                var barW = unit * 0.055;
                var barX = cx + dir * (offset + tr * 2.15) - (dir < 0 ? barW : 0);
                ctx.fillRect(barX, cy - tr, barW, tr * 2);
            } else if (control.icon === "scan") {
                // A disc: ring plus centre hole.
                ctx.lineWidth = unit * 0.075;
                ctx.strokeStyle = ctx.fillStyle;
                ctx.beginPath();
                ctx.arc(cx, cy, unit * 0.26, 0, Math.PI * 2);
                ctx.stroke();
                ctx.beginPath();
                ctx.arc(cx, cy, unit * 0.075, 0, Math.PI * 2);
                ctx.fill();
            }
        }
    }

    // Canvas does not repaint on property changes by itself.
    onIconChanged: canvas.requestPaint()
    onEnabledChanged: canvas.requestPaint()
    onIconColorChanged: canvas.requestPaint()
    onPrimaryChanged: canvas.requestPaint()

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        onClicked: control.clicked()
    }
}
