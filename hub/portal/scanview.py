"""View & rotate a saved scan: page thumbnails, a zoomable page view, and saving
rotated pages back into the file.

scanservjs (the scan app) can list, download, rename and delete scans, but has no
viewer. The scans folder is shared with the hub (./scans -> /scans), so the hub
renders pages itself (pdftoppm for PDFs, Pillow for images and the .zip of images
scanservjs makes for multi-page JPG/PNG scans) and rewrites the file on Save:
turn, reorder and delete pages. PDFs with qpdf, which only changes each page's
/Rotate flag and page list (no quality loss, text layer kept); images with Pillow.

Who may open a scan follows the Scan page's rules (scanproxy.may_access): shared
scans for everyone, private ones only for their owner.
"""
import hashlib
import json
import os
import shutil
import subprocess
import io
import re
import tempfile
import time
import zipfile

import requests
from flask import Blueprint, abort, jsonify, render_template, request, send_file

import scanproxy
import store

SCANS_DIR = os.environ.get("SCANS_DIR", "/scans")
CACHE_DIR = os.path.join(tempfile.gettempdir(), "psh-view")
CACHE_KEEP = 24 * 3600
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
SIZES = {"thumb": (45, 360), "full": (150, 2600)}     # PDF dpi, image max side
SKIP_BLANK = "Skip blank pages"                       # the scan app's filter (scanservjs/config/config.local.js)

bp = Blueprint("scanview", __name__)


def scan_path(name):
    """Absolute path of a scan this browser may see, or 404."""
    if not name or "/" in name or "\\" in name or name.startswith(".") or "\0" in name:
        abort(404)
    path = os.path.join(SCANS_DIR, name)
    ext = os.path.splitext(name)[1].lower()
    if ext not in (".pdf", ".zip") and ext not in IMAGE_EXT:
        abort(404)
    if not os.path.isfile(path) or not scanproxy.may_access(name):
        abort(404)
    return path


def is_pdf(path):
    return path.lower().endswith(".pdf")


def is_zip(path):
    return path.lower().endswith(".zip")


def zip_members(zf):
    """Images inside a scan archive, in name order (scanservjs zips multi-page
    JPG/PNG scans as scan-0001.jpg, scan-0002.jpg...)."""
    return sorted(i.filename for i in zf.infolist()
                  if not i.is_dir() and os.path.splitext(i.filename)[1].lower() in IMAGE_EXT
                  and not os.path.basename(i.filename).startswith("."))


def page_count(path):
    if is_zip(path):
        try:
            with zipfile.ZipFile(path) as zf:
                return len(zip_members(zf))
        except (OSError, zipfile.BadZipFile):
            return 0
    if is_pdf(path):
        try:
            out = subprocess.run(["pdfinfo", path], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return 0
        for line in out.splitlines():
            if line.startswith("Pages:"):
                return int(line.split()[1])
        return 0
    from PIL import Image
    try:
        with Image.open(path) as im:
            return getattr(im, "n_frames", 1)
    except OSError:
        return 0


def _cache_name(path, page, size):
    st = os.stat(path)
    key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:20]
    return os.path.join(CACHE_DIR, f"{key}-{page}-{size}.jpg")


def _prune_cache():
    cutoff = time.time() - CACHE_KEEP
    try:
        for e in os.scandir(CACHE_DIR):
            if e.stat().st_mtime < cutoff:
                os.remove(e.path)
    except OSError:
        pass


def render_page(path, page, size):
    """JPEG file of one page (1-based), rendered once and cached."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    out = _cache_name(path, page, size)
    if os.path.exists(out):
        return out
    _prune_cache()
    dpi, max_side = SIZES[size]
    tmp = out + f".{os.getpid()}.tmp"
    if is_pdf(path):
        r = subprocess.run(["pdftoppm", "-jpeg", "-jpegopt", "quality=85", "-r", str(dpi),
                            "-f", str(page), "-l", str(page), "-singlefile", path, tmp],
                           capture_output=True, timeout=120)
        if r.returncode != 0 or not os.path.exists(tmp + ".jpg"):
            abort(500)
        os.replace(tmp + ".jpg", out)
    else:
        from PIL import Image
        if is_zip(path):
            with zipfile.ZipFile(path) as zf:
                src, frame = io.BytesIO(zf.read(zip_members(zf)[page - 1])), 0
        else:
            src, frame = path, page - 1
        with Image.open(src) as im:
            im.seek(frame)
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            im.save(tmp, "JPEG", quality=85)
        os.replace(tmp, out)
    return out


def _turn(img, deg):
    from PIL import Image
    turn = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    return img.transpose(turn[deg]) if deg else img


def _open_frames(src):
    """All frames of an image, its format, and save options that keep its quality."""
    from PIL import Image, ImageSequence
    with Image.open(src) as im:
        fmt, info = im.format, dict(im.info)
        frames = [f.copy() for f in ImageSequence.Iterator(im)]
    opts = {k: info[k] for k in ("dpi", "icc_profile") if k in info}
    if fmt == "JPEG":
        opts.update(quality=95, subsampling=0)
    elif fmt == "TIFF" and info.get("compression"):
        opts["compression"] = info["compression"]
    return frames, fmt, opts


def _turn_image_bytes(data, deg):
    frames, fmt, opts = _open_frames(io.BytesIO(data))
    buf = io.BytesIO()
    frames = [_turn(f, deg) for f in frames]
    if len(frames) > 1:
        frames[0].save(buf, fmt, save_all=True, append_images=frames[1:], **opts)
    else:
        frames[0].save(buf, fmt, **opts)
    return buf.getvalue()


def _qpdf(args):
    r = subprocess.run(["qpdf"] + args, capture_output=True, text=True, timeout=300)
    if r.returncode not in (0, 3):                   # 3 = done, with warnings
        raise RuntimeError((r.stderr or r.stdout).strip() or "qpdf failed")


def edit_file(path, order, rotations):
    """Rewrite a scan: keep the pages in `order` (original page numbers, so pages
    left out are deleted) and turn them clockwise by {original page: degrees}."""
    st = os.stat(path)
    ext = os.path.splitext(path)[1]
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".edit-", suffix=ext)
    os.close(fd)
    tmp2 = None
    try:
        if is_pdf(path):
            src = path
            if order != list(range(1, len(order) + 1)) or len(order) != page_count(path):
                fd, tmp2 = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".edit-", suffix=ext)
                os.close(fd)
                _qpdf(["--empty", "--pages", path, ",".join(map(str, order)), "--", tmp2])
                src = tmp2
            # Rotations now refer to positions in the new order. qpdf only changes
            # each page's /Rotate entry: no re-encoding, text layer kept.
            by_angle = {}
            for pos, page in enumerate(order, 1):
                if rotations.get(page):
                    by_angle.setdefault(rotations[page], []).append(str(pos))
            _qpdf([f"--rotate=+{deg}:{','.join(p)}" for deg, p in by_angle.items()] + [src, tmp])
        elif is_zip(path):
            # One image per page: copy unchanged pages as they are, re-save turned
            # ones, and renumber the names so unzipping keeps the new order.
            with zipfile.ZipFile(path) as zf, zipfile.ZipFile(tmp, "w") as out:
                members = zip_members(zf)
                for pos, page in enumerate(order, 1):
                    member = members[page - 1]
                    info = zf.getinfo(member)
                    data = zf.read(member)
                    if rotations.get(page):
                        data = _turn_image_bytes(data, rotations[page])
                    m = re.match(r"^(.*?)(\d+)(\.\w+)$", member)
                    new = f"{m.group(1)}{pos:0{len(m.group(2))}d}{m.group(3)}" if m else member
                    out.writestr(zipfile.ZipInfo(new, info.date_time), data, compress_type=info.compress_type)
        else:
            frames, fmt, opts = _open_frames(path)
            frames = [_turn(frames[p - 1], rotations.get(p)) for p in order]
            if len(frames) > 1:
                frames[0].save(tmp, fmt, save_all=True, append_images=frames[1:], **opts)
            else:
                frames[0].save(tmp, fmt, **opts)
        shutil.copymode(path, tmp)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
        os.replace(tmp, path)
        # Keep its date, so it stays in place in the file list and private-scan expiry
        # still counts from when it was scanned.
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    finally:
        for t in (tmp, tmp2):
            if t and os.path.exists(t):
                os.remove(t)


def _append(path, new_path):
    """Put the pages of new_path (a fresh scan) after the pages of path.
    Returns the file's path afterwards: a single JPG/PNG becomes a .zip of images,
    because one image file can't hold several pages."""
    st = os.stat(path)
    ext = os.path.splitext(path)[1].lower()
    target = path if ext in (".pdf", ".zip") else os.path.splitext(path)[0] + ".zip"
    if target != path and os.path.exists(target):
        raise RuntimeError(f"{os.path.basename(target)} already exists")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".edit-", suffix=os.path.splitext(target)[1])
    os.close(fd)
    try:
        if ext == ".pdf":
            if not is_pdf(new_path):
                raise RuntimeError("the new scan isn't a PDF")
            _qpdf(["--empty", "--pages", path, new_path, "--", tmp])
        else:
            def images(p):              # [(name, bytes, date_time)] of a zip or a single image
                if is_zip(p):
                    with zipfile.ZipFile(p) as zf:
                        return [(m, zf.read(m), zf.getinfo(m).date_time) for m in zip_members(zf)]
                with open(p, "rb") as f:
                    return [(os.path.basename(p), f.read(), time.localtime(os.path.getmtime(p))[:6])]
            pages = images(path) + images(new_path)
            stem = re.sub(r"\.\w+$", "", os.path.basename(target))
            width = max(4, len(str(len(pages))))
            with zipfile.ZipFile(tmp, "w") as out:
                for i, (name, data, when) in enumerate(pages, 1):
                    out.writestr(zipfile.ZipInfo(f"{stem}-{i:0{width}d}{os.path.splitext(name)[1].lower()}", when), data)
        shutil.copymode(path, tmp)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
        os.replace(tmp, target)
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        if target != path:
            os.remove(path)
        return target
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _pipeline_for(path, options):
    """The scan app's output format that matches this file, from the pipelines it offers."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        want = [r"^PDF \(JPG", r"^PDF"]
    elif ext == ".png":
        want = [r"^PNG"]
    else:                               # .zip / .jpg: scanservjs zips multi-page JPG scans
        want = [r"^JPG \| @:pipeline.high-quality", r"^JPG"]
        if is_zip(path):
            try:
                with zipfile.ZipFile(path) as zf:
                    if any(m.lower().endswith(".png") for m in zip_members(zf)):
                        want = [r"^PNG"]
            except (OSError, zipfile.BadZipFile):
                pass
    for pattern in want:
        for o in options:
            if re.search(pattern, o, re.I) and "ocr" not in o.lower():
                return o
    return None


@bp.route("/scan/view/add/options")
def add_options():
    """Scanners and their settings for the Add pages panel (from the scan app)."""
    path = scan_path(request.args.get("f", ""))
    try:
        ctx = requests.get(scanproxy.SCANNER_URL + "/api/v1/context", timeout=60).json()
    except (requests.RequestException, ValueError):
        return jsonify(ok=False, message="The scanner service isn't reachable right now."), 502
    out = []
    for d in ctx.get("devices", []):
        f = d.get("features", {})
        pipe = _pipeline_for(path, (d.get("settings", {}).get("pipeline") or {}).get("options") or [])
        if not pipe:
            continue
        pick = lambda k: {"options": (f.get(k) or {}).get("options") or [], "default": (f.get(k) or {}).get("default")}
        res = pick("--resolution")
        filters = (d.get("settings", {}).get("filters") or {}).get("options") or []
        out.append({"id": d["id"], "name": d.get("name") or d["id"], "source": pick("--source"),
                    "resolution": res, "mode": pick("--mode"), "skip_blank": SKIP_BLANK in filters,
                    "feeder": ((f.get("--source") or {}).get("hubFeeder"))})
    if not out:
        return jsonify(ok=False, message="No scanner is available right now."), 404
    return jsonify(ok=True, devices=out)


@bp.route("/scan/view/add", methods=["POST"])
def add_pages():
    name = request.form.get("f", "")
    path = scan_path(name)
    try:
        ctx = requests.get(scanproxy.SCANNER_URL + "/api/v1/context", timeout=60).json()
    except (requests.RequestException, ValueError):
        return jsonify(ok=False, message="The scanner service isn't reachable right now."), 502
    dev = next((d for d in ctx.get("devices", []) if d.get("id") == request.form.get("device")), None)
    if not dev:
        return jsonify(ok=False, message="That scanner isn't available any more. Reload the page."), 400
    f = dev.get("features", {})
    pipe = _pipeline_for(path, (dev.get("settings", {}).get("pipeline") or {}).get("options") or [])
    params = {"deviceId": dev["id"]}
    for field, feat in (("source", "--source"), ("mode", "--mode")):
        v = request.form.get(field)
        if v and v in ((f.get(feat) or {}).get("options") or []):
            params[field] = v
    try:
        res = int(request.form.get("resolution", "0"))
        if res in ((f.get("--resolution") or {}).get("options") or []):
            params["resolution"] = res
    except ValueError:
        pass
    # Same scan area as the Scan page starts with (the default paper size).
    for key, feat in (("left", "-l"), ("top", "-t"), ("width", "-x"), ("height", "-y")):
        if f.get(feat, {}).get("default") is not None:
            params[key] = f[feat]["default"]
    filters = (dev.get("settings", {}).get("filters") or {}).get("options") or []
    skip = [SKIP_BLANK] if request.form.get("skip_blank") and SKIP_BLANK in filters else []
    before = page_count(path)
    new_name, err = scanproxy.scan_for_hub(params, pipe, filters=skip)
    if err:
        return jsonify(ok=False, message=f"Couldn't scan: {err}"), 409 if "in use" in err else 500
    new_path = os.path.join(SCANS_DIR, new_name)
    try:
        target = _append(path, new_path)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, zipfile.BadZipFile) as e:
        return jsonify(ok=False, message=f"Scanned, but couldn't add the pages: {e}. "
                                         f"The new pages are saved separately as {new_name}."), 500
    scanproxy.remove_scan(new_name)
    new_file = os.path.basename(target)
    if new_file != name and store.owner_of(name):
        store.rename(name, new_file)
    added = page_count(target) - before
    note = f" It's now {new_file} (a .zip of images), because a JPG/PNG holds one page." if new_file != name else ""
    return jsonify(ok=True, file=new_file,
                   message=f"Added {added} page{'s' if added != 1 else ''}.{note}")


@bp.route("/scan/view")
def view():
    name = request.args.get("f", "")
    path = scan_path(name)
    return render_template("scanview.html", name=name, pages=page_count(path), pdf=is_pdf(path),
                           saved=request.args.get("saved", "")[:200])


@bp.route("/scan/view/download")
def download():
    """The file as it is now (served by the hub, so it's never a cached or stale copy)."""
    name = request.args.get("f", "")
    resp = send_file(scan_path(name), as_attachment=True, download_name=name, max_age=0, conditional=False)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/scan/view/page")
def page():
    path = scan_path(request.args.get("f", ""))
    size = request.args.get("s", "thumb")
    if size not in SIZES:
        abort(404)
    try:
        n = int(request.args.get("p", "1"))
    except ValueError:
        abort(404)
    if n < 1 or n > max(1, page_count(path)):
        abort(404)
    resp = send_file(render_page(path, n, size), mimetype="image/jpeg", max_age=0)
    resp.headers["Cache-Control"] = "private, no-cache"
    return resp


@bp.route("/scan/view/save", methods=["POST"])
def save():
    name = request.form.get("f", "")
    path = scan_path(name)
    pages = page_count(path)
    try:
        raw = json.loads(request.form.get("rotations", "{}"))
        rotations = {}
        for k, v in raw.items():
            p, deg = int(k), int(v) % 360
            if not 1 <= p <= pages or deg not in (0, 90, 180, 270):
                raise ValueError
            if deg:
                rotations[p] = deg
        order = json.loads(request.form.get("order", "null"))
        order = list(range(1, pages + 1)) if order is None else [int(p) for p in order]
        if len(set(order)) != len(order) or any(not 1 <= p <= pages for p in order):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        return jsonify(ok=False, message="Those changes don't make sense for this file."), 400
    if not order:
        return jsonify(ok=False, message="Keep at least one page. To remove the whole scan, "
                                         "delete it under Files on the Scan page."), 400
    rotations = {p: d for p, d in rotations.items() if p in order}
    moved = order != sorted(order)
    deleted = pages - len(order)
    if not rotations and not moved and not deleted:
        return jsonify(ok=True, message="Nothing to change.")
    try:
        edit_file(path, order, rotations)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
        return jsonify(ok=False, message=f"Couldn't save: {e}"), 500
    done = []
    if rotations:
        done.append(f"{len(rotations)} page{'s' if len(rotations) != 1 else ''} turned")
    if deleted:
        done.append(f"{deleted} deleted")
    if moved:
        done.append("pages reordered")
    return jsonify(ok=True, message="Saved: " + ", ".join(done) + ".")
