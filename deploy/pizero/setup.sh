#!/usr/bin/env bash
# Turn the Pi Zero satellite (wyoming-satellite) into Cortana's ears and mouth.
#
#   ./deploy/pizero/setup.sh                      # patch + chimes, no speaker yet
#   ./deploy/pizero/setup.sh AA:BB:CC:DD:EE:FF    # ...and play through this Bluetooth speaker
#
# The speaker must already be paired + trusted (bluetoothctl: scan on, pair, trust, connect).
# Idempotent: safe to re-run (e.g. after updating wyoming-satellite, which drops the patch).
set -euo pipefail

ZERO="${ZERO_HOST:-john@192.168.1.96}"
KEY="${ZERO_KEY:-$HOME/.ssh/rpi_key}"
PI="${PI_HOST:-john@192.168.1.39}"        # the Pi 5: Home Assistant and both agents
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAC="${1:-}"
[[ -z "$MAC" || "$MAC" =~ ^([0-9A-F]{2}:){5}[0-9A-F]{2}$ ]] || { echo "bad MAC: $MAC (use upper case)"; exit 1; }

scp -q -i "$KEY" "$HERE/wyoming-satellite-followup.patch" "$HERE/bt-speaker-connect.sh" \
    "$HERE/cortana-play.sh" "$HERE/cortana-led.sh" "$ZERO:/tmp/"
# the "Cortana" / "Hey Cortana" wake word (trained with deploy/pizero/wakeword/README.md)
[ -f "$HERE/wakeword/cortana.tflite" ] && scp -q -i "$KEY" "$HERE/wakeword/cortana.tflite" "$ZERO:/tmp/"

ssh -i "$KEY" "$ZERO" MAC="$MAC" bash -s <<'REMOTE'
set -euo pipefail
cd ~/wyoming-satellite

# 1. follow-up listening patch (server sends `detection` -> listen without the wake word)
if git apply --check /tmp/wyoming-satellite-followup.patch 2>/dev/null; then
    git apply /tmp/wyoming-satellite-followup.patch && echo "patch: applied"
elif git apply -R --check /tmp/wyoming-satellite-followup.patch 2>/dev/null; then
    echo "patch: already applied"
else
    echo "patch: does NOT apply to this wyoming-satellite version - follow-ups won't work"; exit 1
fi

# 2. Bluetooth speaker (BlueALSA: light enough for 416 MB, no PipeWire)
SND=""
if [ -n "$MAC" ]; then
    dpkg -s bluez-alsa-utils libasound2-plugins >/dev/null 2>&1 ||
        sudo -n apt-get install -y -q bluez-alsa-utils libasound2-plugin-bluez libasound2-plugins
    sudo -n usermod -aG audio,bluetooth john
    sudo -n install -m 755 /tmp/bt-speaker-connect.sh /usr/local/bin/bt-speaker-connect
    sudo -n install -m 755 /tmp/cortana-play.sh /usr/local/bin/cortana-play
    # An Echo only takes audio from a phone/computer-type device that is a SOURCE: say
    # we're a computer, offer only a2dp-source (no sink), and stay at 44.1 kHz so the
    # stream is never renegotiated (the Echo drops the link when it is).
    # Keep-alive 2,000,000 s (~23 days, i.e. for good): the stream stays open between sounds, as
    # the Echo says "Playing from ..." each time it starts. bt-speaker-connect (re)opens it.
    BEFORE="$(cat /etc/systemd/system/bluealsa.service.d/cortana.conf /etc/asound.conf 2>/dev/null | md5sum)"
    if ! grep -q "^Class = 0x00010c" /etc/bluetooth/main.conf; then
        sudo -n sed -i -E "s/^#?\s*Class\s*=.*/Class = 0x00010c/" /etc/bluetooth/main.conf
        sudo -n systemctl restart bluetooth
    fi
    sudo -n mkdir -p /etc/systemd/system/bluealsa.service.d
    printf '[Service]\nExecStart=\nExecStart=/usr/bin/bluealsa -S --keep-alive=2000000 --a2dp-force-audio-cd -p a2dp-source\n' \
        | sudo -n tee /etc/systemd/system/bluealsa.service.d/cortana.conf >/dev/null
    echo "SPEAKER_MAC=$MAC" | sudo -n tee /etc/default/cortana-speaker >/dev/null
    sudo -n tee /etc/systemd/system/bt-speaker-connect.service >/dev/null <<UNIT
[Unit]
Description=Connect Cortana's Bluetooth speaker if it dropped
After=bluetooth.service bluealsa.service
[Service]
Type=oneshot
EnvironmentFile=/etc/default/cortana-speaker
ExecStart=/usr/local/bin/bt-speaker-connect \$SPEAKER_MAC
UNIT
    sudo -n tee /etc/systemd/system/bt-speaker-connect.timer >/dev/null <<UNIT
[Unit]
Description=Keep Cortana's Bluetooth speaker connected
[Timer]
OnBootSec=20
OnUnitActiveSec=60
[Install]
WantedBy=timers.target
UNIT
    # Always feed the speaker 2 ch / 44.1 kHz (what bluealsa is forced to): given anything
    # else, BlueALSA renegotiates the A2DP stream, and an Echo drops the link (mono: it
    # shows NOT_SUPPORTED). Music (MPD) and the satellite both play through this.
    sudo -n tee /etc/asound.conf >/dev/null <<ASOUND
# Written by deploy/pizero/setup.sh (Cortana).
pcm.cortana_speaker {
  type plug
  rate_converter "speexrate_medium"   # Piper's 22 kHz -> 44.1 kHz; ALSA's built-in linear is tinny
  slave {
    pcm { type bluealsa device "$MAC" profile "a2dp" }
    channels 2
    rate 44100
    format S16_LE
  }
}
ASOUND
    sudo -n systemctl daemon-reload
    sudo -n systemctl enable --now bluealsa.service bt-speaker-connect.timer
    # restarting bluealsa drops the speaker for up to a minute: only when its setup changed
    if [ "$BEFORE" != "$(cat /etc/systemd/system/bluealsa.service.d/cortana.conf /etc/asound.conf | md5sum)" ]; then
        sudo -n systemctl restart bluealsa.service bt-speaker-connect.timer
    fi
    SND="--snd-command 'cortana-play -r 22050 -c 1 -f S16_LE -t raw' --snd-command-rate 22050 --snd-command-width 2 --snd-command-channels 1"
fi

# 3. the green LED as a "your turn" light (cortana-led: on = speak now, blinking = thinking,
#    off = idle/answering). The satellite runs as john: let it drive the LED, keep the
#    blink trigger loaded, and give it a state dir.
sudo -n install -m 755 /tmp/cortana-led.sh /usr/local/bin/cortana-led
echo ledtrig-timer | sudo -n tee /etc/modules-load.d/cortana-led.conf >/dev/null
sudo -n modprobe ledtrig-timer
echo "d /run/cortana-led 0755 john john -" | sudo -n tee /etc/tmpfiles.d/cortana-led.conf >/dev/null
sudo -n systemd-tmpfiles --create /etc/tmpfiles.d/cortana-led.conf
sudo -n tee /etc/udev/rules.d/90-cortana-led.rules >/dev/null <<'RULE'
ACTION=="add", SUBSYSTEM=="leds", KERNEL=="ACT", RUN+="/bin/chgrp john /sys%p/trigger /sys%p/brightness", RUN+="/bin/chmod g+w /sys%p/trigger /sys%p/brightness"
RULE
sudo -n udevadm control --reload
sudo -n udevadm trigger --action=add /sys/class/leds/ACT
LED="--startup-command 'cortana-led off' --streaming-start-command 'cortana-led listen' --streaming-stop-command 'cortana-led think' --tts-start-command 'cortana-led off' --tts-played-command 'cortana-led off' --error-command 'cortana-led off' --disconnected-command 'cortana-led off'"

# 3b. USB mic hardware gain: it ships at 0 of 16 (0 dB), too quiet for the wake word across a
#     room. 8 = +12 dB: room noise peaks ~6.7k/32k there, so speech has headroom (16 nears clipping).
amixer -q -c 0 cset name='Mic Capture Volume' "${MIC_GAIN:-8}" && sudo -n alsactl store

# 3c. wake words: our "Cortana" model (if deployed) next to the built-in okay_nabu
WAKE="--wake-word-name okay_nabu"
if [ -f /tmp/cortana.tflite ]; then
    install -D -m 644 /tmp/cortana.tflite ~/wyoming-openwakeword/custom/cortana.tflite
    sudo -n mkdir -p /etc/systemd/system/wyoming-openwakeword.service.d
    sudo -n tee /etc/systemd/system/wyoming-openwakeword.service.d/cortana.conf >/dev/null <<UNIT
# Written by deploy/pizero/setup.sh (Cortana): load the custom wake word models.
[Service]
ExecStart=
ExecStart=/home/john/wyoming-openwakeword/.venv/bin/python3 -m wyoming_openwakeword \\
  --uri tcp://127.0.0.1:10400 --custom-model-dir /home/john/wyoming-openwakeword/custom \\
  --threshold ${WAKE_THRESHOLD:-0.5}
UNIT
    sudo -n systemctl daemon-reload
    sudo -n systemctl restart wyoming-openwakeword
    WAKE="--wake-word-name cortana --wake-word-name okay_nabu"
fi

# 4. satellite command: same as before + speaker + chimes + LED (drop-in replaces ExecStart)
# The mic stays open during the wake chime: over Bluetooth the chime takes ~1 s to come out,
# and muting through it (the default, +0.5 s) cut off the start of every request.
S=/home/john/wyoming-satellite/sounds
sudo -n mkdir -p /etc/systemd/system/wyoming-satellite.service.d
sudo -n tee /etc/systemd/system/wyoming-satellite.service.d/cortana.conf >/dev/null <<UNIT
# Written by deploy/pizero/setup.sh (Cortana). The server is cortana-agent on the Pi 5.
[Service]
ExecStart=
ExecStart=/home/john/wyoming-satellite/.venv/bin/python3 -m wyoming_satellite \\
  --name "Cortana Downstairs" --area "Downstairs" \\
  --uri tcp://0.0.0.0:10700 \\
  --mic-command "arecord -q -D plughw:0,0 -r 16000 -c 1 -f S16_LE -t raw" \\
  --wake-uri tcp://127.0.0.1:10400 $WAKE \\
  $SND \\
  --awake-wav $S/awake.wav --done-wav $S/done.wav \\
  --mic-no-mute-during-awake-wav --mic-seconds-to-mute-after-awake-wav 0 \\
  --timer-finished-wav $S/timer_finished.wav --timer-finished-wav-repeat 2 0.6 \\
  $LED
UNIT
sudo -n systemctl daemon-reload
sudo -n systemctl restart wyoming-satellite
sleep 4
systemctl is-active wyoming-openwakeword wyoming-satellite

# 5. the MHS-3.5" SPI screen (ILI9486 panel + XPT2046 touch, lcdwiki.com/MHS-3.5inch_RPi_Display).
#    Same pins as the stock piscreen overlay; `drm` picks the tiny/ili9486 DRM driver. Not
#    LCDwiki's LCD-show script, which is written for old Raspberry Pi OS and breaks Trixie.
#    The screen draws ~100 mA more: on a weak supply the Zero reboots or loses Wi-Fi.
CFG=/boot/firmware/config.txt
SCREEN="$(printf '%s\n' '# >>> cortana screen (deploy/pizero/setup.sh)' '[all]' 'dtparam=spi=on' \
    "dtoverlay=piscreen,drm,speed=24000000,rotate=${LCD_ROTATE:-0}" '# <<< cortana screen')"
if ! grep -qxF "dtoverlay=piscreen,drm,speed=24000000,rotate=${LCD_ROTATE:-0}" "$CFG"; then
    sudo -n cp "$CFG" "$CFG.bak-$(date +%Y%m%d%H%M%S)"
    REBOOT=1
fi
sudo -n sed -i -e '/^# >>> cortana screen/,/^# <<< cortana screen/d' \
    -e '/^# MHS-3.5 SPI screen/d' -e '/^dtparam=spi=on$/d' -e '/^dtoverlay=piscreen/d' "$CFG"
printf '%s\n' "$SCREEN" | sudo -n tee -a "$CFG" >/dev/null
[ -n "${REBOOT:-}" ] && echo "screen: config.txt changed - reboot the Zero (sudo reboot) to apply"
# Touch: with rotate=0 the panel's up-down axis comes out upside down (taps on the bottom row of
# tiles landed 4 rows from the top). libinput flips it back: no reboot, cage rereads it on start.
RULE=/etc/udev/rules.d/99-cortana-touch.rules
WANT='ACTION=="add|change", KERNEL=="event*", ATTRS{name}=="ADS7846 Touchscreen", ENV{LIBINPUT_CALIBRATION_MATRIX}="1 0 0 0 -1 1"'
if [ "$(cat $RULE 2>/dev/null)" != "$WANT" ]; then
    echo "$WANT" | sudo -n tee $RULE >/dev/null
    sudo -n udevadm control --reload
    sudo -n udevadm trigger --action=change --subsystem-match=input --sysname-match="event*"; sudo -n udevadm settle
    sudo -n systemctl try-restart cortana-dash
    echo "touch: calibration updated"
fi
true
REMOTE

# 6. the dashboard on the screen (deploy/pizero/dashboard): cage + foot + a Textual app on tty1.
#    The Home Assistant token comes from Cortana's settings on the Pi 5 and goes straight to
#    /etc/cortana-dash.env (600) on the Zero: never through a file here.
tar -C "$HERE" -czf - --exclude=__pycache__ --exclude=.pytest_cache --exclude=tests dashboard |
    ssh -i "$KEY" "$ZERO" 'rm -rf /tmp/cortana-dash && mkdir -p /tmp/cortana-dash && tar -xzf - -C /tmp/cortana-dash'
ssh -i "$KEY" "$PI" "sed -n 's/^HA_TOKEN=//p' ~/wire-pod/cortana.env" | ssh -i "$KEY" "$ZERO" '
    IFS= read -r T || true
    [ -n "$T" ] || { echo "dashboard: no HA_TOKEN in ~/wire-pod/cortana.env on the Pi 5"; exit 1; }
    umask 077
    printf "HA_URL=%s\nHA_TOKEN=%s\nDASH_AGENTS=%s\n" "http://192.168.1.39:8123" "$T" \
        "cortana=http://192.168.1.39:8099/dash,vector=http://192.168.1.39:8098/dash" |
        sudo -n tee /etc/cortana-dash.env.new >/dev/null
    sudo -n chmod 600 /etc/cortana-dash.env.new && sudo -n mv /etc/cortana-dash.env.new /etc/cortana-dash.env'
ssh -i "$KEY" "$ZERO" bash -s <<'DASH'
set -euo pipefail
dpkg -s cage foot fonts-jetbrains-mono python3-venv grim >/dev/null 2>&1 ||
    sudo -n apt-get install -y -q --no-install-recommends cage foot fonts-jetbrains-mono python3-venv grim
D=/opt/cortana-dash
sudo -n mkdir -p $D && sudo -n chown john:john $D
BEFORE="$( (cd $D && cat cortana-dash.service foot.ini cortana_dash/*.py) 2>/dev/null | md5sum || true)"
find $D -mindepth 1 -maxdepth 1 ! -name venv ! -name cursors -exec rm -rf {} +
cp -a /tmp/cortana-dash/dashboard/. $D/
[ -x $D/venv/bin/python ] || python3 -m venv $D/venv
if ! cmp -s $D/requirements.txt $D/venv/.requirements; then
    $D/venv/bin/pip install -q --disable-pip-version-check -r $D/requirements.txt
    cp $D/requirements.txt $D/venv/.requirements
fi
python3 $D/blank-cursor.py $D/cursors
echo "d /var/lib/cortana-dash 0755 john john -" | sudo -n tee /etc/tmpfiles.d/cortana-dash.conf >/dev/null
sudo -n systemd-tmpfiles --create /etc/tmpfiles.d/cortana-dash.conf
sudo -n install -m 644 $D/cortana-dash.service /etc/systemd/system/cortana-dash.service
sudo -n systemctl daemon-reload
sudo -n systemctl disable getty@tty1.service >/dev/null 2>&1 || true
sudo -n systemctl enable cortana-dash.service >/dev/null 2>&1
if [ "$BEFORE" != "$(cd $D && cat cortana-dash.service foot.ini cortana_dash/*.py | md5sum)" ] ||
   ! systemctl is-active -q cortana-dash; then
    sudo -n systemctl restart cortana-dash.service
fi
sleep 6
echo "dashboard: $(systemctl is-active cortana-dash)"
DASH
