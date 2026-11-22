#!/usr/bin/env bash
# Prepare a Linux host (Debian, Ubuntu, Raspberry Pi OS) for PrintScan Hub.
#
#   scripts/linux-host.sh status           check services, ports and printers (read-only)
#   scripts/linux-host.sh prepare [opts]   stop the host's own CUPS so the hub can use port 631,
#                                          then install what's missing. With no options it asks
#                                          about each one; options skip the questions:
#        --all  (or -y)                    install everything missing: avahi-daemon, ipp-usb, Docker
#        --avahi  --ipp-usb  --docker      install only the ones named (combine freely)
#        --none                            install nothing, just stop the host's CUPS
#   scripts/linux-host.sh restore          turn the host's CUPS back on (stop the hub first)
#   scripts/linux-host.sh install-docker   install Docker Engine + Compose from Docker's repository
#
# Commands that change the system re-run themselves with sudo.
set -euo pipefail

HOST_CUPS_UNITS=(cups-browsed.service cups.service cups.socket cups.path)

# Ports from the hub's .env (next to this script's folder), with the defaults.
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
# (No .env yet is normal: the README runs prepare before copying it. Don't let set -e exit then.)
env_val() { [ -f "$ENV_FILE" ] || return 0; sed -n "s/^$1=\([0-9]*\).*/\1/p" "$ENV_FILE" | tail -1; }
PORTAL_PORT=$(env_val PORTAL_PORT); PORTAL_PORT=${PORTAL_PORT:-80}
CUPS_PORT=$(env_val CUPS_PORT);     CUPS_PORT=${CUPS_PORT:-631}
SCAN_PORT=$(env_val SCAN_PORT);     SCAN_PORT=${SCAN_PORT:-8080}
PORTS="$PORTAL_PORT $CUPS_PORT 21 445 $SCAN_PORT"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[ok]\033[0m %s\n' "$*"; }

as_root() { if [ "$(id -u)" -ne 0 ]; then exec sudo -E bash "$0" "$@"; fi; }

usage() { sed -n '2,20p' "$0" | sed -n '/^#/!q;p' | sed 's/^# \{0,1\}//'; }

os_family() {
  . /etc/os-release
  case " $ID ${ID_LIKE:-} " in
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

# Ask a yes/no question on the terminal. $2 is the default (y or n).
ask() {
  local reply hint="[Y/n]"; [ "$2" = n ] && hint="[y/N]"
  read -r -p "    $1 $hint " reply </dev/tty || reply=""
  reply=${reply:-$2}
  [[ "$reply" =~ ^[Yy] ]]
}

has_usb_printer() {
  lsusb -v 2>/dev/null | grep -q 'bInterfaceClass *7 Printer' || ls /dev/usb/lp* >/dev/null 2>&1
}

prepare() {
  as_root prepare "$@"
  [ "$(os_family)" = debian ] || { warn "prepare only knows Debian-family systems; stop your CUPS service by hand."; exit 1; }

  # What to install: ask (default), everything (--all), or exactly the named ones.
  local mode=ask want_avahi=0 want_ippusb=0 want_docker=0 a
  for a in "$@"; do
    case "$a" in
      --all|-y|--yes)  mode=all ;;
      --avahi)         mode=pick; want_avahi=1 ;;
      --ipp-usb)       mode=pick; want_ippusb=1 ;;
      --docker)        mode=pick; want_docker=1 ;;
      --none)          mode=pick ;;
      *) warn "Unknown option: $a"; usage; exit 2 ;;
    esac
  done
  if [ "$mode" = ask ] && ! { : </dev/tty; } 2>/dev/null; then
    mode=all; say "No terminal to ask on: installing everything that's missing (same as --all)."
  fi

  local have_avahi=0 have_ippusb=0 have_docker=0
  dpkg -s avahi-daemon >/dev/null 2>&1 && have_avahi=1
  dpkg -s ipp-usb      >/dev/null 2>&1 && have_ippusb=1
  command -v docker    >/dev/null      && have_docker=1

  if [ "$mode" = all ]; then
    want_avahi=1; want_ippusb=1; want_docker=1
  elif [ "$mode" = ask ]; then
    if [ $have_avahi = 1 ] && [ $have_ippusb = 1 ] && [ $have_docker = 1 ]; then
      ok "avahi-daemon, ipp-usb and Docker are already installed."
    else
      say "Choose what to install (Enter accepts the default)"
    fi
    if [ $have_avahi = 0 ]; then
      echo "    avahi-daemon: lets the hub find network printers and scanners automatically,"
      echo "    and lets phones and laptops find the hub's printers (AirPrint / Mopria)."
      ask "Install avahi-daemon?" y && want_avahi=1
    fi
    if [ $have_ippusb = 0 ]; then
      local def=n; has_usb_printer && def=y
      echo "    ipp-usb: only needed for printers plugged into this machine by USB. Shares them"
      echo "    driverlessly (print and scan) at http://localhost:60000.$([ $def = y ] && echo ' A USB printer is connected now.')"
      ask "Install ipp-usb?" "$def" && want_ippusb=1
    fi
    if [ $have_docker = 0 ]; then
      echo "    Docker: runs the hub itself. Installed from Docker's own repository."
      ask "Install Docker Engine + Compose?" y && want_docker=1
    fi
  fi

  say "Stopping and disabling the host's CUPS (the hub runs its own on port 631)"
  for u in "${HOST_CUPS_UNITS[@]}"; do
    systemctl disable --now "$u" >/dev/null 2>&1 || true
  done
  ok "cups: $(unit_state cups), cups-browsed: $(unit_state cups-browsed)"

  local pkgs=()
  [ $want_avahi = 1 ]  && [ $have_avahi = 0 ]  && pkgs+=(avahi-daemon)
  [ $want_ippusb = 1 ] && [ $have_ippusb = 0 ] && pkgs+=(ipp-usb)
  if [ ${#pkgs[@]} -gt 0 ]; then
    say "Installing ${pkgs[*]}"
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${pkgs[@]}"
  fi
  if dpkg -s avahi-daemon >/dev/null 2>&1; then
    systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
  fi
  ok "avahi-daemon: $(unit_state avahi-daemon), ipp-usb: $(unit_state ipp-usb) (ipp-usb starts when a supported USB printer is plugged in)"
  [ $want_docker = 1 ] && [ $have_docker = 0 ] && do_install_docker
  if [ ${#pkgs[@]} -gt 0 ] && command -v docker >/dev/null \
     && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx printscan-hub; then
    warn "The hub is already running: restart it so it picks up the change (docker compose restart hub)."
  fi
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
  do_install_docker
}

do_install_docker() {
  [ "$(os_family)" = debian ] || { warn "Only Debian-family systems are handled; see https://docs.docker.com/engine/install/"; exit 1; }
  . /etc/os-release
  repo=debian; [ "$ID" = ubuntu ] || [[ " ${ID_LIKE:-} " == *" ubuntu "* ]] && repo=ubuntu
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

cmd="${1:-status}"; shift || true
case "$cmd" in
  status)          status ;;
  prepare)         prepare "$@" ;;
  restore)         restore ;;
  install-docker)  install_docker ;;
  -h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac
