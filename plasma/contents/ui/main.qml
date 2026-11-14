/*
 * Teju Cast - system tray widget for tejucast (https://github.com/adurysurya/tejucast)
 * SPDX-License-Identifier: MIT
 */
import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts

import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasma5support as P5Support
import org.kde.plasma.plasmoid

PlasmoidItem {
    id: root

    // tejucast is usually linked into ~/.local/bin, which plasmashell's PATH may lack.
    readonly property string tejucast: 'PATH="$HOME/.local/bin:$PATH" tejucast'

    property var status: ({ state: "stopped" })
    property var devices: []
    property bool scanning: false
    property bool installed: true
    property double optimisticUntil: 0   // keep "starting" visible until the service reports in

    readonly property bool busyState: ["starting", "choosing", "connecting", "stopping"].indexOf(status.state) >= 0
    readonly property bool streaming: status.state === "streaming"
    readonly property bool active: busyState || streaming
    readonly property string modeLabel: status.mode === "extend" ? i18n("Extending desktop") : i18n("Mirroring")

    // TVs found on the network, plus the one we're casting to even if discovery missed it.
    readonly property var tvList: {
        const list = devices.slice();
        if (active && status.host && !list.some(d => d.host === status.host)) {
            list.unshift({ name: status.tv || status.host, host: status.host, model: "", online: true });
        }
        return list;
    }

    Plasmoid.icon: "video-television-symbolic"
    Plasmoid.status: PlasmaCore.Types.ActiveStatus
    Plasmoid.busy: scanning

    toolTipMainText: i18n("Teju Cast")
    toolTipSubText: streaming ? i18n("%1 to %2", modeLabel, status.tv)
                  : busyState ? i18n("Connecting to %1…", status.tv)
                  : i18n("Not casting")

    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: i18n("Search for TVs")
            icon.name: "view-refresh-symbolic"
            enabled: !root.scanning
            onTriggered: root.scan()
        },
        PlasmaCore.Action {
            text: i18n("Stop Casting")
            icon.name: "media-playback-stop-symbolic"
            visible: root.active
            onTriggered: root.stop()
        }
    ]

    // ------------------------------------------------------------------ backend

    P5Support.DataSource {
        id: exec
        engine: "executable"
        connectedSources: []
        property var callbacks: ({})

        onNewData: (sourceName, data) => {
            const cb = callbacks[sourceName];
            delete callbacks[sourceName];
            disconnectSource(sourceName);
            if (cb) {
                cb(data["exit code"], data["stdout"] || "", data["stderr"] || "");
            }
        }

        function run(cmd, cb) {
            callbacks[cmd] = cb || null;
            connectSource(cmd);
        }
    }

    function q(s) {
        return "'" + String(s).replace(/'/g, "'\\''") + "'";
    }

    function scan() {
        if (scanning) {
            return;
        }
        scanning = true;
        exec.run(tejucast + " --list --json", (code, out) => {
            scanning = false;
            installed = code !== 127;
            try {
                devices = JSON.parse(out);
            } catch (e) {
                // keep the previous list
            }
        });
    }

    function refreshStatus() {
        exec.run('cat "${XDG_RUNTIME_DIR:-/tmp}/tejucast/status.json" 2>/dev/null; echo; systemctl --user is-active tejucast',
                 (code, out) => {
            const lines = out.trim().split("\n");
            const unit = lines.pop();
            let st = { state: "stopped" };
            try {
                const text = lines.join("\n").trim();
                if (text.length > 0) {
                    st = JSON.parse(text);
                }
            } catch (e) {}
            const running = ["active", "activating", "deactivating", "reloading"].indexOf(unit) >= 0;
            if (Date.now() < optimisticUntil && (!running || st.state === "stopped" || st.state === "error")) {
                return; // just clicked Start: the service hasn't written its first status yet
            }
            if (running && (st.state === "stopped" || st.state === "error")) {
                st.state = "starting";
            } else if (!running && st.state !== "error") {
                st.state = "stopped";
            }
            status = st;
        });
    }

    function start(dev, mode) {
        const c = Plasmoid.configuration;
        let cmd = tejucast + " --background --host " + q(dev.host) + " --label " + q(dev.name)
                + " --delay " + c.delay + " --height " + c.height;
        if (mode === "extend") {
            cmd += " --extend";
        }
        if (c.audioMode === "both") {
            cmd += " --laptop-audio";
        } else if (c.audioMode === "off") {
            cmd += " --no-audio";
        }
        optimisticUntil = Date.now() + 8000;
        status = { state: "starting", tv: dev.name, host: dev.host, mode: mode };
        exec.run(cmd, (code, out, err) => {
            if (code !== 0) {
                optimisticUntil = 0;
                status = { state: "error", tv: dev.name, host: dev.host, mode: mode,
                           message: (err || out || i18n("Could not start tejucast")).trim() };
            }
        });
    }

    function stop() {
        optimisticUntil = 0;
        status = Object.assign({}, status, { state: "stopping" });
        exec.run(tejucast + " --stop", () => refreshStatus());
    }

    Timer {
        interval: (root.expanded || root.active) ? 1500 : 6000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.refreshStatus()
    }

    onExpandedChanged: isExpanded => {
        if (isExpanded) {
            scan();
            refreshStatus();
        }
    }

    Component.onCompleted: scan()

    // ------------------------------------------------------------------ panel icon

    compactRepresentation: MouseArea {
        id: compact

        property bool wasExpanded: false

        hoverEnabled: true
        acceptedButtons: Qt.LeftButton | Qt.MiddleButton
        onPressed: wasExpanded = root.expanded
        onClicked: mouse => {
            if (mouse.button === Qt.MiddleButton) {
                if (root.active) {
                    root.stop();
                }
            } else {
                root.expanded = !wasExpanded;
            }
        }

        Kirigami.Icon {
            anchors.fill: parent
            source: Plasmoid.icon
            active: compact.containsMouse
        }

        // small dot: green while casting, amber while connecting
        Rectangle {
            visible: root.active
            width: Math.max(4, Math.round(Math.min(parent.width, parent.height) * 0.36))
            height: width
            radius: width / 2
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            color: root.streaming ? Kirigami.Theme.positiveTextColor : Kirigami.Theme.neutralTextColor
            border.width: 1
            border.color: Kirigami.Theme.backgroundColor
        }
    }

    // ------------------------------------------------------------------ popup

    fullRepresentation: PlasmaExtras.Representation {
        Layout.minimumWidth: Kirigami.Units.gridUnit * 20
        Layout.minimumHeight: Kirigami.Units.gridUnit * 15
        Layout.preferredHeight: Kirigami.Units.gridUnit * 19
        collapseMarginsHint: true

        header: PlasmaExtras.PlasmoidHeading {
            // The system tray draws its own heading (title + actions); only show ours elsewhere.
            visible: !(Plasmoid.containmentDisplayHints & PlasmaCore.Types.ContainmentDrawsPlasmoidHeading)
            contentItem: RowLayout {
                Kirigami.Heading {
                    Layout.fillWidth: true
                    text: i18n("Teju Cast")
                    level: 1
                }
                PlasmaComponents3.ToolButton {
                    icon.name: "view-refresh-symbolic"
                    enabled: !root.scanning
                    onClicked: root.scan()
                    PlasmaComponents3.ToolTip.text: i18n("Search for TVs")
                    PlasmaComponents3.ToolTip.visible: hovered
                }
            }
        }

        contentItem: ColumnLayout {
            spacing: 0

            Kirigami.InlineMessage {
                Layout.fillWidth: true
                Layout.margins: Kirigami.Units.smallSpacing
                type: Kirigami.MessageType.Error
                visible: root.status.state === "error"
                text: root.status.message || i18n("Casting stopped because of an error.")
            }

            PlasmaComponents3.ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                PlasmaComponents3.ScrollBar.horizontal.policy: PlasmaComponents3.ScrollBar.AlwaysOff
                contentWidth: availableWidth - tvView.leftMargin - tvView.rightMargin

                contentItem: ListView {
                    id: tvView

                    model: root.tvList
                    clip: true
                    currentIndex: -1
                    spacing: Kirigami.Units.smallSpacing
                    topMargin: Kirigami.Units.largeSpacing
                    leftMargin: Kirigami.Units.largeSpacing
                    rightMargin: Kirigami.Units.largeSpacing
                    bottomMargin: Kirigami.Units.largeSpacing
                    highlight: PlasmaExtras.Highlight {}
                    highlightMoveDuration: Kirigami.Units.shortDuration
                    highlightResizeDuration: Kirigami.Units.shortDuration

                    delegate: PlasmaExtras.ExpandableListItem {
                        id: item

                        required property var modelData
                        required index

                        readonly property bool casting: root.active && root.status.host === modelData.host

                        icon: "video-television"
                        iconEmblem: casting && root.streaming ? "emblem-checked" : ""
                        title: modelData.name
                        subtitle: {
                            if (casting) {
                                switch (root.status.state) {
                                case "starting": return i18n("Starting…");
                                case "choosing": return i18n("Choose what to share in the dialog…");
                                case "connecting": return i18n("Connecting…");
                                case "stopping": return i18n("Stopping…");
                                case "streaming":
                                    if (root.status.codec === "hls") {
                                        return i18n("%1 · video stream mode", root.modeLabel);
                                    }
                                    return i18n("%1 · %2p · %3 ms", root.modeLabel, root.status.height, root.status.delay);
                                }
                            }
                            if (modelData.online === false) {
                                return i18n("Not found on the network");
                            }
                            return modelData.model || i18n("Available");
                        }
                        isBusy: casting && root.busyState
                        showDefaultActionButtonWhenBusy: true
                        isDefault: casting
                        enabled: modelData.online !== false || casting

                        defaultActionButtonAction: QQC2.Action {
                            icon.name: item.casting ? "media-playback-stop-symbolic" : "video-display"
                            text: item.casting ? i18n("Stop") : i18n("Mirror")
                            onTriggered: item.casting ? root.stop() : root.start(item.modelData, "mirror")
                        }

                        contextualActions: [
                            QQC2.Action {
                                icon.name: "window-duplicate"
                                text: i18n("Mirror Screen")
                                enabled: !(item.casting && root.status.mode === "mirror")
                                onTriggered: root.start(item.modelData, "mirror")
                            },
                            QQC2.Action {
                                icon.name: "view-split-left-right"
                                text: i18n("Extend Desktop")
                                enabled: !(item.casting && root.status.mode === "extend")
                                onTriggered: root.start(item.modelData, "extend")
                            }
                        ]
                    }

                    PlasmaExtras.PlaceholderMessage {
                        anchors.centerIn: parent
                        width: parent.width - Kirigami.Units.gridUnit * 4
                        visible: tvView.count === 0
                        iconName: root.installed ? "video-television" : "dialog-error"
                        text: !root.installed ? i18n("tejucast is not installed")
                            : root.scanning ? i18n("Searching for TVs…")
                            : i18n("No TVs found")
                        explanation: !root.installed
                            ? i18n("Link tejucast.py to ~/.local/bin/tejucast (see github.com/adurysurya/tejucast).")
                            : root.scanning ? "" : i18n("Make sure the TV is on and on the same network.")
                        helpfulAction: QQC2.Action {
                            text: i18n("Search Again")
                            icon.name: "view-refresh-symbolic"
                            enabled: !root.scanning && root.installed
                            onTriggered: root.scan()
                        }
                    }
                }
            }
        }

        footer: PlasmaExtras.PlasmoidHeading {
            contentItem: GridLayout {
                columns: 2
                columnSpacing: Kirigami.Units.largeSpacing
                rowSpacing: Kirigami.Units.smallSpacing

                PlasmaComponents3.Label {
                    text: i18n("Sound:")
                    Layout.alignment: Qt.AlignRight
                }
                PlasmaComponents3.ComboBox {
                    Layout.fillWidth: true
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 10
                    textRole: "text"
                    valueRole: "value"
                    model: [
                        { text: i18n("Play on the TV"), value: "tv" },
                        { text: i18n("TV and this computer"), value: "both" },
                        { text: i18n("Off"), value: "off" }
                    ]
                    Component.onCompleted: currentIndex = Math.max(0, indexOfValue(Plasmoid.configuration.audioMode))
                    onActivated: Plasmoid.configuration.audioMode = currentValue
                }

                PlasmaComponents3.Label {
                    text: i18n("Quality:")
                    Layout.alignment: Qt.AlignRight
                }
                PlasmaComponents3.ComboBox {
                    Layout.fillWidth: true
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 10
                    textRole: "text"
                    valueRole: "value"
                    model: [
                        { text: i18n("1080p (sharper)"), value: 1080 },
                        { text: i18n("720p (smoother on weak Wi-Fi)"), value: 720 }
                    ]
                    Component.onCompleted: currentIndex = Math.max(0, indexOfValue(Plasmoid.configuration.height))
                    onActivated: Plasmoid.configuration.height = currentValue
                }

                PlasmaComponents3.Label {
                    text: i18n("Delay:")
                    Layout.alignment: Qt.AlignRight
                }
                RowLayout {
                    Layout.fillWidth: true
                    PlasmaComponents3.Slider {
                        id: delaySlider
                        Layout.fillWidth: true
                        from: 80
                        to: 400
                        stepSize: 10
                        value: Plasmoid.configuration.delay
                        onMoved: Plasmoid.configuration.delay = value
                    }
                    PlasmaComponents3.Label {
                        text: i18n("%1 ms", delaySlider.value)
                        Layout.minimumWidth: Kirigami.Units.gridUnit * 3
                    }
                }

                PlasmaComponents3.Label {
                    Layout.columnSpan: 2
                    Layout.fillWidth: true
                    visible: root.active
                    text: i18n("Changes apply the next time you start casting.")
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                    wrapMode: Text.WordWrap
                }
            }
        }
    }
}
