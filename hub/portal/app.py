"""PrintScan Hub portal.

Public:  upload a file from any browser and print it (no drivers on the client).
Admin:   add/remove printers, discover devices, install drivers.
"""
import hashlib
import hmac
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from functools import wraps
from pathlib import Path

import cups
from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.utils import secure_filename

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SCAN_PORT = os.environ.get("SCAN_PORT", "8080")
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

app = Flask(__name__)
# Hard ceiling for any request; per-page limits are checked in check_limits().
app.config["MAX_CONTENT_LENGTH"] = int(max(MAX_UPLOAD_MB, MAX_DRIVER_UPLOAD_MB) * 1024 * 1024) + 1024 * 1024
# Stable across gunicorn workers so flash messages survive redirects.
app.secret_key = hashlib.sha256(b"printscan-hub:" + ADMIN_PASSWORD.encode()).digest()

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
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Form expired, go back and reload the page.")


@app.context_processor
def inject_globals():
    host = request.host.rsplit(":", 1)[0] if not request.host.startswith("[") else request.host.split("]")[0] + "]"
    return {
        "hub_name": HUB_NAME,
        "scan_url": f"http://{host}:{SCAN_PORT}",
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
                "marker-low-levels", "printer-state-reasons", "printer-more-info"]
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
            "label": MARKER_LABELS.get(n.lower(), n),
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
        alerts.append({"text": REASON_TEXT.get(r, r.replace("-", " ").capitalize()), "sev": sev})
    return alerts


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
    if reachable is False:
        alerts.insert(0, {"text": f"Not responding at {host}", "sev": "error"})

    web = None
    more = attrs.get("printer-more-info", "") if live else ""
    m = re.match(r"^(https?://[^/]+)", more or "")
    if m and "localhost" not in m.group(1):
        web = m.group(1) + "/"
    elif host:
        web = f"http://{host}/"

    status = {"supplies": parse_supplies(attrs), "alerts": alerts, "web": web, "live": live}
    _status_cache[(name, uri)] = (time.time(), status)
    return status


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


def list_printers(details=True):
    c = conn()
    printers = c.getPrinters()
    try:
        default = c.getDefault()
    except cups.IPPError:
        default = None
    out = []
    for name, a in sorted(printers.items()):
        status = printer_status(c, name, a.get("device-uri", "")) if details else {}
        out.append({
            **status,
            "name": name,
            "info": a.get("printer-info") or name,
            "location": a.get("printer-location", ""),
            "model": a.get("printer-make-and-model", ""),
            "uri": a.get("device-uri", ""),
            "state": PRINTER_STATES.get(a.get("printer-state"), "?"),
            "message": a.get("printer-state-message", ""),
            "accepting": a.get("printer-is-accepting-jobs", True),
            "shared": a.get("printer-is-shared", True),
            "default": name == default,
        })
    return out


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


@app.get("/")
def index():
    """Home: big Print / Scan buttons plus a quick status overview."""
    try:
        printers, err = list_printers(), None
    except (RuntimeError, cups.IPPError) as e:
        printers, err = [], f"Cannot reach CUPS: {e}"
    return render_template("home.html", printers=printers, error=err, scanners=list_scanners())


@app.get("/print")
def print_page():
    try:
        printers, jobs, err = list_printers(), list_jobs(), None
    except (RuntimeError, cups.IPPError) as e:
        printers, jobs, err = [], [], f"Cannot reach CUPS: {e}"
    return render_template("print.html", printers=printers, jobs=jobs, error=err,
                           scanners=list_scanners(),
                           accepted=sorted(ACCEPTED), has_office=HAS_OFFICE)


@app.get("/scan")
def scan():
    return render_template("scan.html", scanners=list_scanners())


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

    flash(f"Sent â€œ{filename}â€ to {printer} (job {job}).", "ok")
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
                  "cls": a.get("device-class", "")}
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
                flash(f"The hub couldn't resolve the name â€œ{probe}â€. Use the printer's IP address instead.", "error")
        else:
            flash("Enter an IP address or hostname, e.g. 192.168.1.50", "error")

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
        hint = (" The printer did not answer as an IPP Everywhere device â€” pick a specific driver instead."
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


@app.errorhandler(413)
def too_large(_):
    flash(f"That upload is larger than the {upload_limit_mb()} MB limit.", "error")
    return redirect(request.referrer or url_for("index"))
