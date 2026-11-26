"""Serve scanservjs through the portal at /scan/app/ and decide who sees which scans.

Modes (per browser, cookie `psh_files`):
  shared   default. Files list shows shared scans; new scans are shared.
  private  Files list shows only this browser's (or signed-in user's) scans;
           new scans belong to them. Browser-private scans are limited in
           number and age; account scans have their own limits.

Preview: scanservjs keeps a single preview image for everyone, and every
preview or scan replaces it. The proxy gives each browser its own: the browser
that made the current image ("holder") is served it live; every other browser
gets its own last image from a snapshot (or a blank page).
"""
import hashlib
import json
import os
import re
import secrets
import time
import urllib.parse

import requests
from flask import Blueprint, Response, abort, jsonify, request, session, stream_with_context

import store

SCANNER_URL = os.environ.get("SCANNER_URL", "http://127.0.0.1:" + os.environ.get("SCAN_PORT", "8080")).rstrip("/")


def _int_env(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


LIMITS = {
    # owner prefix: (max files, keep days); 0 = unlimited
    "b:": (_int_env("PRIVATE_MAX_FILES", 20), _int_env("PRIVATE_KEEP_DAYS", 7)),
    "u:": (_int_env("ACCOUNT_MAX_FILES", 200), _int_env("ACCOUNT_KEEP_DAYS", 0)),
}
BROWSER_COOKIE, MODE_COOKIE = "psh_bid", "psh_files"
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
               "transfer-encoding", "upgrade", "content-length", "content-encoding", "host",
               "cookie", "authorization", "accept-encoding"}
FILE_PATH = re.compile(r"^api/v1/files/([^/]+)(/.*)?$")
PREVIEW_DIR = os.path.join(os.path.dirname(store.DB_PATH), "previews")
PREVIEW_KEEP_DAYS = 14
# Plain white page, used if scanservjs' own placeholder hasn't been seen yet.
BLANK_JPEG = ("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4UG1R"
              "V19iZ2hnPk1xeXBkeFxlZ2P/wAALCABuAFUBAREA/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA"
              "/9oACAEBAAA/AKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA//2Q==")

bp = Blueprint("scanproxy", __name__)
_last_cleanup = {"at": 0.0}


# ------------------------------------------------------------- identity ---
def browser_id():
    bid = request.cookies.get(BROWSER_COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", bid):
        bid = getattr(request, "_new_bid", None) or secrets.token_urlsafe(18)
        request._new_bid = bid           # set as cookie in after_request
    return bid


def current_user():
    return session.get("user")


def owner_key():
    user = current_user()
    return "u:" + user if user else "b:" + browser_id()


def mode():
    m = request.cookies.get(MODE_COOKIE)
    if m in ("shared", "private"):
        return m
    return "private" if current_user() else "shared"


def limits_for(owner):
    return LIMITS[owner[:2]]


def status():
    """Summary for the Scan page bar."""
    owner = owner_key()
    max_files, keep_days = limits_for(owner)
    return {"mode": mode(), "user": current_user(), "count": len(store.files_of(owner)),
            "max_files": max_files, "keep_days": keep_days}


@bp.after_app_request
def set_browser_cookie(resp):
    bid = getattr(request, "_new_bid", None)
    if bid:
        resp.set_cookie(BROWSER_COOKIE, bid, max_age=2 * 365 * 86400, httponly=True, samesite="Lax")
    return resp


# ------------------------------------------------------------ scanner I/O ---
def _forward(path, data=None):
    url = SCANNER_URL + "/" + urllib.parse.quote(path, safe="/:@!$&'()*+,;=-._~")
    if request.query_string:
        url += "?" + request.query_string.decode("latin-1")
    headers = {k: v for k, v in request.headers if k.lower() not in HOP_HEADERS}
    return requests.request(request.method, url, headers=headers,
                            data=request.get_data() if data is None else data,
                            stream=True, timeout=(5, 900), allow_redirects=False)


OPEN_FAILED = re.compile(rb"open of device .{0,200} failed", re.S)


def _forward_scan(path):
    """Forward a preview/scan, retrying once if the scanner refused to open.
    Network scanners sometimes reject the connection for a moment (busy, just
    woken up); nothing has been scanned at that point, so a retry is safe."""
    r = _forward(path)
    if r.status_code >= 400 and OPEN_FAILED.search(r.content or b""):
        time.sleep(2)
        r = _forward(path)
    return r


def _relay(r):
    headers = [(k, v) for k, v in r.raw.headers.items() if k.lower() not in HOP_HEADERS]
    return Response(stream_with_context(r.iter_content(65536)), status=r.status_code, headers=headers)


def _delete_files(names):
    for name in names:
        try:
            requests.delete(SCANNER_URL + "/api/v1/files/" + urllib.parse.quote(name), timeout=15)
        except requests.RequestException:
            pass
    store.forget(names)


def delete_owned(names):
    """Delete files from disk and the ownership table (used when an account is removed)."""
    _delete_files(names)


def _enforce_limit(owner):
    max_files, _ = limits_for(owner)
    if max_files:
        files = store.files_of(owner)
        if len(files) > max_files:
            _delete_files([f["name"] for f in files[: len(files) - max_files]])


def _cleanup(existing_names=None):
    """Expire old private scans and forget rows for files deleted elsewhere."""
    if time.time() - _last_cleanup["at"] < 600 and existing_names is None:
        return
    _last_cleanup["at"] = time.time()
    for prefix, (_, keep_days) in LIMITS.items():
        if keep_days:
            _delete_files(store.expired(prefix, keep_days * 86400))
    if existing_names is not None:
        gone = [n for n in store.owners() if n not in existing_names]
        if gone:
            store.forget(gone)
    _prune_previews()


# --------------------------------------------------------------- preview ---
# State lives in files because gunicorn runs several worker processes:
#   previews/_holder.json   {"key": <browser key that made scanservjs' image, or null>}
#   previews/_default.json  scanservjs' "no preview yet" image
#   previews/<key>.json     {<filter query>: <base64 jpeg>} for one browser
def _pv_key():
    return hashlib.sha256(("b:" + browser_id()).encode()).hexdigest()[:32]


def _pv_read(name, default=None):
    try:
        with open(os.path.join(PREVIEW_DIR, name + ".json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _pv_write(name, data):
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    path = os.path.join(PREVIEW_DIR, name + ".json")
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _holder():
    state = _pv_read("_holder")
    if state is None:
        # First run: an image left over from before belongs to nobody.
        try:
            requests.delete(SCANNER_URL + "/api/v1/preview", timeout=10)
        except requests.RequestException:
            pass
        state = {"key": None}
        _pv_write("_holder", state)
    return state.get("key")


def _set_holder(key):
    _pv_write("_holder", {"key": key})


def _filter_key():
    return "&".join(sorted(f"{k}={v}" for k, v in request.args.items(multi=True) if "filter" in k))


def _begin_capture():
    """Before a preview/scan: this browser's image is about to replace scanservjs' one."""
    me = _pv_key()
    if _holder() != me:
        # Clear the previous browser's image (it has its own snapshot) so a
        # failed scan doesn't show it here.
        try:
            requests.delete(SCANNER_URL + "/api/v1/preview", timeout=10)
        except requests.RequestException:
            pass
        _set_holder(me)


def _end_capture():
    """After a successful preview/scan: snapshot the new image for this browser."""
    me = _pv_key()
    if _holder() != me:
        return
    try:
        r = requests.get(SCANNER_URL + "/api/v1/preview", timeout=60)
        if r.status_code == 200 and _holder() == me:
            _pv_write(me, {"": r.json()["content"]})
    except (requests.RequestException, ValueError, KeyError):
        pass


def _preview_get():
    me, fkey, holder = _pv_key(), _filter_key(), _holder()
    if holder in (me, None):
        r = _forward("api/v1/preview")
        if r.status_code != 200:
            return _relay(r)
        body = r.json()
        if _holder() == holder:                  # nobody replaced it meanwhile
            if holder == me:
                cache = _pv_read(me, {})
                cache[fkey] = body.get("content")
                _pv_write(me, cache)
            else:
                _pv_write("_default", body)
            return jsonify(body)
    cache = _pv_read(me, {})
    content = cache.get(fkey) or cache.get("") or _pv_read("_default", {}).get("content") or BLANK_JPEG
    return jsonify({"content": content})


def _preview_delete():
    me = _pv_key()
    _pv_write(me, {})
    if _holder() == me:
        r = _forward("api/v1/preview")
        _set_holder(None)
        return _relay(r)
    return jsonify({})


# --------------------------------------------- sessions and "scanner in use" ---
#   previews/<key>.meta.json   who/what device, last seen, last action (Admin view)
#   previews/_busy-<id>.json   a preview/scan running on one scanner right now
BUSY_STALE = 15 * 60          # a busy marker older than this is from a crashed request
_UA_OS = [("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Windows", "Windows"),
          ("Mac OS", "Mac"), ("CrOS", "ChromeOS"), ("Linux", "Linux")]
_UA_BROWSER = [("SamsungBrowser", "Samsung Internet"), ("Edg", "Edge"), ("Firefox/", "Firefox"),
               ("FxiOS", "Firefox"), ("CriOS", "Chrome"), ("OPR/", "Opera"), ("Chrome/", "Chrome"),
               ("Safari/", "Safari")]


def device_label(ua):
    ua = ua or ""
    os_name = next((n for p, n in _UA_OS if p in ua), "")
    browser = next((n for p, n in _UA_BROWSER if p in ua), "Browser")
    return f"{browser} on {os_name}" if os_name else browser


def my_key():
    return _pv_key()


def touch(action=None):
    """Record this device for the Admin session list (at most once a minute unless acting)."""
    key, now, user = _pv_key(), time.time(), current_user()
    meta = _pv_read(key + ".meta", {})
    if not action and now - meta.get("last", 0) < 60 and meta.get("user") == user:
        return
    meta.update(user=user, device=device_label(request.headers.get("User-Agent")),
                ip=request.remote_addr, last=now)
    meta.setdefault("first", now)
    if action:
        meta.update(action=action, action_at=now)
    try:
        _pv_write(key + ".meta", meta)
    except OSError:
        pass


def _who(meta):
    return meta.get("user") or meta.get("device") or "Someone"


def _busy_name(device_id):
    return "_busy-" + hashlib.sha256((device_id or "").encode()).hexdigest()[:16]


def busy_list():
    """Previews/scans running now: [{key, who, device_id, kind, started}]."""
    out, now = [], time.time()
    try:
        entries = [e.name[:-5] for e in os.scandir(PREVIEW_DIR) if e.name.startswith("_busy-") and e.name.endswith(".json")]
    except OSError:
        return out
    for name in entries:
        b = _pv_read(name)
        if b and now - b.get("started", 0) < BUSY_STALE:
            out.append(b)
    return out


def _start_busy(kind, device_id):
    me = _pv_key()
    meta = _pv_read(me + ".meta", {})
    _pv_write(_busy_name(device_id), {"key": me, "who": _who(meta), "device": meta.get("device", ""),
                                      "device_id": device_id, "kind": kind, "started": time.time()})


def _end_busy(device_id):
    name = _busy_name(device_id)
    b = _pv_read(name)
    if b and b.get("key") == _pv_key():
        try:
            # The scanner can keep reporting "Processing" for a bit after our scan:
            # remember it was ours so busy_elsewhere() doesn't blame someone else.
            _pv_write(_done_name(device_id), {"at": time.time()})
            os.remove(os.path.join(PREVIEW_DIR, name + ".json"))
        except OSError:
            pass


def _busy_by_other(device_id):
    me = _pv_key()
    return next((b for b in busy_list() if b.get("device_id") == device_id and b.get("key") != me), None)


# ------------------------------------- busy with something outside the hub ---
# The hub only knows about scans it started. A scan from another app (Brother
# iPrint&Scan, Windows Scan...) or from the printer's own panel shows up in the
# scanner's eSCL status as State=Processing, so ask the scanner itself.
EXTERNAL_TTL = 5              # seconds an answer is reused (the Scan page polls every 3 s)
OWN_GRACE = 45                # after our own scan the scanner may still say Processing for a while
_ext_cache = {}


def _escl_url(device_id):
    """eSCL address of a scanner the scan app found, from scanners-detected.json."""
    try:
        data = json.loads(open(os.path.join(os.environ.get("HUB_SHARED_DIR", "/data/shared"),
                                            "scanners-detected.json")).read())
    except (OSError, ValueError):
        return None
    return next((d.get("url") for d in data.get("devices", []) if d.get("id") == device_id and d.get("url")), None)


def _done_name(device_id):
    return "_done-" + hashlib.sha256((device_id or "").encode()).hexdigest()[:16]


def busy_elsewhere(device_id):
    """True if the scanner says it's scanning, and it isn't a scan the hub started or
    just finished. None when the scanner can't tell (no eSCL, offline)."""
    if any(b.get("device_id") == device_id for b in busy_list()):
        return False                           # that's ours: shown by the hub's own marker
    done = _pv_read(_done_name(device_id)) or {}
    if time.time() - done.get("at", 0) < OWN_GRACE:
        return False
    url = _escl_url(device_id)
    if not url:
        return None
    hit = _ext_cache.get(url)
    if hit and time.time() - hit[0] < EXTERNAL_TTL:
        return hit[1]
    try:
        r = requests.get(url.rstrip("/") + "/ScannerStatus", timeout=2)
        m = re.search(r"<(?:\w+:)?State>\s*(\w+)\s*</", r.text) if r.ok else None
        busy = bool(m and m.group(1).lower() == "processing")
    except requests.RequestException:
        busy = None
    _ext_cache[url] = (time.time(), busy)
    return busy


def external_busy():
    """[device id] of scanners busy with a scan the hub didn't start."""
    try:
        data = json.loads(open(os.path.join(os.environ.get("HUB_SHARED_DIR", "/data/shared"),
                                            "scanners-detected.json")).read())
    except (OSError, ValueError):
        return []
    ids = [d["id"] for d in data.get("devices", []) if d.get("url") and not d.get("hidden")]
    return [i for i in ids if busy_elsewhere(i)]


def _claim_scanner(kind, device_id=None):
    """Mark the requested scanner busy, or return a 409 if someone else is using it."""
    if device_id is None:
        params = (request.get_json(silent=True) or {}).get("params") or {}
        device_id = str(params.get("deviceId") or "")
    other = _busy_by_other(device_id)
    if other:
        mins = int((time.time() - other.get("started", 0)) // 60)
        since = f"{mins} min" if mins else "less than a minute"
        return device_id, Response(
            f"The scanner is in use: {other.get('who', 'someone')} started a {other.get('kind', 'scan').lower()} "
            f"{since} ago. Try again when it's finished.", 409, mimetype="text/plain")
    if busy_elsewhere(device_id):
        return device_id, Response(
            "The scanner is in use: it's scanning for another computer or app, or from its own panel. "
            "Try again when it's finished.", 409, mimetype="text/plain")
    touch(kind)
    _start_busy(kind, device_id)
    return device_id, None


def scan_for_hub(params, pipeline, kind="Add pages", filters=()):
    """Run a scan on behalf of a hub page (not the scan app): same "scanner in use"
    rules and preview handling as the Scan page. Returns (file name, None) or
    (None, error text)."""
    device_id, refusal = _claim_scanner(kind, str(params.get("deviceId") or ""))
    if refusal:
        return None, refusal.get_data(as_text=True)
    try:
        _begin_capture()
        body = {"params": params, "batch": "none", "index": 1, "pipeline": pipeline, "filters": list(filters)}
        r = requests.post(SCANNER_URL + "/api/v1/scan", json=body, timeout=(5, 900))
        if r.status_code >= 400 and OPEN_FAILED.search(r.content or b""):
            time.sleep(2)
            r = requests.post(SCANNER_URL + "/api/v1/scan", json=body, timeout=(5, 900))
        if r.status_code != 200:
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            return None, (msg or f"error {r.status_code}").strip()[:300]
        _end_capture()
        name = (r.json().get("file") or {}).get("name")
        return (name, None) if name else (None, "The scan app didn't return a file.")
    except requests.RequestException:
        return None, "The scanner service isn't reachable right now."
    finally:
        _end_busy(device_id)


def remove_scan(name):
    """Delete a scan file through the scan app and forget who owned it."""
    _delete_files([name])


def sessions():
    """Devices that used the Scan page recently, newest first (Admin → Scanners)."""
    busy = {b["key"]: b for b in busy_list()}
    holder, out = _pv_read("_holder", {}).get("key"), []
    try:
        names = [e.name[:-10] for e in os.scandir(PREVIEW_DIR) if e.name.endswith(".meta.json")]
    except OSError:
        return out
    for key in names:
        meta = _pv_read(key + ".meta")
        if not meta:
            continue
        out.append({**meta, "key": key, "busy": busy.get(key), "holder": key == holder,
                    "has_preview": bool(_pv_read(key, {}).get(""))})
    return sorted(out, key=lambda s: -s.get("last", 0))


def preview_jpeg(key):
    """This device's last preview image as JPEG bytes, or None."""
    import base64
    if not re.fullmatch(r"[0-9a-f]{32}", key or ""):
        return None
    content = _pv_read(key, {}).get("")
    try:
        return base64.b64decode(content) if content else None
    except ValueError:
        return None


def _prune_previews():
    cutoff = time.time() - PREVIEW_KEEP_DAYS * 86400
    try:
        for entry in os.scandir(PREVIEW_DIR):
            if not entry.name.startswith("_") and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
    except OSError:
        pass


def visible(name, owners, me, m):
    owner = owners.get(name)
    return owner is None if m == "shared" else owner == me


def may_access(name):
    owner = store.owner_of(name)
    return owner is None or owner == owner_key()


# ---------------------------------------------------------------- routes ---
@bp.route("/scan/app/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE"])
@bp.route("/scan/app/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def scan_app(path):
    # Block cross-site requests (another website trying to drive the scanner).
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        abort(403)
    # The access checks below match on the path as written, but requests/urllib3
    # collapses "a/../b" before forwarding, so "api/v1/x/../files" would reach the
    # unfiltered file list. Refuse anything that isn't already a plain path.
    if "\\" in path or any(seg in ("", ".", "..") for seg in path.split("/")[:-1]) \
            or path.split("/")[-1] in (".", ".."):
        abort(404)
    if path.startswith("api-docs"):
        abort(404)
    touch()

    try:
        # File list: filter to what this browser/user may see in the current mode.
        if path == "api/v1/files" and request.method == "GET":
            r = _forward(path)
            if r.status_code != 200:
                return _relay(r)
            files = r.json()
            _cleanup(existing_names={f.get("name") for f in files})
            owners, me, m = store.owners(), owner_key(), mode()
            return jsonify([f for f in files if visible(f.get("name"), owners, me, m)])

        # Anything addressing one file: check access first.
        fm = FILE_PATH.match(path)
        if fm:
            name = urllib.parse.unquote(fm.group(1))
            if not may_access(name):
                abort(404)
            r = _forward(path)
            if r.status_code < 300:
                if request.method == "DELETE":
                    store.forget([name])
                elif request.method == "PUT":
                    new = (request.get_json(silent=True) or {}).get("newName")
                    if new and store.owner_of(name):
                        store.rename(name, new)
            return _relay(r)

        # Scans: in private mode the finished file belongs to this browser/user.
        # Preview image: one per browser (see the module docstring).
        if path == "api/v1/preview":
            if request.method == "GET":
                return _preview_get()
            if request.method == "DELETE":
                return _preview_delete()
            if request.method == "POST":
                device_id, refusal = _claim_scanner("Preview")
                if refusal:
                    return refusal
                try:
                    _begin_capture()
                    r = _forward_scan(path)
                    if r.status_code < 300:
                        _end_capture()
                finally:
                    _end_busy(device_id)
                return _relay(r)
        if path == "api/v1/context" and request.method == "DELETE":
            r = _forward(path)                   # scanservjs' Reset also deletes its preview
            if r.status_code < 300:
                _set_holder(None)
                _pv_write(_pv_key(), {})
            return _relay(r)

        if path == "api/v1/scan" and request.method == "POST":
            device_id, refusal = _claim_scanner("Scan")
            if refusal:
                return refusal
            try:
                _begin_capture()
                r = _forward_scan(path)
                if r.status_code != 200:
                    return _relay(r)
                _end_capture()
                body = r.json()
            finally:
                _end_busy(device_id)
            name = (body.get("file") or {}).get("name") if isinstance(body, dict) else None
            if name and mode() == "private":
                owner = owner_key()
                store.claim(name, owner)
                _enforce_limit(owner)
            return jsonify(body)

        _cleanup()
        return _relay(_forward(path))
    except requests.RequestException:
        return Response("The scanner service isn't reachable right now. Try again in a moment.",
                        502, mimetype="text/plain")
