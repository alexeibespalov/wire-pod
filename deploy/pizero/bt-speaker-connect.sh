#!/bin/sh
# Keep the Bluetooth speaker connected AND its audio stream open (run every minute by a timer,
# first 20 s after boot).
#
# An Echo says "Playing from <name>" every time the A2DP stream starts. bluealsa holds the
# stream open after the last sound (--keep-alive, set very long by setup.sh), but someone has
# to open it once: after boot, after a reconnect, or if keep-alive ever runs out. So when the
# stream isn't running, play a moment of silence; the Echo announces once and then stays quiet.
MAC="$1"
[ -n "$MAC" ] || exit 0
PCM="/org/bluealsa/hci0/dev_$(echo "$MAC" | tr : _)/a2dpsrc/sink"
running() { busctl --system get-property org.bluealsa "$PCM" org.bluealsa.PCM1 Running 2>/dev/null; }
# no audio link (dropped, or connected without A2DP, e.g. after bluealsa restarted): connect
if [ -z "$(running)" ]; then
    timeout 15 bluetoothctl connect "$MAC" >/dev/null 2>&1 || exit 0
    sleep 3
fi
[ "$(running)" = "b true" ] && exit 0
# 0.1 s of 44.1 kHz stereo silence; if something else is playing the device is busy: fine.
head -c 17640 /dev/zero | aplay -q -D cortana_speaker -t raw -f cd 2>/dev/null || true
