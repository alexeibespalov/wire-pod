#!/bin/sh
# The Zero's green LED as Cortana's "your turn" light (run by wyoming-satellite's event hooks):
#   listen  solid on    the mic is open: speak now
#   think   blinking    got it, she's working on it
#   off     off         idle, or she's answering
# A "think" that nothing ends (a lost reply) goes off by itself after 20 s.
LED=/sys/class/leds/ACT
STATE=/run/cortana-led/state
set_led() {  # trigger brightness
    echo "$1" > $LED/trigger 2>/dev/null
    [ -n "$2" ] && echo "$2" > $LED/brightness 2>/dev/null
}
mkdir -p "$(dirname $STATE)" 2>/dev/null
echo "$1" > $STATE 2>/dev/null
case "$1" in
    listen) set_led none 1 ;;
    think)  set_led timer
            ( sleep 20; [ "$(cat $STATE 2>/dev/null)" = think ] && "$0" off ) >/dev/null 2>&1 & ;;
    *)      set_led none 0 ;;
esac
exit 0
