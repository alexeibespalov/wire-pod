#!/bin/sh
# The satellite's speaker command (chimes and Cortana's voice). The Echo takes one stream
# at a time, so pause the music (MPD) first; cortana-agent resumes it after the conversation.
# The `cortana` MPD channel tells cortana-nowplaying to show "Cortana" on the Echo's screen.
mpc -q sendmessage cortana busy >/dev/null 2>&1 &
if mpc status 2>/dev/null | grep -q '^\[playing\]'; then
    mpc -q pause
fi
# MPD lets go of the speaker a moment after pausing: retry while it's busy. aplay exits
# on a failed open before reading stdin, so the audio is still there for the next try.
rc=1
i=0
while [ $i -lt 15 ]; do
    if aplay -q -D cortana_speaker "$@" 2>/tmp/cortana-play.err; then rc=0; break; fi
    grep -q "busy" /tmp/cortana-play.err || break
    i=$((i + 1)); sleep 0.1
done
mpc -q sendmessage cortana quiet >/dev/null 2>&1 &
[ $rc -eq 0 ] || cat /tmp/cortana-play.err >&2
exit $rc
