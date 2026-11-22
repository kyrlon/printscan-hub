#!/bin/bash
echo "[scanner] Installing scanner drivers from $DRIVERS_DIR..."
hub-driver sync || echo "[scanner] WARNING: some drivers failed, see $DRIVERS_DIR/install.log"

# sane-airscan (driverless eSCL/WSD scanning) refuses to load at all without
# an Avahi daemon, even for scanners configured by IP in AIRSCAN_DEVICES.
# Use the host's avahi when reachable, otherwise run one in the container.
bus_alive()   { dbus-send --system --print-reply --dest=org.freedesktop.DBus / org.freedesktop.DBus.GetId >/dev/null 2>&1; }
avahi_alive() { dbus-send --system --print-reply --dest=org.freedesktop.Avahi / org.freedesktop.Avahi.Server.GetVersionString >/dev/null 2>&1; }

if avahi_alive; then
  echo "[scanner] Using the host's avahi-daemon"
elif bus_alive; then
  echo "[scanner] WARNING: host D-Bus is mounted but the host has no avahi-daemon."
  echo "[scanner]          Network scanners won't work until you run 'sudo apt install avahi-daemon' on the host."
else
  echo "[scanner] Starting avahi-daemon inside the container"
  mkdir -p /run/dbus
  rm -f /run/dbus/pid /run/dbus/system_bus_socket /run/avahi-daemon/pid
  dbus-daemon --system --fork
  avahi-daemon --daemonize --no-chroot || echo "[scanner] WARNING: avahi failed to start"
fi

# Build the device list once at startup. scanservjs only searches for scanners
# when the Scan page asks, and that search is what writes scanners-detected.json
# for the hub (Home / Print / Admin). Without this, after every restart the hub
# shows only AIRSCAN_DEVICES until someone opens the Scan page. Slow hosts (a
# Raspberry Pi 2 takes ~30 s) are why this waits generously.
(
  port="${SCAN_PORT:-8080}"
  for _ in $(seq 1 60); do
    sleep 5
    node -e "fetch('http://127.0.0.1:$port/api/v1/context', {signal: AbortSignal.timeout(300000)})
               .then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))" && {
      echo "[scanner] Scanner list ready (shared with the hub)"; exit 0; }
  done
  echo "[scanner] WARNING: couldn't build the scanner list at startup; it's built when the Scan page opens"
) &

exec /entrypoint.sh "$@"
