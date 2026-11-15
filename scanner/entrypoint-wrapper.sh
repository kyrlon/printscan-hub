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

exec /entrypoint.sh "$@"
