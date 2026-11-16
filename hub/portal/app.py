"""PrintScan Hub portal.

Public:  upload a file from any browser and print it (no drivers on the client).
Admin:   add/remove printers, discover devices, install drivers.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
from functools import wraps
from pathlib import Path

import cups
import requests
from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.utils import secure_filename

import scanproxy
import store

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
HUB_NAME = os.environ.get("HUB_NAME", "PrintScan Hub")
def _env_mb(name, default):
    """Read a size in MB from .env; a typo falls back to the default instead of crashing."""
    raw = os.environ.get(name, "").strip().lower().removesuffix("mb").strip()
    try:
        value = float(raw) if raw else default
        if value <= 0:
            raise ValueError
    except ValueError:
        print(f"[portal] WARNING: {name}={os.environ.get(name)!r} is not a positive number, using {default}", flush=True)
        value = default
    return int(value) if value == int(value) else value


MAX_UPLOAD_MB = _env_mb("MAX_UPLOAD_MB", 200)                 # files sent to print
MAX_DRIVER_UPLOAD_MB = _env_mb("MAX_DRIVER_UPLOAD_MB", 500)   # driver packages (admin)
DRIVERS_DIR = Path(os.environ.get("DRIVERS_DIR", "/drivers"))

PRINTABLE = {".pdf", ".ps", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp"}
OFFICE = {".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp"}
HAS_OFFICE = shutil.which("soffice") is not None
ACCEPTED = PRINTABLE | (OFFICE if HAS_OFFICE else set())

JOB_STATES = {3: "pending", 4: "held", 5: "printing", 6: "stopped",
              7: "canceled", 8: "aborted", 9: "completed"}
PRINTER_STATES = {3: "idle", 4: "printing", 5: "stopped"}

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,127}$")
PAGES_RE = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")

def detect_environment():
    """Work out whether USB printers can possibly work in this container.

    Docker Desktop (Windows/macOS) and Docker inside WSL2 run containers in a
    Linux VM. The VM's kernel name gives it away, and the host's USB ports are
    not connected to that VM unless the user attaches them with USB/IP.
    """
    try:
        kernel = Path("/proc/sys/kernel/osrelease").read_text().strip()
    except OSError:
        kernel = ""
    k = kernel.lower()
    if "linuxkit" in k:
        vm = "Docker Desktop"
    elif "microsoft" in k or "wsl" in k:
        vm = "Docker Desktop / WSL2"
    else:
        vm = None
    usb_dir = Path("/sys/bus/usb/devices")
    usb_bus = usb_dir.is_dir() and any(usb_dir.iterdir())
    return {"vm": vm, "kernel": kernel, "usb_bus": usb_bus}


ENVIRONMENT = detect_environment()

HUB_ADDRESS = os.environ.get("HUB_ADDRESS", "").strip()
INBOX = {
    "enabled": bool(os.environ.get("SCAN_INBOX_PASSWORD")),
    "user": os.environ.get("SCAN_INBOX_USER", "scanner"),
    "password": os.environ.get("SCAN_INBOX_PASSWORD", ""),
    "ftp_port": os.environ.get("SCAN_INBOX_FTP_PORT", "21"),
    # Windows owns port 445, so the desktop override turns SMB off.
    "smb": os.environ.get("SCAN_INBOX_SMB", "true").lower() == "true" and not ENVIRONMENT["vm"],
    "smb_port": os.environ.get("SCAN_INBOX_SMB_PORT", "445"),
}

ALLOW_SIGNUP = os.environ.get("ALLOW_SIGNUP", "false").lower() == "true"
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")

app = Flask(__name__)
# Hard ceiling for any request; per-page limits are checked in check_limits().
app.config["MAX_CONTENT_LENGTH"] = int(max(MAX_UPLOAD_MB, MAX_DRIVER_UPLOAD_MB) * 1024 * 1024) + 1024 * 1024
# Stable across gunicorn workers so flash messages survive redirects.
app.secret_key = hashlib.sha256(b"printscan-hub:" + ADMIN_PASSWORD.encode()).digest()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  PERMANENT_SESSION_LIFETIME=30 * 86400)

store.init()
app.register_blueprint(scanproxy.bp)

_cups_user_lock = threading.Lock()
_ppd_cache = {"at": 0.0, "data": {}}


# ---------------------------------------------------------------- helpers ---
def conn():
    return cups.Connection()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


def upload_limit_mb():
    return MAX_DRIVER_UPLOAD_MB if request.endpoint == "admin_driver_upload" else MAX_UPLOAD_MB


@app.before_request
def check_limits():
    # Runs before the body is read, so an oversized upload is refused immediately.
    if request.method == "POST" and request.content_length:
        if request.content_length > upload_limit_mb() * 1024 * 1024 + 64 * 1024:  # + form fields
            abort(413)


@app.before_request
def check_csrf():
    # The proxied scan app sends JSON API calls; it's guarded in scanproxy instead.
    if request.method == "POST" and not request.path.startswith("/scan/app/"):
        sent = request.form.get("csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Form expired, go back and reload the page.")


@app.context_processor
def inject_globals():
    host = request.host.rsplit(":", 1)[0] if not request.host.startswith("[") else request.host.split("]")[0] + "]"
    return {
        "hub_name": HUB_NAME,
        "scan_url": url_for("scanproxy.scan_app"),
        "signed_in_user": session.get("user"),
        "accounts_on": ALLOW_SIGNUP or store.user_count() > 0,
        "cups_url": f"http://{host}:631",
        "ipp_base": f"http://{host}:631/printers/",
        "env": ENVIRONMENT,
        "inbox": INBOX,
        "max_mb": MAX_UPLOAD_MB,
        "max_driver_mb": MAX_DRIVER_UPLOAD_MB,
        # Address printers should use to reach the hub ("localhost" is useless to them).
        "hub_address": HUB_ADDRESS or (host if host not in ("localhost", "127.0.0.1", "[::1]") else ""),
    }


def require_admin(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        ok = (auth is not None and ADMIN_PASSWORD
              and hmac.compare_digest(auth.username or "", ADMIN_USER)
              and hmac.compare_digest(auth.password or "", ADMIN_PASSWORD))
        if not ok:
            return Response("Admin login required", 401,
                            {"WWW-Authenticate": 'Basic realm="PrintScan Hub admin"'})
        return view(*args, **kwargs)
    return wrapper


STATUS_ATTRS = ["marker-names", "marker-levels", "marker-colors", "marker-types",
                "marker-low-levels", "printer-state-reasons", "printer-more-info",
                "printer-make-and-model"]
MARKER_LABELS = {"bk": "Black", "k": "Black", "black": "Black", "c": "Cyan", "cyan": "Cyan",
                 "m": "Magenta", "magenta": "Magenta", "y": "Yellow", "yellow": "Yellow"}
REASON_TEXT = {
    "media-empty": "Out of paper", "media-needed": "Load paper", "media-low": "Paper low",
    "media-jam": "Paper jam", "cover-open": "Cover open", "door-open": "Door open",
    "toner-low": "Toner low", "toner-empty": "Out of toner", "marker-supply-low": "Toner/ink low",
    "marker-supply-empty": "Toner/ink empty", "marker-waste-almost-full": "Waste toner box almost full",
    "marker-waste-full": "Waste toner box full", "developer-low": "Developer low",
    "opc-near-eol": "Drum nearing end of life", "opc-life-over": "Replace drum",
    "input-tray-missing": "Paper tray missing", "output-area-full": "Output tray full",
    "output-area-almost-full": "Output tray almost full", "offline": "Printer offline",
    "connecting-to-device": "Connecting to printer", "fuser-over-temp": "Fuser too hot",
    "interlock-open": "Cover open", "spool-area-full": "Printer memory full",
}
_status_cache = {}


def device_host_port(uri):
    """Host and port of a network device URI, or None for USB/local devices."""
    m = re.match(r"^(ipps?|https?|socket|lpd)://(\[[^\]]+\]|[^/:?]+)(?::(\d+))?", uri)
    if not m:
        return None, None
    default = {"ipp": 631, "ipps": 631, "http": 80, "https": 443, "socket": 9100, "lpd": 515}[m.group(1)]
    return m.group(2), int(m.group(3) or default)


def parse_supplies(a):
    names = a.get("marker-names") or []
    if isinstance(names, str):
        names = [names]
    levels = a.get("marker-levels") or []
    colors = a.get("marker-colors") or []
    lows = a.get("marker-low-levels") or []
    types = a.get("marker-types") or []
    out = []
    for i, n in enumerate(names):
        lvl = levels[i] if i < len(levels) else -1
        color = (colors[i] if i < len(colors) else "") or "#888888"
        color = "#" + color.split("#")[1] if "#" in color else "#888888"  # "#00FFFF#FF00FF" -> first
        low = lows[i] if i < len(lows) else 10
        kind = types[i] if i < len(types) else ""
        out.append({
            "label": MARKER_LABELS.get(n.lower(), n[:1].upper() + n[1:]),
            "level": lvl if lvl >= 0 else None,       # -1/-2 unknown, -3 "some left"
            "some": lvl == -3,
            "low": 0 <= lvl <= low,
            "color": color,
            "black": color.lower() in ("#000000", "#000"),
            "kind": kind.replace("-", " ") if kind not in ("toner", "ink", "ink-cartridge") else "",
        })
    return out


def parse_alerts(reasons):
    if isinstance(reasons, str):
        reasons = [reasons]
    alerts = []
    for r in reasons or []:
        if r in ("none", "paused") or r.startswith("cups-") or r.startswith("com."):
            continue
        sev = "info"
        for suffix, s in (("-error", "error"), ("-warning", "warn"), ("-report", "info")):
            if r.endswith(suffix):
                r, sev = r[: -len(suffix)], s
                break
        else:
            sev = "error" if r in ("media-empty", "media-jam", "cover-open", "door-open",
                                   "toner-empty", "offline", "interlock-open") else "warn"
        if r == "other":
            continue        # vague "other-error" says nothing useful next to a specific alert
        text = REASON_TEXT.get(r, r.replace("-", " ").capitalize())
        existing = next((a for a in alerts if a["text"] == text), None)
        if existing:        # same condition reported twice (e.g. two trays): show once, worst severity
            if ["info", "warn", "error"].index(sev) > ["info", "warn", "error"].index(existing["sev"]):
                existing["sev"] = sev
            continue
        alerts.append({"text": text, "sev": sev})
    return alerts


def input_trays(uri):
    """Per-tray state from printer-input-tray (pycups can't decode it; ipptool can).

    Printers report "media-empty" for the whole printer even when only one tray
    (often the photo tray) is empty; this tells us which one.
    """
    try:
        p = subprocess.run(["ipptool", "-T", "5", "-tv", uri, "get-printer-attributes.test"],
                           capture_output=True, text=True, timeout=12)
    except (subprocess.TimeoutExpired, OSError):
        return []
    m = re.search(r"printer-input-tray \(.*?\) = (.*)", p.stdout)
    if not m:
        return []
    trays = []
    for part in re.split(r",(?=type=)", m.group(1)):
        kv = dict(x.split("=", 1) for x in part.strip().rstrip(";").split(";") if "=" in x)
        if "type" not in kv:
            continue
        try:
            status = int(kv.get("status", "0"))
            feed, xfeed = int(kv.get("mediafeed", "-2")), int(kv.get("mediaxfeed", "-2"))
        except ValueError:
            continue
        if kv.get("type") == "other" and feed < 0:
            continue                      # manual-feed slot / unknown: not a real tray
        # Printer-MIB status bits: 8 = alert, 16 = critical alert; 4/5 = unavailable
        problem = bool(status & 24) or (status & 7) in (4, 5)
        name = kv.get("name", "Tray")
        if 0 < xfeed <= 130000 and 0 < feed <= 180000:   # 5x7 in or smaller
            label = "Photo tray"
        elif name == "InputTray1":
            label = "Main tray"
        else:
            label = re.sub(r"InputTray(\d+)", r"Tray \1", name)
        trays.append({"label": label, "problem": problem})
    return trays


def printer_status(c, name, uri):
    """Live toner levels/alerts from the device itself, falling back to CUPS's cache."""
    cached = _status_cache.get((name, uri))
    if cached and time.time() - cached[0] < 60:
        return cached[1]

    host, port = device_host_port(uri)
    attrs, live, reachable = None, False, None
    if host:
        reachable = _port_open(host.strip("[]"), port, timeout=1.0)
        if reachable and uri.startswith("ipp://"):
            try:
                attrs = cups.Connection(host=host.strip("[]"), port=port).getPrinterAttributes(
                    uri=uri, requested_attributes=STATUS_ATTRS)
                live = True
            except (RuntimeError, cups.IPPError):
                attrs = None
    if attrs is None:
        try:
            attrs = c.getPrinterAttributes(name, requested_attributes=STATUS_ATTRS)
        except cups.IPPError:
            attrs = {}

    alerts = parse_alerts(attrs.get("printer-state-reasons"))
    # Per-tray paper state, shown quietly next to the ink levels. Printers raise
    # "out of paper" when *any* tray is empty (often just the photo tray); only
    # keep the alert when the main tray, or every tray, is empty.
    trays = []
    if uri.startswith(("ipp://", "ipps://")) and reachable is not False:
        trays = input_trays(uri)
    paper = next((a for a in alerts if a["text"] in ("Out of paper", "Load paper")), None)
    if paper and trays:
        main = next((t for t in trays if t["label"] == "Main tray"),
                    next((t for t in trays if t["label"] != "Photo tray"), None))
        if main and not main["problem"]:
            alerts.remove(paper)
        elif main and any(not t["problem"] for t in trays):
            paper["text"] = f"{main['label']} empty"
    if reachable is False:
        alerts.insert(0, {"text": f"Not responding at {host}", "sev": "error"})

    # Printers reached via localhost (e.g. USB through ipp-usb) only serve their
    # web page to the hub itself, so a link would just point back at the hub.
    local = (host or "").strip("[]") in ("localhost", "127.0.0.1", "::1")
    web = None
    more = attrs.get("printer-more-info", "") if live else ""
    m = re.match(r"^(https?://[^/]+)", more or "")
    if m and not re.search(r"//(localhost|127\.0\.0\.1|\[::1\])", m.group(1)):
        web = m.group(1) + "/"
    elif host and not local:
        web = f"http://{host}/"

    # The real model, straight from the device. Queues made with the generic
    # "everywhere" driver may only say "Printer - IPP Everywhere".
    device_model = attrs.get("printer-make-and-model") if live else None
    status = {"supplies": parse_supplies(attrs), "alerts": alerts, "web": web, "live": live,
              "device_model": device_model, "trays": trays}
    _status_cache[(name, uri)] = (time.time(), status)
    return status


SHARED_DIR = Path(os.environ.get("HUB_SHARED_DIR", "/data/shared"))


def hidden_scanners():
    try:
        return set(json.loads((SHARED_DIR / "scanners-hidden.json").read_text()).get("hidden", []))
    except (OSError, ValueError):
        return set()


def save_hidden_scanners(keys):
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SHARED_DIR / "scanners-hidden.json.tmp"
    tmp.write_text(json.dumps({"hidden": sorted(keys)}, indent=1))
    tmp.replace(SHARED_DIR / "scanners-hidden.json")


_ident_cache = {}


def scanner_identity(url):
    """Serial number and UUID from an eSCL scanner's capabilities (cached 10 min)."""
    if not url:
        return {}
    hit = _ident_cache.get(url)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    ident = {}
    try:
        r = requests.get(url.rstrip("/") + "/ScannerCapabilities", timeout=3)
        if r.ok:
            for key, tag in (("serial", "SerialNumber"), ("uuid", "UUID")):
                m = re.search(rf"<(?:\w+:)?{tag}>\s*([^<]+?)\s*</", r.text)
                if m:
                    ident[key] = m.group(1)
    except requests.RequestException:
        pass
    _ident_cache[url] = (time.time(), ident)
    return ident


def _base_name(name):
    return re.sub(r"\s*\((?:usb|network|wi-?fi)\)\s*$", "", name or "", flags=re.I).strip().lower()


def detected_scanners():
    """What the scan app (SANE) found, as reported by the scanner container."""
    try:
        data = json.loads((SHARED_DIR / "scanners-detected.json").read_text())
    except (OSError, ValueError):
        return None, None
    hidden = hidden_scanners()
    out = []
    for d in data.get("devices", []):
        url = d.get("url") or ""
        host, _ = device_host_port(url) if url else (None, None)
        prefix = d.get("id", "").split(":")[0]
        if host and host.strip("[]") in ("localhost", "127.0.0.1", "::1"):
            conn_label = "USB (shared by ipp-usb)"
        elif host:
            conn_label = f"Network ({host.strip('[]')})"
        elif prefix == "airscan":
            conn_label = "Network"
        elif prefix == "net":
            conn_label = "Shared by another computer"
        else:
            conn_label = f"USB / local ({prefix} driver)"
        res = [r for r in d.get("resolutions", []) if isinstance(r, (int, float))]
        ident = scanner_identity(url)
        tag = re.search(r"\[([0-9A-Fa-f]{6})\]", d.get("name", ""))
        out.append({**d, "hidden": d.get("key") in hidden, "connection": conn_label,
                    "res_range": f"{min(res)}–{max(res)} dpi" if res else "",
                    "serial": ident.get("serial"), "uuid": ident.get("uuid"),
                    "mac_tag": tag.group(1).upper() if tag else None})

    # Same physical device reached two ways (USB + Wi-Fi): match on UUID or serial,
    # else on the name without the "(USB)" suffix.
    groups = {}
    for s in out:
        key = (("uuid", s["uuid"].lower()) if s["uuid"] else
               ("serial", s["serial"]) if s["serial"] else ("name", _base_name(s.get("name"))))
        groups.setdefault(key, []).append(s)
    for (how, _), members in groups.items():
        visible = sum(not m["hidden"] for m in members)
        for s in members:
            s["twins"] = [m["connection"] for m in members if m is not s]
            s["twin_by"] = how if len(members) > 1 else None
            # Warn only while the duplicate is still visible on the Scan page.
            s["twin_resolved"] = visible <= 1
    return out, data.get("updated", "")


def serial_lookup(scanners):
    """Map UUID / IP / network name -> serial, learned from the scanners' eSCL replies.

    Printer discovery rarely includes a serial for network devices, but an
    all-in-one's scanner reports it, along with the same UUID and name.
    """
    by = {"uuid": {}, "host": {}, "name": {}}
    for s in scanners or []:
        if not s.get("serial"):
            continue
        if s.get("uuid"):
            by["uuid"][s["uuid"].lower().removeprefix("urn:uuid:")] = s["serial"]
        host, _ = device_host_port(s["url"]) if s.get("url") else (None, None)
        if host and host.strip("[]") not in ("localhost", "127.0.0.1", "::1"):
            by["host"][host.strip("[]")] = s["serial"]
        by["name"][(s.get("name") or "").lower()] = s["serial"]
    return by


def device_serial(uri, device_id, by):
    """Best-effort serial number for a discovered printer (None if unknown)."""
    m = re.search(r"(?:^|;)\s*(?:SN|SERN):\s*([^;]+)", device_id or "")
    if m and m.group(1).strip():
        return m.group(1).strip()
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query)
    if q.get("serial"):
        return q["serial"][0]
    if q.get("uuid") and q["uuid"][0].lower() in by["uuid"]:
        return by["uuid"][q["uuid"][0].lower()]
    host = urllib.parse.unquote(urllib.parse.urlsplit(uri).hostname or "")
    if host in by["host"]:
        return by["host"][host]
    # "dnssd://Name._ipp._tcp.local/" or "ipps://Name._ipps._tcp.local/": the
    # service name usually matches the one the scanner announces.
    svc = re.sub(r"\._ipps?\._tcp\.local\.?$", "", host, flags=re.I).lower()
    if svc in by["name"]:
        return by["name"][svc]
    # Brother "BRW<MAC>"-style hostnames: resolve over mDNS and match the IP.
    if host and "." not in host:
        try:
            ip = socket.getaddrinfo(host + ".local", None, socket.AF_INET)[0][4][0]
            return by["host"].get(ip)
        except (socket.gaierror, OSError, IndexError):
            pass
    return None


def list_scanners():
    """Scanners configured in .env (AIRSCAN_DEVICES), with their device web pages."""
    raw = os.environ.get("AIRSCAN_DEVICES", "")
    out = []
    for entry in re.split(re.escape(os.environ.get("DELIMITER", ";")) + r"|\n", raw):
        name, _, rest = entry.partition("=")
        url = rest.split(",")[0].strip()
        if not name.strip() or not url:
            continue
        host, _ = device_host_port(url)
        out.append({"name": name.strip().strip('"').replace("_", " "),
                    "web": f"http://{host}/" if host else None})
    return out


DISABLED_REASON = "Disabled by admin"
CUPS_PRINTER_REJECTING = cups.CUPS_PRINTER_REJECTING   # 0x80000, a bit in printer-type


def is_rejecting(attrs):
    """True if the queue isn't accepting jobs. getPrinters() omits
    printer-is-accepting-jobs, but printer-type carries it as a bit."""
    return bool(attrs.get("printer-type", 0) & CUPS_PRINTER_REJECTING)


def list_printers(details=True, include_disabled=True):
    """CUPS queues. Public pages pass include_disabled=False to leave out
    printers an admin switched off (Disable on the Admin page)."""
    c = conn()
    printers = c.getPrinters()
    try:
        default = c.getDefault()
    except cups.IPPError:
        default = None
    out = []
    for name, a in sorted(printers.items()):
        disabled = is_rejecting(a)
        if disabled and not include_disabled:
            continue
        status = printer_status(c, name, a.get("device-uri", "")) if details and not disabled else {}
        out.append({
            **status,
            "name": name,
            "info": a.get("printer-info") or name,
            "location": a.get("printer-location", ""),
            "model": a.get("printer-make-and-model", ""),
            "uri": a.get("device-uri", ""),
            "state": PRINTER_STATES.get(a.get("printer-state"), "?"),
            "message": a.get("printer-state-message", ""),
            "accepting": not disabled,
            "disabled": disabled,
            "shared": a.get("printer-is-shared", True),
            "default": name == default,
        })
    return out


def queue_for_device(dev, printers):
    """Name of an existing queue that already uses this discovered device, or None.

    One printer can be discovered under several addresses (dnssd name, IP, USB),
    so compare the exact URI, then the host, then the model + connection type
    (USB via ipp-usb vs network count as different devices).
    """
    local = ("localhost", "127.0.0.1", "::1")
    uri = dev["uri"]
    host, _ = device_host_port(uri)
    host = (host or "").strip("[]")
    dev_usb = ("(usb)" in (dev.get("info") or "").lower() or "(usb)" in urllib.parse.unquote(uri).lower()
               or uri.startswith(("usb:", "hp:/usb")) or host in local)
    for p in printers:
        p_host = (device_host_port(p["uri"])[0] or "").strip("[]")
        if uri == p["uri"] or (host and host == p_host and host not in local):
            return p["name"]
    model = (dev.get("model") or dev.get("info") or "").lower().strip()
    for p in printers:
        p_model = (p.get("device_model") or p.get("model") or "").lower()
        p_host = (device_host_port(p["uri"])[0] or "").strip("[]")
        p_usb = p["uri"].startswith(("usb:", "hp:/usb")) or p_host in local
        if model and len(model) > 4 and p_model and (model in p_model or p_model.split(",")[0] in model) \
                and dev_usb == p_usb:
            return p["name"]
    return None


def list_jobs(limit=15):
    attrs = ["job-id", "job-name", "job-originating-user-name", "job-printer-uri",
             "job-state", "time-at-creation", "job-state-reasons"]
    c = conn()
    jobs = {}
    for which in ("not-completed", "completed"):
        try:
            jobs.update(c.getJobs(which_jobs=which, requested_attributes=attrs))
        except cups.IPPError:
            pass
    out = []
    for jid, a in jobs.items():
        out.append({
            "id": jid,
            "name": a.get("job-name", ""),
            "user": a.get("job-originating-user-name", ""),
            "printer": a.get("job-printer-uri", "").rsplit("/", 1)[-1],
            "state": JOB_STATES.get(a.get("job-state"), "?"),
            "active": a.get("job-state", 9) < 7,
            "when": time.strftime("%b %d %H:%M", time.localtime(a.get("time-at-creation", 0))),
        })
    out.sort(key=lambda j: j["id"], reverse=True)
    return out[:limit]


def convert_to_pdf(src: Path, workdir: Path) -> Path:
    profile = workdir / "lo-profile"
    cmd = ["soffice", f"-env:UserInstallation=file://{profile}", "--headless",
           "--norestore", "--convert-to", "pdf", "--outdir", str(workdir), str(src)]
    subprocess.run(cmd, check=True, timeout=180, capture_output=True,
                   env={**os.environ, "HOME": str(workdir)})
    pdf = workdir / (src.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError("conversion produced no PDF")
    return pdf


HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252})$")


def _port_open(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_host(host):
    """Check one IP/hostname for printer services and suggest URI + driver.

    Needed because device discovery (mDNS/SNMP broadcast) can't cross the VM
    boundary on Docker Desktop, or reach printers on another subnet.
    """
    found = []
    if _port_open(host, 631):
        for path in ("/ipp/print", "/ipp", "/ipp/printer", "/printers/ipp", ""):
            uri = f"ipp://{host}:631{path}"
            try:
                p = subprocess.run(["ipptool", "-T", "5", "-tv", uri, "get-printer-attributes.test"],
                                   capture_output=True, text=True, timeout=15)
            except subprocess.TimeoutExpired:
                continue
            if "successful-ok" not in p.stdout:
                continue
            mm = re.search(r"printer-make-and-model \(.*?\) = (.+)", p.stdout)
            fmts = re.search(r"document-format-supported \(.*?\) = (.+)", p.stdout)
            fmts = fmts.group(1) if fmts else ""
            driverless = any(f in fmts for f in ("image/pwg-raster", "image/urf", "application/pdf"))
            found.append({
                "uri": f"ipp://{host}{path}" if path else uri,
                "info": (mm.group(1).strip() if mm else "IPP printer"),
                "model": "IPP Everywhere: no driver needed" if driverless else "IPP (needs a driver)",
                "cls": "network", "suggest": "everywhere" if driverless else "",
            })
            break
    if _port_open(host, 9100):
        found.append({"uri": f"socket://{host}:9100", "info": "Raw / JetDirect (port 9100)",
                      "model": "pick a driver for your model", "cls": "network", "suggest": ""})
    if _port_open(host, 515):
        found.append({"uri": f"lpd://{host}/queue", "info": "LPD (port 515)",
                      "model": "replace 'queue' with the printer's queue name", "cls": "network", "suggest": ""})
    return found


def run_hub_driver(*args, timeout=900):
    p = subprocess.run(["hub-driver", *args], capture_output=True, text=True, timeout=timeout)
    _ppd_cache["at"] = 0  # driver list may have changed
    return p.returncode, (p.stdout + p.stderr).strip()


def get_ppds():
    if time.time() - _ppd_cache["at"] > 600:
        _ppd_cache["data"] = conn().getPPDs()
        _ppd_cache["at"] = time.time()
    return _ppd_cache["data"]


# ----------------------------------------------------------------- public ---
@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/favicon.ico")
def favicon():
    # Browsers ask for /favicon.ico on their own (e.g. for bookmarks); hand them the SVG.
    return redirect(url_for("static", filename="icon.svg"), code=301)


@app.get("/")
def index():
    """Home: big Print / Scan buttons plus a quick status overview."""
    try:
        printers, err = list_printers(include_disabled=False), None
    except (RuntimeError, cups.IPPError) as e:
        printers, err = [], f"Cannot reach CUPS: {e}"
    return render_template("home.html", printers=printers, error=err, scanners=list_scanners())


@app.get("/print")
def print_page():
    try:
        printers, jobs, err = list_printers(include_disabled=False), list_jobs(), None
    except (RuntimeError, cups.IPPError) as e:
        printers, jobs, err = [], [], f"Cannot reach CUPS: {e}"
    return render_template("print.html", printers=printers, jobs=jobs, error=err,
                           scanners=list_scanners(),
                           accepted=sorted(ACCEPTED), has_office=HAS_OFFICE)


@app.get("/scan")
def scan():
    return render_template("scan.html", files=scanproxy.status())


@app.post("/scan/mode")
def scan_mode():
    m = request.form.get("mode")
    resp = redirect(url_for("scan"))
    if m in ("shared", "private"):
        resp.set_cookie(scanproxy.MODE_COOKIE, m, max_age=2 * 365 * 86400, samesite="Lax")
    return resp


# --------------------------------------------------------------- accounts ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if USERNAME_RE.match(username) and store.check_login(username, password):
            csrf = session.get("csrf")
            session.clear()                 # fresh session on sign-in
            session.permanent = True
            session["user"], session["csrf"] = username, csrf
            flash(f"Signed in as {username}. Your scans are private to your account.", "ok")
            resp = redirect(url_for("scan"))
            resp.delete_cookie(scanproxy.MODE_COOKIE)   # accounts default to private
            return resp
        time.sleep(1)                       # slow down password guessing
        flash("Wrong username or password.", "error")
    return render_template("login.html", allow_signup=ALLOW_SIGNUP)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if not ALLOW_SIGNUP:
        abort(404)
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        err = account_form_error(username, password, request.form.get("password2", ""))
        if not err:
            try:
                store.create_user(username, password)
            except store.sqlite3.IntegrityError:
                err = "That username is taken."
        if err:
            flash(err, "error")
        else:
            session["user"] = username
            session.permanent = True
            flash(f"Account created. Signed in as {username}.", "ok")
            resp = redirect(url_for("scan"))
            resp.delete_cookie(scanproxy.MODE_COOKIE)
            return resp
    return render_template("login.html", allow_signup=True, signup=True)


@app.post("/logout")
def logout():
    session.pop("user", None)
    flash("Signed out.", "ok")
    resp = redirect(url_for("scan"))
    resp.delete_cookie(scanproxy.MODE_COOKIE)
    return resp


def account_form_error(username, password, password2=None):
    if not USERNAME_RE.match(username):
        return "Username: 2–32 lowercase letters, numbers, dots, dashes or underscores."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if password2 is not None and password != password2:
        return "The passwords don't match."
    return None


@app.post("/print")
def print_file():
    f = request.files.get("file")
    printer = request.form.get("printer", "")
    if not f or not f.filename:
        flash("Choose a file to print.", "error")
        return redirect(url_for("print_page"))
    if not NAME_RE.match(printer):
        flash("Choose a printer.", "error")
        return redirect(url_for("print_page"))

    filename = secure_filename(f.filename) or "document"
    ext = Path(filename).suffix.lower()
    if ext not in ACCEPTED:
        flash(f"{ext or 'This file type'} is not supported. Supported: {', '.join(sorted(ACCEPTED))}", "error")
        return redirect(url_for("print_page"))

    options = {}
    try:
        copies = max(1, min(99, int(request.form.get("copies", "1"))))
    except ValueError:
        copies = 1
    if copies > 1:
        options["copies"] = str(copies)
    sides = request.form.get("sides", "one-sided")
    if sides in ("one-sided", "two-sided-long-edge", "two-sided-short-edge"):
        options["sides"] = sides
    color = request.form.get("color", "")
    if color in ("monochrome", "color"):
        options["print-color-mode"] = color
    pages = request.form.get("pages", "").replace(" ", "")
    if pages:
        if not PAGES_RE.match(pages):
            flash("Page range should look like 1-3,5,8-10.", "error")
            return redirect(url_for("print_page"))
        options["page-ranges"] = pages
    if request.form.get("fit"):
        options["fit-to-page"] = "true"
    if request.form.get("landscape"):
        options["orientation-requested"] = "4"
    user = re.sub(r"[^A-Za-z0-9 ._@-]", "", request.form.get("user", ""))[:64].strip() or "web"

    with tempfile.TemporaryDirectory(prefix="print-") as tmp:
        tmp = Path(tmp)
        path = tmp / filename
        f.save(path)
        try:
            if ext in OFFICE:
                path = convert_to_pdf(path, tmp)
            with _cups_user_lock:
                cups.setUser(user)
                try:
                    job = conn().printFile(printer, str(path), filename, options)
                finally:
                    cups.setUser("root")
        except subprocess.TimeoutExpired:
            flash("Converting the document took too long. Try saving it as PDF first.", "error")
            return redirect(url_for("print_page"))
        except (subprocess.CalledProcessError, RuntimeError) as e:
            flash(f"Could not convert the document to PDF ({e}). Try saving it as PDF first.", "error")
            return redirect(url_for("print_page"))
        except cups.IPPError as e:
            flash(f"Printer rejected the job: {e.args[-1] if e.args else e}", "error")
            return redirect(url_for("print_page"))

    flash(f"Sent \u201c{filename}\u201d to {printer} (job {job}).", "ok")
    return redirect(url_for("print_page"))


@app.post("/printers/<name>/test")
def print_test_page(name):
    """Anyone on the LAN can print a test page to check a printer works."""
    if not NAME_RE.match(name):
        abort(404)
    try:
        c = conn()
        queues = c.getPrinters()
        if name not in queues or is_rejecting(queues[name]):
            abort(404)
        with _cups_user_lock:
            cups.setUser("test-page")
            try:
                job = c.printTestPage(name)
            finally:
                cups.setUser("root")
        _status_cache.clear()
        flash(f"Test page sent to {name} (job {job}). It shows the printer's colors and alignment.", "ok")
    except cups.IPPError as e:
        flash(f"Couldn't send the test page: {e.args[-1] if e.args else e}", "error")
    return redirect(url_for("print_page"))


@app.post("/jobs/<int:job_id>/cancel")
def cancel_job(job_id):
    try:
        conn().cancelJob(job_id)
        flash(f"Job {job_id} canceled.", "ok")
    except cups.IPPError as e:
        flash(f"Could not cancel job {job_id}: {e}", "error")
    return redirect(request.referrer or url_for("print_page"))


# ------------------------------------------------------------------ admin ---
@app.get("/admin")
@require_admin
def admin():
    printers = list_printers()
    devices, ppds = None, None
    if request.args.get("discover"):
        try:
            raw = conn().getDevices(timeout=10)
            devices = sorted(
                ({"uri": u, "info": a.get("device-info", ""),
                  "model": a.get("device-make-and-model", ""),
                  "cls": a.get("device-class", ""), "device_id": a.get("device-id", "")}
                 for u, a in raw.items() if ":" in u),
                key=lambda d: (d["cls"] != "direct", d["info"]))
        except cups.IPPError as e:
            flash(f"Device discovery failed: {e}", "error")

    probe = request.args.get("probe", "").strip()
    if probe:
        if HOST_RE.match(probe):
            try:
                socket.getaddrinfo(probe, None)
                devices = probe_host(probe)
                if not devices:
                    flash(f"No printer services answered at {probe} (checked ports 631, 9100, 515).", "error")
            except socket.gaierror:
                devices = []
                flash(f"The hub couldn't resolve the name \u201c{probe}\u201d. Use the printer's IP address instead.", "error")
        else:
            flash("Enter an IP address or hostname, e.g. 192.168.1.50", "error")

    # Mark devices that already have a queue; the scan list hides them unless ?all=1.
    hidden_added = 0
    scanners, scanners_updated = detected_scanners()
    if devices:
        serials = serial_lookup(scanners)
        for d in devices:
            d["added_as"] = queue_for_device(d, printers)
            d["serial"] = device_serial(d["uri"], d.get("device_id"), serials)
        if request.args.get("discover") and not request.args.get("all"):
            hidden_added = sum(1 for d in devices if d["added_as"])
            # Serial ports and the braille driver aren't printers people want here.
            devices = [d for d in devices if not d["added_as"]
                       and not d["uri"].startswith(("serial:", "cups-brf:"))]

    q = request.args.get("q", "").strip()
    if q:
        terms = q.lower().split()
        ppds = [{"name": n, "model": a.get("ppd-make-and-model", n)}
                for n, a in get_ppds().items()
                if all(t in (a.get("ppd-make-and-model", "") + " " + n).lower() for t in terms)]
        ppds.sort(key=lambda p: p["model"])
        ppds = ppds[:300]

    log_tail = ""
    log = DRIVERS_DIR / "install.log"
    if log.exists():
        log_tail = "\n".join(log.read_text(errors="replace").splitlines()[-60:])

    return render_template(
        "admin.html", printers=printers, devices=devices, ppds=ppds, q=q, probe=probe,
        users=store.list_users(), allow_signup=ALLOW_SIGNUP, limits=scanproxy.LIMITS,
        scanners=scanners, scanners_updated=scanners_updated,
        hidden_added=hidden_added, show_all=bool(request.args.get("all")),
        prefill_uri=request.args.get("uri", ""), prefill_model=request.args.get("model", ""),
        log_tail=log_tail, weak_password=ADMIN_PASSWORD in ("changeme", "admin", "password"),
        apt_list=(DRIVERS_DIR / "apt-packages.txt").read_text() if (DRIVERS_DIR / "apt-packages.txt").exists() else "",
    )


@app.post("/admin/printers/add")
@require_admin
def admin_add_printer():
    name = request.form.get("name", "").strip()
    uri = request.form.get("uri", "").strip()
    model = request.form.get("model", "").strip() or "everywhere"
    desc = request.form.get("desc", "").strip()[:127]
    loc = request.form.get("location", "").strip()[:127]
    if not NAME_RE.match(name):
        flash("Queue name may only contain letters, numbers, - and _ (no spaces).", "error")
        return redirect(url_for("admin", uri=uri, model=model))
    if not re.match(r"^[a-z][a-z0-9+.-]*:", uri) or len(uri) > 1024:
        flash("Enter a valid device URI, e.g. ipp://192.168.1.50/ipp/print or socket://192.168.1.50:9100", "error")
        return redirect(url_for("admin", model=model))

    cmd = ["lpadmin", "-p", name, "-E", "-v", uri, "-m", model,
           "-o", "printer-is-shared=true", "-o", "printer-error-policy=retry-job"]
    if desc:
        cmd += ["-D", desc]
    if loc:
        cmd += ["-L", loc]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        hint = (" The printer did not answer as an IPP Everywhere device, so pick a specific driver instead."
                if model == "everywhere" else "")
        flash(f"Adding printer failed: {(p.stderr or p.stdout).strip()}{hint}", "error")
        return redirect(url_for("admin", uri=uri, model=model))
    subprocess.run(["cupsenable", name], capture_output=True)
    subprocess.run(["cupsaccept", name], capture_output=True)
    if request.form.get("default"):
        conn().setDefault(name)
    flash(f"Printer {name} added and shared.", "ok")
    return redirect(url_for("admin"))


@app.post("/admin/printers/<name>/<action>")
@require_admin
def admin_printer_action(name, action):
    if not NAME_RE.match(name):
        abort(404)
    c = conn()
    try:
        _status_cache.clear()
        if action == "delete":
            c.deletePrinter(name)
            flash(f"Removed {name}.", "ok")
        elif action == "default":
            c.setDefault(name)
            flash(f"{name} is now the default printer.", "ok")
        elif action == "test":
            job = c.printTestPage(name)
            flash(f"Test page sent to {name} (job {job}).", "ok")
        elif action == "resume":
            c.enablePrinter(name)
            c.acceptJobs(name)
            flash(f"{name} resumed.", "ok")
        elif action == "disable":
            # Keep the queue and its settings, but stop jobs, hide it from the
            # Home/Print pages and stop offering it to laptops on the network.
            c.rejectJobs(name, reason=DISABLED_REASON)
            c.disablePrinter(name, reason=DISABLED_REASON)
            c.setPrinterShared(name, False)
            flash(f"{name} is disabled: hidden from the Print page and not shared. Enable it any time.", "ok")
        elif action == "enable":
            c.enablePrinter(name)
            c.acceptJobs(name)
            c.setPrinterShared(name, True)
            flash(f"{name} is enabled and shared again.", "ok")
        else:
            abort(404)
    except cups.IPPError as e:
        flash(f"{action} failed: {e}", "error")
    return redirect(url_for("admin"))


@app.post("/admin/drivers/upload")
@require_admin
def admin_driver_upload():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("Choose one or more driver files.", "error")
        return redirect(url_for("admin"))
    outputs, ok = [], True
    with tempfile.TemporaryDirectory(prefix="drv-") as tmp:
        for f in files:
            name = secure_filename(f.filename)
            if not re.search(r"\.(deb|ppd|ppd\.gz|sh)$", name, re.I):
                flash(f"Skipped {f.filename}: only .deb, .ppd, .ppd.gz and .sh are supported.", "error")
                continue
            path = Path(tmp) / name
            f.save(path)
            rc, out = run_hub_driver("add", str(path))
            ok &= rc == 0
            outputs.append(f"### {name}\n{out}")
    return render_template("result.html", title="Driver install", ok=ok, output="\n\n".join(outputs))


@app.post("/admin/drivers/apt")
@require_admin
def admin_driver_apt():
    pkgs = request.form.get("packages", "").split()
    if not pkgs or not all(re.match(r"^[a-z0-9][a-z0-9+.:-]+$", p) for p in pkgs):
        flash("Enter Debian package names separated by spaces, e.g. printer-driver-brlaser", "error")
        return redirect(url_for("admin"))
    rc, out = run_hub_driver("apt", *pkgs)
    return render_template("result.html", title="Package install", ok=rc == 0, output=out)


@app.post("/admin/drivers/sync")
@require_admin
def admin_driver_sync():
    rc, out = run_hub_driver("sync")
    return render_template("result.html", title="Driver sync", ok=rc == 0, output=out)


@app.get("/admin/drivers/search")
@require_admin
def admin_driver_search():
    term = request.args.get("term", "").strip()
    if not re.match(r"^[A-Za-z0-9 ._+-]{2,60}$", term):
        flash("Search term should be 2-60 letters/numbers.", "error")
        return redirect(url_for("admin"))
    rc, out = run_hub_driver("search", term, timeout=300)
    return render_template("result.html", title=f"Driver search: {term}", ok=True, output=out)


@app.post("/admin/scanners/refresh")
@require_admin
def admin_scanners_refresh():
    """Make the scan app search for scanners again (same as ↻ on the Scan page)."""
    try:
        requests.delete(scanproxy.SCANNER_URL + "/api/v1/context", timeout=15)
        r = requests.get(scanproxy.SCANNER_URL + "/api/v1/context", timeout=240)
        found = len(r.json().get("devices", []))
        devices, _ = detected_scanners()
        total = len(devices) if devices is not None else found
        flash(f"Scanner search finished: {total} found ({found} shown on the Scan page).", "ok")
    except (requests.RequestException, ValueError) as e:
        flash(f"Couldn't reach the scan app: {e}", "error")
    return redirect(url_for("admin") + "#scanners")


@app.post("/admin/scanners/<action>")
@require_admin
def admin_scanner_visibility(action):
    key = request.form.get("key", "")
    devices, _ = detected_scanners()
    known = {d["key"] for d in devices or []}
    if action not in ("hide", "show") or key not in known:
        abort(404)
    hidden = hidden_scanners()
    if action == "hide":
        if len(known - hidden - {key}) == 0:
            flash("At least one scanner has to stay visible on the Scan page.", "error")
            return redirect(url_for("admin") + "#scanners")
        hidden.add(key)
    else:
        hidden.discard(key)
    save_hidden_scanners(hidden)
    name = next(d["name"] for d in devices if d["key"] == key)
    flash(f"{name} is now {'hidden from' if action == 'hide' else 'shown on'} the Scan page.", "ok")
    return redirect(url_for("admin") + "#scanners")


@app.post("/admin/users/add")
@require_admin
def admin_user_add():
    username = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    err = account_form_error(username, password)
    if not err:
        try:
            store.create_user(username, password)
        except store.sqlite3.IntegrityError:
            err = f"An account named {username} already exists."
    flash(err or f"Account {username} created. They can sign in on the Scan page.", "error" if err else "ok")
    return redirect(url_for("admin") + "#accounts")


@app.post("/admin/users/<username>/<action>")
@require_admin
def admin_user_action(username, action):
    if not USERNAME_RE.match(username):
        abort(404)
    if action == "password":
        password = request.form.get("password", "")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif store.set_password(username, password):
            flash(f"Password for {username} changed.", "ok")
    elif action == "delete":
        names = store.delete_user(username)
        if request.form.get("files") == "delete":
            scanproxy.delete_owned(names)
            flash(f"Deleted {username} and their {len(names)} scan(s).", "ok")
        else:
            store.forget(names)       # no owner any more -> shared
            flash(f"Deleted {username}. Their {len(names)} scan(s) are now shared.", "ok")
    else:
        abort(404)
    return redirect(url_for("admin") + "#accounts")


@app.errorhandler(413)
def too_large(_):
    flash(f"That upload is larger than the {upload_limit_mb()} MB limit.", "error")
    return redirect(request.referrer or url_for("index"))
