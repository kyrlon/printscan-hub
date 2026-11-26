"""View & rotate a saved scan: page thumbnails, a zoomable page view, and saving
rotated pages back into the file.

scanservjs (the scan app) can list, download, rename and delete scans, but has no
viewer. The scans folder is shared with the hub (./scans -> /scans), so the hub
renders pages itself (pdftoppm for PDFs, Pillow for images) and rewrites the file
on Save: PDFs with qpdf, which only changes each page's /Rotate flag (no quality
loss, text layer kept); images with Pillow.

Who may open a scan follows the Scan page's rules (scanproxy.may_access): shared
scans for everyone, private ones only for their owner.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time

from flask import Blueprint, abort, jsonify, render_template, request, send_file

import scanproxy

SCANS_DIR = os.environ.get("SCANS_DIR", "/scans")
CACHE_DIR = os.path.join(tempfile.gettempdir(), "psh-view")
CACHE_KEEP = 24 * 3600
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
SIZES = {"thumb": (45, 360), "full": (150, 2600)}     # PDF dpi, image max side

bp = Blueprint("scanview", __name__)


def scan_path(name):
    """Absolute path of a scan this browser may see, or 404."""
    if not name or "/" in name or "\\" in name or name.startswith(".") or "\0" in name:
        abort(404)
    path = os.path.join(SCANS_DIR, name)
    ext = os.path.splitext(name)[1].lower()
    if ext != ".pdf" and ext not in IMAGE_EXT:
        abort(404)
    if not os.path.isfile(path) or not scanproxy.may_access(name):
        abort(404)
    return path


def is_pdf(path):
    return path.lower().endswith(".pdf")


def page_count(path):
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
        with Image.open(path) as im:
            im.seek(page - 1)
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            im.save(tmp, "JPEG", quality=85)
        os.replace(tmp, out)
    return out


def rotate_file(path, rotations):
    """Turn pages clockwise by 90/180/270 degrees: {page number: degrees}."""
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rotate-", suffix=os.path.splitext(path)[1])
    os.close(fd)
    try:
        if is_pdf(path):
            args = ["qpdf", path, tmp]
            by_angle = {}
            for page, deg in rotations.items():
                by_angle.setdefault(deg, []).append(str(page))
            for deg, pages in by_angle.items():
                args.insert(1, f"--rotate=+{deg}:{','.join(pages)}")
            r = subprocess.run(args, capture_output=True, text=True, timeout=300)
            if r.returncode not in (0, 3):           # 3 = done, with warnings
                raise RuntimeError((r.stderr or r.stdout).strip() or "qpdf failed")
        else:
            from PIL import Image, ImageSequence
            turn = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                    270: Image.Transpose.ROTATE_90}
            with Image.open(path) as im:
                fmt, info = im.format, dict(im.info)
                frames = [f.copy() for f in ImageSequence.Iterator(im)]
            frames = [f.transpose(turn[rotations[i + 1]]) if rotations.get(i + 1) else f
                      for i, f in enumerate(frames)]
            opts = {k: info[k] for k in ("dpi", "icc_profile") if k in info}
            if fmt == "JPEG":
                opts.update(quality=95, subsampling=0)
            elif fmt == "TIFF" and info.get("compression"):
                opts["compression"] = info["compression"]
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
        if os.path.exists(tmp):
            os.remove(tmp)


@bp.route("/scan/view")
def view():
    name = request.args.get("f", "")
    path = scan_path(name)
    return render_template("scanview.html", name=name, pages=page_count(path), pdf=is_pdf(path),
                           download=scanproxy.file_url(name))


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
    try:
        raw = json.loads(request.form.get("rotations", "{}"))
        pages = page_count(path)
        rotations = {}
        for k, v in raw.items():
            p, deg = int(k), int(v) % 360
            if not 1 <= p <= pages or deg not in (0, 90, 180, 270):
                raise ValueError
            if deg:
                rotations[p] = deg
    except (ValueError, TypeError, AttributeError):
        return jsonify(ok=False, message="Those changes don't make sense for this file."), 400
    if not rotations:
        return jsonify(ok=True, message="Nothing to change.")
    try:
        rotate_file(path, rotations)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
        return jsonify(ok=False, message=f"Couldn't save: {e}"), 500
    n = len(rotations)
    return jsonify(ok=True, message=f"Saved. {n} page{'s' if n != 1 else ''} rotated.")
