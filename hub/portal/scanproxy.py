"""Serve scanservjs through the portal at /scan/app/ and decide who sees which scans.

Modes (per browser, cookie `psh_files`):
  shared   default. Files list shows shared scans; new scans are shared.
  private  Files list shows only this browser's (or signed-in user's) scans;
           new scans belong to them. Browser-private scans are limited in
           number and age; account scans have their own limits.
"""
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
        if path == "api/v1/scan" and request.method == "POST":
            r = _forward(path)
            if r.status_code != 200:
                return _relay(r)
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
