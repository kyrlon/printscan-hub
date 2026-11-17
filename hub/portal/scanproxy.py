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
    if path.startswith("api-docs"):
        abort(404)

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
                _begin_capture()
                r = _forward(path)
                if r.status_code < 300:
                    _end_capture()
                return _relay(r)
        if path == "api/v1/context" and request.method == "DELETE":
            r = _forward(path)                   # scanservjs' Reset also deletes its preview
            if r.status_code < 300:
                _set_holder(None)
                _pv_write(_pv_key(), {})
            return _relay(r)

        if path == "api/v1/scan" and request.method == "POST":
            _begin_capture()
            r = _forward(path)
            if r.status_code != 200:
                return _relay(r)
            _end_capture()
            body = r.json()
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
