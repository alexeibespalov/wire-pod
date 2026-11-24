#!/usr/bin/env bash
# Music for Cortana: MPD on the Pi Zero plays the NAS music share on the Bluetooth speaker.
#
#   ./deploy/pizero/music.sh          # after setup.sh <SPEAKER_MAC> (it makes cortana_speaker)
#
# MPD reads the files straight off the share (mounted read-only) and plays them as they are:
# nothing is copied or converted ahead of time. cortana-agent on the Pi 5 drives it on :6600
# (app/music.py); the satellite's speaker command (cortana-play) pauses it for chimes and
# replies, and the agent resumes it after. The SMB login is copied from the Pi 5
# (/etc/samba/credentials_omv, the same one Home Assistant's /media mount uses).
# Idempotent: safe to re-run.
set -euo pipefail

ZERO="${ZERO_HOST:-john@192.168.1.96}"
PI5="${PI5_HOST:-john@192.168.1.39}"
KEY="${ZERO_KEY:-$HOME/.ssh/rpi_key}"
SHARE="${MUSIC_SHARE:-//192.168.1.101/Shared_Media/Music}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

scp -q -i "$KEY" "$HERE/mpd.conf" "$HERE/cortana-nowplaying.py" "$ZERO:/tmp/"
ssh -i "$KEY" "$PI5" 'sudo -n cat /etc/samba/credentials_omv' \
    | ssh -i "$KEY" "$ZERO" 'sudo -n install -d -m 755 /etc/samba &&
        sudo -n tee /etc/samba/credentials_omv >/dev/null && sudo -n chmod 600 /etc/samba/credentials_omv'

ssh -i "$KEY" "$ZERO" SHARE="$SHARE" bash -s <<'REMOTE'
set -euo pipefail
PKGS="mpd mpc cifs-utils python3-dbus python3-gi"
dpkg -s $PKGS >/dev/null 2>&1 ||
    sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $PKGS

# the share, read-only, mounted on first use
sudo -n mkdir -p /mnt/music
LINE="$SHARE /mnt/music cifs credentials=/etc/samba/credentials_omv,ro,uid=mpd,gid=audio,iocharset=utf8,_netdev,nofail,x-systemd.automount 0 0"
grep -q " /mnt/music " /etc/fstab || echo "$LINE" | sudo -n tee -a /etc/fstab >/dev/null
sudo -n systemctl daemon-reload
sudo -n systemctl start mnt-music.automount
ls /mnt/music >/dev/null

sudo -n systemctl enable mpd.service >/dev/null 2>&1
if ! cmp -s /tmp/mpd.conf /etc/mpd.conf; then      # (a restart would abort a running scan)
    sudo -n install -m 644 /tmp/mpd.conf /etc/mpd.conf
    sudo -n systemctl restart mpd.service
    sleep 2
fi
systemctl is-active mpd
mpc -q update      # scans in the background; a first scan of ~7,800 songs takes a while

# what's playing (or "Cortana") on the speaker's screen, and its buttons -> MPD
sudo -n install -m 755 /tmp/cortana-nowplaying.py /usr/local/bin/cortana-nowplaying
sudo -n tee /etc/systemd/system/cortana-nowplaying.service >/dev/null <<UNIT
[Unit]
Description=Cortana: what's playing, on the Bluetooth speaker's screen (AVRCP)
After=bluetooth.service mpd.service
Wants=bluetooth.service
[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/cortana-nowplaying
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
sudo -n systemctl daemon-reload
sudo -n systemctl enable cortana-nowplaying.service >/dev/null 2>&1
sudo -n systemctl restart cortana-nowplaying.service
sleep 3
systemctl is-active cortana-nowplaying
mpc stats | head -3
REMOTE
