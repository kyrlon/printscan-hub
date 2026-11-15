#!/bin/bash
set -euo pipefail

USER_NAME="${SCAN_INBOX_USER:-scanner}"
PASSWORD="${SCAN_INBOX_PASSWORD:-}"
PUID="${SCAN_INBOX_UID:-1000}"
FTP_PORT="${SCAN_INBOX_FTP_PORT:-21}"
PASV_MIN="${SCAN_INBOX_PASV_MIN:-21100}"
PASV_MAX="${SCAN_INBOX_PASV_MAX:-21110}"
PASV_ADDRESS="${HUB_ADDRESS:-}"
SMB="${SCAN_INBOX_SMB:-true}"
SMB_PORT="${SCAN_INBOX_SMB_PORT:-445}"
SMB1="${SCAN_INBOX_SMB1:-false}"   # only for very old devices

say() { echo "[inbox] $*"; }

if [ -z "$PASSWORD" ]; then
  say "ERROR: set SCAN_INBOX_PASSWORD in .env (the printer logs in with it)."
  exit 1
fi
if ! [[ "$USER_NAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
  say "ERROR: SCAN_INBOX_USER must be lowercase letters/numbers (got '$USER_NAME')."
  exit 1
fi

# --- account (home = the scans folder) ---------------------------------------
if ! id "$USER_NAME" >/dev/null 2>&1; then
  getent group "$PUID" >/dev/null || addgroup -g "$PUID" "$USER_NAME"
  adduser -D -H -h /scans -s /sbin/nologin -u "$PUID" -G "$(getent group "$PUID" | cut -d: -f1)" "$USER_NAME"
fi
echo "$USER_NAME:$PASSWORD" | chpasswd >/dev/null
mkdir -p /scans
chown "$PUID" /scans 2>/dev/null || true
chmod 0777 /scans 2>/dev/null || true

# --- FTP ---------------------------------------------------------------------
mkdir -p /var/run/vsftpd/empty
cat > /etc/vsftpd/vsftpd.conf <<EOF
listen=YES
listen_ipv6=NO
listen_port=$FTP_PORT
background=NO
anonymous_enable=NO
local_enable=YES
write_enable=YES
local_umask=002
file_open_mode=0666
chroot_local_user=YES
allow_writeable_chroot=YES
check_shell=NO
userlist_enable=YES
userlist_deny=NO
userlist_file=/etc/vsftpd/user_list
seccomp_sandbox=NO
secure_chroot_dir=/var/run/vsftpd/empty
pasv_enable=YES
pasv_min_port=$PASV_MIN
pasv_max_port=$PASV_MAX
port_enable=YES
connect_from_port_20=NO
xferlog_enable=YES
xferlog_std_format=NO
vsftpd_log_file=/proc/1/fd/1
idle_session_timeout=600
EOF
echo "$USER_NAME" > /etc/vsftpd/user_list
if [ -n "$PASV_ADDRESS" ]; then
  echo "pasv_address=$PASV_ADDRESS" >> /etc/vsftpd/vsftpd.conf
fi
say "FTP on port $FTP_PORT (passive ports $PASV_MIN-$PASV_MAX${PASV_ADDRESS:+, advertised as $PASV_ADDRESS}), user '$USER_NAME'"

# --- SMB (optional) ----------------------------------------------------------
if [ "$SMB" = true ]; then
  cat > /etc/samba/smb.conf <<EOF
[global]
  server role = standalone server
  workgroup = WORKGROUP
  server string = PrintScan Hub inbox
  smb ports = $SMB_PORT
  server min protocol = $([ "$SMB1" = true ] && echo NT1 || echo SMB2)
  ntlm auth = $([ "$SMB1" = true ] && echo yes || echo ntlmv2-only)
  map to guest = never
  load printers = no
  printing = bsd
  printcap name = /dev/null
  disable spoolss = yes
  log file = /dev/stdout
  log level = 1

[scans]
  path = /scans
  valid users = $USER_NAME
  read only = no
  force user = $USER_NAME
  create mask = 0664
  directory mask = 0775
EOF
  (echo "$PASSWORD"; echo "$PASSWORD") | smbpasswd -s -a "$USER_NAME" >/dev/null
  smbd --foreground --no-process-group --debug-stdout &
  say "SMB share \\\\<hub>\\scans on port $SMB_PORT"
else
  say "SMB disabled"
fi

exec vsftpd /etc/vsftpd/vsftpd.conf
