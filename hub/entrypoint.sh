#!/bin/bash
set -uo pipefail

ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
PORTAL_PORT="${PORTAL_PORT:-80}"
AVAHI="${AVAHI:-auto}"   # auto | internal | host | off

say() { echo "[hub] $*"; }

# --- 1. Seed persistent CUPS config (never overwrites your edits) ----------
cp -an /opt/cups-defaults/. /etc/cups/
mkdir -p /etc/cups/ppd /run/cups /var/spool/cups /var/cache/cups /var/log/cups

# --- 2. Admin account (used by CUPS web admin AND the portal admin page) ----
if [ -z "$ADMIN_PASSWORD" ]; then
  say "ERROR: ADMIN_PASSWORD is not set. Copy .env.example to .env and set it."
  exit 1
fi
id "$ADMIN_USER" >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin -G lpadmin "$ADMIN_USER"
usermod -aG lpadmin "$ADMIN_USER"
echo "$ADMIN_USER:$ADMIN_PASSWORD" | chpasswd

# --- 2b. Warn when USB printers can't work (Docker Desktop / WSL2 VM) ---------
if [ -z "$(ls -A /sys/bus/usb/devices 2>/dev/null)" ]; then
  case "$(uname -r)" in
    *linuxkit*|*microsoft*|*WSL*)
      say "WARNING: running inside Docker Desktop / WSL2 ($(uname -r))."
      say "         USB printers will NOT work; network printers are fine." ;;
    *)
      say "WARNING: no USB bus visible; USB printers won't be detected (need privileged + /dev/bus/usb)." ;;
  esac
fi

# --- 3. Install remembered drivers ------------------------------------------
say "Installing drivers from $DRIVERS_DIR (first start after recreate may take a while)..."
hub-driver sync || say "WARNING: some drivers failed to install, see $DRIVERS_DIR/install.log"

# --- 4. mDNS / Bonjour so clients can auto-discover printers ----------------
HOST_DBUS=/var/run/dbus/system_bus_socket
if [ "$AVAHI" = auto ]; then
  if [ -S "$HOST_DBUS" ] && dbus-send --system --print-reply --dest=org.freedesktop.Avahi / \
        org.freedesktop.Avahi.Server.GetVersionString >/dev/null 2>&1; then
    AVAHI=host
  else
    AVAHI=internal
  fi
fi
case "$AVAHI" in
  host)     say "Using the host's avahi-daemon for printer discovery" ;;
  internal)
    if [ -S "$HOST_DBUS" ] && dbus-send --system --print-reply --dest=org.freedesktop.DBus / \
          org.freedesktop.DBus.GetId >/dev/null 2>&1; then
      # The host's D-Bus is mounted but has no avahi; don't fight over its socket.
      say "WARNING: host D-Bus found but no avahi-daemon on the host. For printer auto-discovery run"
      say "         'sudo apt install avahi-daemon' on the host, or remove the /var/run/dbus mount."
    else
      say "Starting avahi-daemon inside the container"
      rm -f /run/dbus/pid /run/dbus/system_bus_socket /run/avahi-daemon/pid
      mkdir -p /run/dbus
      dbus-daemon --system --fork
      avahi-daemon --daemonize --no-chroot || say "WARNING: avahi failed to start (printers won't be auto-discovered)"
    fi
    ;;
  *)        say "Printer discovery (mDNS) disabled" ;;
esac

# --- 5. Start CUPS with a watchdog (driver installers sometimes restart it) --
rm -f /run/cups/cupsd.pid
cups_watchdog() {
  while true; do
    if ! pgrep -x cupsd >/dev/null; then
      say "starting cupsd"
      /usr/sbin/cupsd -f &
    fi
    sleep 5
  done
}
cups_watchdog &
for _ in $(seq 1 30); do lpstat -r 2>/dev/null | grep -q "is running" && break; sleep 1; done
say "CUPS is up on port 631"

# --- 6. Web portal (foreground) ---------------------------------------------
say "Portal listening on port $PORTAL_PORT"
cd /opt/portal
exec gunicorn --bind "0.0.0.0:$PORTAL_PORT" --workers 2 --threads 4 \
     --timeout 900 --access-logfile - app:app
