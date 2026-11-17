"""Install and remove apps: from an uploaded .deb or from the CardputerZero
Store. Store downloads are checked against the store's published SHA-256."""

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

from . import admin

REGISTRY = "https://cardputer.cc/generated/registry.json"
DOWNLOAD_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
                  "raw.githubusercontent.com", "cardputer.cc")
APPLAUNCH_APPS = "/usr/share/APPLaunch/applications"
MAX_DEB = 500 * 1024 * 1024
PKG_RE = re.compile(r"[a-z0-9][a-z0-9+.-]+")
SELF = "zerodesk"
MIN_FREE = 100 * 1024 * 1024
UPLOAD_TTL = 10 * 60
UPLOAD_RE = re.compile(r"upload-[0-9a-f]{16}\.deb")
SHA_RE = re.compile(r"[0-9a-f]{64}")


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "zerodesk", "debs")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def installed_versions():
    try:
        out = subprocess.run(["dpkg-query", "-W", "-f=${Package}\t${Version}\t${db:Status-Abbrev}\n"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    res = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith("ii"):
            res[parts[0]] = parts[1]
    return res


def launcher_packages(apps_dir=APPLAUNCH_APPS):
    """Packages that own a launcher entry: the only ones ZeroDesk will remove."""
    try:
        files = [os.path.join(apps_dir, f) for f in os.listdir(apps_dir) if f.endswith(".desktop")]
    except OSError:
        return {}
    if not files:
        return {}
    out = subprocess.run(["dpkg", "-S"] + files, capture_output=True, text=True, timeout=30).stdout
    res = {}
    for line in out.splitlines():
        pkg, _, path = line.partition(": ")
        for p in pkg.split(", "):
            p = p.split(":")[0].strip()
            if PKG_RE.fullmatch(p) and p != SELF:
                res[p] = os.path.basename(path).rsplit(".desktop", 1)[0]
    return res


def check_space(directory, extra=0):
    if shutil.disk_usage(directory).free - extra < MIN_FREE:
        raise admin.AdminError("Not enough free space on the Cardputer")


def new_upload_path():
    """A fresh name for an uploaded .deb; old unused uploads are cleaned up."""
    d = cache_dir()
    now = time.time()
    for name in os.listdir(d):
        path = os.path.join(d, name)
        try:
            if now - os.stat(path).st_mtime > UPLOAD_TTL:
                os.unlink(path)
        except OSError:
            pass
    check_space(d)
    return os.path.join(d, "upload-%s.deb" % secrets.token_hex(8))


def _valid_entry(a):
    dl = a.get("download") if isinstance(a, dict) else None
    return (isinstance(dl, dict) and isinstance(dl.get("package"), str) and PKG_RE.fullmatch(dl["package"])
            and isinstance(dl.get("sha256"), str) and SHA_RE.fullmatch(dl["sha256"].lower())
            and isinstance(dl.get("url"), str))


def _text(v):
    return v if isinstance(v, str) else ""


class Store:
    def __init__(self):
        self._cache = (0.0, None)

    def apps(self):
        t, data = self._cache
        if data is None or time.time() - t > 600:
            req = urllib.request.Request(REGISTRY, headers={"User-Agent": "ZeroDesk"})
            with urllib.request.build_opener(_CheckRedirects).open(req, timeout=20) as r:
                data = json.loads(r.read(8 * 1024 * 1024))
            if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
                raise admin.AdminError("The store sent something unexpected")
            self._cache = (time.time(), data)
        out = []
        for a in data["apps"]:
            if not _valid_entry(a):
                continue
            dl = a["download"]
            pkg = dl["package"]
            author = a.get("author") if isinstance(a.get("author"), dict) else {}
            size = dl.get("size") if isinstance(dl.get("size"), int) else 0
            cats = [c for c in a.get("categories") or [] if isinstance(c, str)] \
                if isinstance(a.get("categories"), list) else []
            perms = a.get("permissions") if isinstance(a.get("permissions"), dict) else {}
            out.append({"code": _text(a.get("share_code")), "title": _text(a.get("title")) or pkg,
                        "summary": _text(a.get("summary")), "package": pkg,
                        "version": _text(a.get("version")), "size": size, "categories": cats,
                        "author": _text(author.get("display_name")), "permissions": perms})
        return out

    def entry(self, code):
        data = self._cache[1]
        if data is None:
            self.apps()
            data = self._cache[1]
        for a in data["apps"]:
            if isinstance(a, dict) and a.get("share_code") == code and _valid_entry(a):
                return a
        raise admin.AdminError("App not found in the store")


def _allowed_url(url):
    if not isinstance(url, str) or any(c in url for c in "@\\ \t\r\n"):
        return False
    try:
        u = urllib.parse.urlparse(url)
        return u.scheme == "https" and u.hostname in DOWNLOAD_HOSTS
    except ValueError:
        return False


class _CheckRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_url(newurl):
            raise admin.AdminError("Download redirected somewhere unexpected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_store_deb(job, entry):
    dl = entry["download"]
    url, want, pkg = dl["url"], dl["sha256"].lower(), dl["package"]
    if not _allowed_url(url):
        raise admin.AdminError("Unexpected download address")
    d = cache_dir()
    size_hint = dl.get("size") if isinstance(dl.get("size"), int) else 0
    check_space(d, size_hint)
    dest = os.path.join(d, "store-%s.deb" % want)
    job.add("Downloading %s %s...\n" % (entry.get("title", pkg), entry.get("version", "")))
    opener = urllib.request.build_opener(_CheckRedirects)
    h = hashlib.sha256()
    size = 0
    tmp = dest + ".part"
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "ZeroDesk"}), timeout=60) as r, \
                open(tmp, "wb") as fh:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DEB:
                    raise admin.AdminError("Download is too large")
                h.update(chunk)
                fh.write(chunk)
        if h.hexdigest() != want:
            raise admin.AdminError("Checksum mismatch: the download doesn't match the store. Not installing.")
        os.replace(tmp, dest)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if isinstance(e, admin.AdminError):
            raise
        raise admin.AdminError("Download failed: %s" % (str(e) or type(e).__name__))
    try:
        if deb_info(dest).get("Package") != pkg:
            raise admin.AdminError("The download isn't the package the store listed. Not installing.")
    except admin.AdminError:
        os.remove(dest)
        raise
    job.add("Downloaded %d KB, checksum OK.\n" % (size // 1024))
    return dest


def deb_info(path):
    out = subprocess.run(["dpkg-deb", "-f", path, "Package", "Version", "Architecture"],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise admin.AdminError("That isn't a valid .deb package")
    info = {}
    for line in out.stdout.splitlines():
        k, _, v = line.partition(": ")
        info[k.strip()] = v.strip()
    if not PKG_RE.fullmatch(info.get("Package", "")):
        raise admin.AdminError("The package has an invalid name")
    return info


def install_steps(path, password):
    """Install a .deb; the file is deleted afterwards either way."""
    def steps(job):
        try:
            info = deb_info(path)
            job.add("Installing %s %s (%s)\n" % (info.get("Package"), info.get("Version"),
                                                 info.get("Architecture")))
            proc = admin.sudo_popen(["/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get",
                                     "install", "-y", "--no-install-recommends", os.path.abspath(path)],
                                    password)
            admin.stream(job, proc)
            if job.state != "failed":
                job.add("\nInstalled. It's on the Cardputer's home screen.\n")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return steps


def removal_plan(pkg):
    """Packages apt would remove along with pkg (simulated, nothing changes)."""
    out = subprocess.run(["/usr/bin/apt-get", "-s", "remove", pkg], capture_output=True, text=True,
                         timeout=60, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                         stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise admin.AdminError("apt can't remove this package")
    return [line.split()[1] for line in out.stdout.splitlines() if line.startswith("Remv ")]


def priority(pkg):
    out = subprocess.run(["dpkg-query", "-W", "-f=${Priority}\t${Essential}", pkg],
                         capture_output=True, text=True, timeout=30)
    return out.stdout.strip().split("\t") if out.returncode == 0 else ["", ""]


def remove_steps(pkg, password):
    def steps(job):
        if pkg == SELF or pkg not in launcher_packages():
            raise admin.AdminError("Only apps on the home screen can be removed here")
        prio = priority(pkg)
        if prio[0] in ("required", "important") or prio[-1] == "yes":
            raise admin.AdminError("%s is part of the system, so it can't be removed here" % pkg)
        others = [p for p in removal_plan(pkg) if p.split(":")[0] != pkg]
        if others:
            raise admin.AdminError("Removing %s would also remove %s, so it isn't done here. "
                                   "Use the shell if you're sure." % (pkg, ", ".join(others[:8])))
        proc = admin.sudo_popen(["/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get",
                                 "remove", "-y", pkg], password)
        admin.stream(job, proc)
        if job.state != "failed":
            job.add("\nRemoved.\n")
    return steps
