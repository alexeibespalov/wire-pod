# Pi Zero 2 W satellite ("cortana-downstairs", 192.168.1.96)

These are Cortana's ears and mouth. The brain is `cortana-agent` on the Pi 5. The Zero runs:

* `wyoming-openwakeword` (`okay_nabu`, local)
* `wyoming-satellite` on :10700 (mic: USB dongle `plughw:0,0`; speaker: Bluetooth through
  BlueALSA), with `wyoming-satellite-followup.patch` applied: when the server sends
  `detection`, it listens again without the wake word (follow-up questions)
* `bt-speaker-connect.timer`: every minute (and 20 s after boot), reconnects the trusted speaker if it dropped and, if its audio stream isn't open, opens it with a moment of silence. bluealsa then holds it open (`--keep-alive=2000000`), so the Echo says "Playing from Cortana Downstairs" once, not before every reply
* the green LED (`cortana-led`, from the satellite's event hooks): **on** = the mic is open,
  speak now; **blinking** = thinking; **off** = idle or answering. The mic opens right after
  the wake word (it isn't muted during the chime), so "Okay Nabu, ..." can be said in one go
* `mpd` on :6600: the NAS music share (`/mnt/music`, read-only CIFS) played straight to the
  speaker, driven by cortana-agent (`app/music.py`). `cortana-play` (the satellite's speaker
  command) pauses it for chimes and replies; the agent resumes it after
* `cortana-nowplaying` (root): a media player registered with BlueZ, so the speaker's screen
  shows the song (title / artist / album, from MPD) or "Cortana" and the sentence she's
  saying (the agent sends `say <sentence>` and `cortana-play` sends `busy` / `quiet` on
  MPD's `cortana` channel). The speaker's play / pause / next buttons go to MPD

`setup.sh [SPEAKER_MAC]` (run from the laptop) applies the patch, installs BlueALSA, writes
the satellite drop-in (`/etc/systemd/system/wyoming-satellite.service.d/cortana.conf`) and
restarts the satellite. It is idempotent. Re-run it after updating wyoming-satellite,
because a `git pull` there drops the patch.

Pair a new speaker first, on the Zero:
`bluetoothctl` -> `scan on` -> `pair <MAC>` -> `trust <MAC>` -> `connect <MAC>`.

The speaker is an Echo Show 8 (`74:A7:EA:AF:EF:CC`). To pair an Echo, say "Alexa, pair
Bluetooth" while the Zero is scanning, and pair from the Zero (the Echo's own screen only
lists speakers *it* can play to, and says it found nothing; that's fine). An Echo needs the
Zero to look like a computer (`Class = 0x00010c`), offer only `a2dp-source`, and keep one
format (BlueALSA `--a2dp-force-audio-cd`, `cortana_speaker` at 2 ch / 44.1 kHz in
`/etc/asound.conf`): mono shows NOT_SUPPORTED on the Echo, and a format change drops the
link. `setup.sh` does all of that. The Echo plays one stream at a time.

`music.sh` (after `setup.sh <MAC>`) installs MPD, mounts the share and starts the first
library scan.

Home Assistant must NOT have a Wyoming entry for this satellite: only one server can hold
it, and it's cortana-agent now.
