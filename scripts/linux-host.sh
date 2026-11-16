#!/usr/bin/env bash
# Prepare a Linux host (Debian, Ubuntu, Raspberry Pi OS) for PrintScan Hub.
#
#   scripts/linux-host.sh status           check services, ports and printers (read-only)
#   scripts/linux-host.sh prepare          stop the host's own CUPS so the hub can use port 631,
#                                          and make sure avahi-daemon + ipp-usb are installed
#   scripts/linux-host.sh restore          turn the host's CUPS back on (stop the hub first)
#   scripts/linux-host.sh install-docker   install Docker Engine + Compose from Docker's repository
#
# Commands that change the system re-run themselves with sudo.
set -euo pipefail

HOST_CUPS_UNITS=(cups-browsed.service cups.service cups.socket cups.path)

# Ports from the hub's .env (next to this script's folder), with the defaults.
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
env_val() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=\([0-9]*\).*/\1/p" "$ENV_FILE" | tail -1; }
PORTAL_PORT=$(env_val PORTAL_PORT); PORTAL_PORT=${PORTAL_PORT:-80}
CUPS_PORT=$(env_val CUPS_PORT);     CUPS_PORT=${CUPS_PORT:-631}
SCAN_PORT=$(env_val SCAN_PORT);     SCAN_PORT=${SCAN_PORT:-8080}
PORTS="$PORTAL_PORT $CUPS_PORT 21 445 $SCAN_PORT"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[ok]\033[0m %s\n' "$*"; }

as_root() { if [ "$(id -u)" -ne 0 ]; then exec sudo -E bash "$0" "$@"; fi; }

os_family() {
  . /etc/os-release
  case " $ID $ID_LIKE " in
    *" debian "*|*" ubuntu "*|*" raspbian "*) echo debian ;;
    *) echo other ;;
  esac
}

unit_state() { systemctl is-active "$1" 2>/dev/null || true; }

status() {
  . /etc/os-release
  say "Host: $PRETTY_NAME ($(uname -m)), IP: $(hostname -I 2>/dev/null | awk '{print $1}')"
  [ "$(os_family)" = debian ] || warn "Not a Debian-family system: package steps here won't apply, but the checks still do."

  say "Services"
  for u in cups cups-browsed avahi-daemon ipp-usb docker; do
    printf '   %-14s %s\n' "$u" "$(unit_state "$u")"
  done
  if [ "$(unit_state cups)" = active ]; then
    if [ "$CUPS_PORT" = 631 ]; then
      warn "The host's CUPS is running: it holds port 631, so the hub's CUPS can't start. Run: $0 prepare (or set CUPS_PORT in .env)"
    else
      ok "The host's CUPS is running on 631; the hub uses CUPS_PORT=$CUPS_PORT, so both can run."
    fi
  fi
  [ "$(unit_state avahi-daemon)" = active ] || warn "avahi-daemon isn't running: printers won't be auto-discovered."

  say "Ports the hub uses ($PORTAL_PORT portal, $CUPS_PORT printing, 21 scan inbox, 445 SMB inbox, $SCAN_PORT internal scan app)"
  for p in $PORTS; do
    holder=$(ss -ltnpH "sport = :$p" 2>/dev/null | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | sort -u | tr '\n' ' ')
    if ss -ltnH "sport = :$p" 2>/dev/null | grep -q .; then
      printf '   %-5s in use %s\n' "$p" "${holder:+by $holder}"
    else
      printf '   %-5s free\n' "$p"
    fi
  done

  say "USB printers"
  if command -v lsusb >/dev/null; then
    lsusb -v 2>/dev/null | awk '/^Bus/{dev=$0} /bInterfaceClass +7 Printer/{print "   " dev}' | sort -u
  fi
  ls /dev/usb/lp* >/dev/null 2>&1 && echo "   raw device nodes: $(ls /dev/usb/lp* | tr '\n' ' ')"
  if curl -s -m 3 -o /dev/null http://localhost:60000/ 2>/dev/null; then
    model=$(curl -s -m 5 http://localhost:60000/eSCL/ScannerCapabilities 2>/dev/null | sed -n 's/.*<pwg:MakeAndModel>\([^<]*\).*/\1/p' | head -1)
    ok "ipp-usb is sharing a USB printer at http://localhost:60000${model:+ ($model, scanner too)}"
    echo "   In the hub: Admin -> Add a printer -> Device URI ipp://localhost:60000/ipp/print, driver: everywhere"
  fi

  say "Docker"
  if command -v docker >/dev/null; then
    echo "   $(docker --version 2>/dev/null) | $(docker compose version 2>/dev/null || echo 'compose plugin missing')"
  else
    warn "Docker isn't installed. Run: $0 install-docker"
  fi
}

prepare() {
  as_root prepare
  [ "$(os_family)" = debian ] || { warn "prepare only knows Debian-family systems; stop your CUPS service by hand."; exit 1; }

  say "Stopping and disabling the host's CUPS (the hub runs its own on port 631)"
  for u in "${HOST_CUPS_UNITS[@]}"; do
    systemctl disable --now "$u" >/dev/null 2>&1 || true
  done
  ok "cups: $(unit_state cups), cups-browsed: $(unit_state cups-browsed)"

  missing=()
  dpkg -s avahi-daemon >/dev/null 2>&1 || missing+=(avahi-daemon)
  dpkg -s ipp-usb >/dev/null 2>&1 || missing+=(ipp-usb)
  if [ ${#missing[@]} -gt 0 ]; then
    say "Installing ${missing[*]} (printer discovery; driverless USB printers/scanners)"
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
  fi
  systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
  ok "avahi-daemon: $(unit_state avahi-daemon), ipp-usb: $(unit_state ipp-usb) (ipp-usb starts when a supported USB printer is plugged in)"
  echo
  status
}

restore() {
  as_root restore
  if command -v docker >/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx printscan-hub; then
    if [ "$CUPS_PORT" = 631 ]; then
      warn "printscan-hub is running and uses port 631. Stop it first (docker compose down), or set CUPS_PORT in .env."
      exit 1
    fi
  fi
  say "Re-enabling the host's CUPS"
  for u in cups.socket cups.path cups.service cups-browsed.service; do
    systemctl enable --now "$u" >/dev/null 2>&1 || true
  done
  ok "cups: $(unit_state cups), cups-browsed: $(unit_state cups-browsed)"
}

install_docker() {
  as_root install-docker
  [ "$(os_family)" = debian ] || { warn "Only Debian-family systems are handled; see https://docs.docker.com/engine/install/"; exit 1; }
  . /etc/os-release
  repo=debian; [ "$ID" = ubuntu ] || [[ " $ID_LIKE " == *" ubuntu "* ]] && repo=ubuntu
  codename=${UBUNTU_CODENAME:-$VERSION_CODENAME}
  say "Installing Docker Engine + Compose from download.docker.com ($repo $codename)"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$repo/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$repo $codename stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    usermod -aG docker "$SUDO_USER"
    ok "Added $SUDO_USER to the docker group (log out and back in to use docker without sudo)."
  fi
  ok "$(docker --version) | $(docker compose version)"
}

case "${1:-status}" in
  status)          status ;;
  prepare)         prepare ;;
  restore)         restore ;;
  install-docker)  install_docker ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
