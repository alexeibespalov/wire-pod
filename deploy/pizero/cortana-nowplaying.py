#!/usr/bin/env python3
"""Tell the Bluetooth speaker what's playing, so the Echo's screen shows it instead of just
"Bluetooth": the song from MPD, or "Cortana" and what she's saying while she talks.

It is a media player registered with BlueZ (org.bluez.Media1.RegisterPlayer, MPRIS
properties), which BlueZ serves to the speaker over AVRCP. The speaker's play / pause /
next / previous buttons come back here and go to MPD.

Sources, both over MPD on localhost:
  * MPD itself (idle player/mixer): the current song and play state
  * the `cortana` channel (MPD client-to-client messages):
        say <sentence>   cortana-agent, as she starts saying each sentence
        busy             cortana-play, before any chime or reply
        quiet            cortana-agent / cortana-play, when she's done
    "Cortana" stays on screen until QUIET_HOLD_S after the last of these, then the song
    comes back.

Runs as root (BlueZ only lets root register a player); installed by deploy/pizero/music.sh.
"""
import logging
import socket
import threading
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

MPD = ("127.0.0.1", 6600)
CHANNEL = "cortana"
PATH = "/org/cortana/player"
ADAPTER = "/org/bluez/hci0"
PLAYER = "org.mpris.MediaPlayer2.Player"
QUIET_HOLD_S = 3.0
NAME = "Cortana"

log = logging.getLogger("cortana-nowplaying")


# --------------------------------------------------------------------------------- MPD
class MPDConn:
    def __init__(self):
        self.sock = socket.create_connection(MPD, timeout=10)
        self.f = self.sock.makefile("rwb")
        hello = self.f.readline()
        if not hello.startswith(b"OK MPD"):
            raise ConnectionError(hello)

    def cmd(self, line: str, timeout: float | None = 10) -> list[tuple[str, str]]:
        self.sock.settimeout(timeout)
        self.f.write(line.encode() + b"\n")
        self.f.flush()
        out = []
        while True:
            raw = self.f.readline()
            if not raw:
                raise ConnectionError("MPD closed the connection")
            s = raw.decode("utf-8", "replace").rstrip("\n")
            if s == "OK":
                return out
            if s.startswith("ACK"):
                raise RuntimeError(s)
            k, _, v = s.partition(": ")
            out.append((k, v))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def mpd_once(line: str) -> list[tuple[str, str]]:
    c = MPDConn()
    try:
        return c.cmd(line)
    finally:
        c.close()


# ----------------------------------------------------------------------- the player object
def _meta(title: str, artist: str, album: str, length_s: float, track: str) -> dbus.Dictionary:
    return dbus.Dictionary({
        "mpris:trackid": dbus.ObjectPath(f"/org/cortana/track/{track}"),
        "xesam:title": dbus.String(title or " "),
        "xesam:artist": dbus.Array([dbus.String(artist or " ")], signature="s"),
        "xesam:album": dbus.String(album or " "),
        "mpris:length": dbus.Int64(int(max(0.0, length_s) * 1_000_000)),
    }, signature="sv")


class Player(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, PATH)
        self.props = {
            "PlaybackStatus": dbus.String("Stopped"),
            "LoopStatus": dbus.String("None"),
            "Shuffle": dbus.Boolean(False),
            "Rate": dbus.Double(1.0), "MinimumRate": dbus.Double(1.0), "MaximumRate": dbus.Double(1.0),
            "Volume": dbus.Double(1.0),
            "Position": dbus.Int64(0),
            "Metadata": _meta("", "", "", 0, "none"),
            "CanGoNext": dbus.Boolean(True), "CanGoPrevious": dbus.Boolean(True),
            "CanPlay": dbus.Boolean(True), "CanPause": dbus.Boolean(True),
            "CanSeek": dbus.Boolean(False), "CanControl": dbus.Boolean(True),
        }
        self.position_at = (0.0, time.monotonic(), False)   # (seconds, when, advancing)

    def set(self, changes: dict) -> None:
        changed = {k: v for k, v in changes.items() if self.props.get(k) != v}
        if changed:
            self.props.update(changed)
            self.PropertiesChanged(PLAYER, dbus.Dictionary(changed, signature="sv"), dbus.Array([], signature="s"))

    def _position(self) -> dbus.Int64:
        pos, at, advancing = self.position_at
        if advancing:
            pos += time.monotonic() - at
        return dbus.Int64(int(pos * 1_000_000))

    # MPRIS methods: the speaker's buttons
    def _mpd(self, line):
        threading.Thread(target=lambda: _quietly(mpd_once, line), daemon=True).start()

    @dbus.service.method(PLAYER)
    def Play(self): self._mpd("play")

    @dbus.service.method(PLAYER)
    def Pause(self): self._mpd("pause 1")

    @dbus.service.method(PLAYER)
    def PlayPause(self): self._mpd("pause")

    @dbus.service.method(PLAYER)
    def Stop(self): self._mpd("pause 1")

    @dbus.service.method(PLAYER)
    def Next(self): self._mpd("next")

    @dbus.service.method(PLAYER)
    def Previous(self): self._mpd("previous")

    @dbus.service.method(PLAYER, in_signature="x")
    def Seek(self, offset): pass

    @dbus.service.method(PLAYER, in_signature="ox")
    def SetPosition(self, track, pos): pass

    # properties
    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, iface, name):
        return self._position() if name == "Position" else self.props[name]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return dbus.Dictionary({**self.props, "Position": self._position()}, signature="sv")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ssv")
    def Set(self, iface, name, value):
        pass

    @dbus.service.signal(dbus.PROPERTIES_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, iface, changed, invalidated):
        pass


def _quietly(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        log.warning("%s", e)


# ------------------------------------------------------------------------------ state
class NowPlaying:
    def __init__(self, player: Player):
        self.player = player
        self.song = {"status": "Stopped", "meta": _meta("", "", "", 0, "none"), "pos": (0.0, False)}
        self.saying = ""
        self.talk_until = 0.0
        self.talk_n = 0
        self.hold_timer = None

    def talking(self) -> bool:
        return time.monotonic() < self.talk_until

    def show(self) -> bool:
        if self.talking():
            self.player.position_at = (0.0, time.monotonic(), False)
            self.player.set({"PlaybackStatus": dbus.String("Playing"),
                             "Metadata": _meta(self.saying or NAME, NAME, "Cortana Downstairs", 0,
                                               f"say{self.talk_n}")})
        else:
            pos, advancing = self.song["pos"]
            self.player.position_at = (pos, time.monotonic(), advancing)
            self.player.set({"PlaybackStatus": dbus.String(self.song["status"]),
                             "Metadata": self.song["meta"]})
        return False

    def on_song(self, status: dict, song: dict) -> bool:
        state = {"play": "Playing", "pause": "Paused"}.get(status.get("state", ""), "Stopped")
        path = song.get("file", "")
        parts = path.split("/")
        fallback_title = parts[-1].rsplit(".", 1)[0] if path else ""
        meta = _meta(song.get("Title") or fallback_title,
                     song.get("Artist") or (parts[0] if len(parts) >= 2 else ""),
                     song.get("Album") or (parts[1] if len(parts) >= 3 else ""),
                     float(song.get("duration") or song.get("Time") or 0),
                     f"song{song.get('Id', 'none')}")
        self.song = {"status": state, "meta": meta,
                     "pos": (float(status.get("elapsed") or 0), state == "Playing")}
        if not self.talking():
            self.show()
        return False

    def on_message(self, text: str) -> bool:
        word, _, rest = text.partition(" ")
        if word in ("say", "busy"):
            if word == "say" and rest.strip():
                self.saying = rest.strip()[:120]
                self.talk_n += 1
            elif not self.talking():
                self.saying = ""
                self.talk_n += 1
            self.talk_until = time.monotonic() + 60       # until "quiet" (or a minute)
        elif word == "quiet":
            self.talk_until = time.monotonic() + QUIET_HOLD_S
            if self.hold_timer:
                GLib.source_remove(self.hold_timer)
            self.hold_timer = GLib.timeout_add(int(QUIET_HOLD_S * 1000) + 100, self._end_talk)
        self.show()
        return False

    def _end_talk(self) -> bool:
        self.hold_timer = None
        if not self.talking():
            self.saying = ""
            self.show()
        return False


def watch_mpd(now: NowPlaying) -> None:
    """Blocking loop in a thread: MPD idle -> main loop updates."""
    while True:
        conn = None
        try:
            conn = MPDConn()
            conn.cmd(f"subscribe {CHANNEL}")
            while True:
                status, song = dict(conn.cmd("status")), dict(conn.cmd("currentsong"))
                GLib.idle_add(now.on_song, status, song)
                for k, v in conn.cmd("readmessages"):
                    if k == "message":
                        GLib.idle_add(now.on_message, v)
                conn.cmd("idle player mixer message", timeout=None)
        except Exception as e:
            log.warning("MPD: %s; retrying", e)
            time.sleep(5)
        finally:
            if conn:
                conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    player = Player(bus)
    now = NowPlaying(player)

    def register(*_):
        try:
            media = dbus.Interface(bus.get_object("org.bluez", ADAPTER), "org.bluez.Media1")
            media.RegisterPlayer(dbus.ObjectPath(PATH), dbus.Dictionary(player.props, signature="sv"))
            log.info("registered with BlueZ")
        except dbus.DBusException as e:
            log.warning("RegisterPlayer: %s (retrying)", e)
            GLib.timeout_add_seconds(10, lambda: register() and False)
        return False

    # bluetoothd restarted -> register again
    bus.add_signal_receiver(lambda name, old, new: new and GLib.timeout_add_seconds(2, register),
                            "NameOwnerChanged", "org.freedesktop.DBus", arg0="org.bluez")
    register()
    threading.Thread(target=watch_mpd, args=(now,), daemon=True).start()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
