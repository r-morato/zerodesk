"""Install and remove apps: from an uploaded .deb or from the CardputerZero
Store. Store downloads are checked against the store's published SHA-256."""

import hashlib
import json
import os
import re
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


class Store:
    def __init__(self):
        self._cache = (0.0, None)

    def apps(self):
        t, data = self._cache
        if data is None or time.time() - t > 600:
            req = urllib.request.Request(REGISTRY, headers={"User-Agent": "ZeroDesk"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read(8 * 1024 * 1024))
            self._cache = (time.time(), data)
        out = []
        for a in data.get("apps", []):
            dl = a.get("download") or {}
            pkg = dl.get("package") or ""
            if not PKG_RE.fullmatch(pkg) or not re.fullmatch(r"[0-9a-f]{64}", dl.get("sha256", "")):
                continue
            out.append({"code": a.get("share_code", ""), "title": a.get("title", pkg),
                        "summary": a.get("summary", ""), "package": pkg, "version": a.get("version", ""),
                        "size": dl.get("size", 0), "categories": a.get("categories", []),
                        "author": (a.get("author") or {}).get("display_name", ""),
                        "permissions": a.get("permissions", {})})
        return out

    def entry(self, code):
        data = self._cache[1]
        if data is None:
            self.apps()
            data = self._cache[1]
        for a in data.get("apps", []):
            if a.get("share_code") == code:
                return a
        raise admin.AdminError("App not found in the store")


def _allowed_url(url):
    u = urllib.parse.urlparse(url)
    return u.scheme == "https" and u.hostname in DOWNLOAD_HOSTS


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
    dest = os.path.join(cache_dir(), "%s.deb" % pkg)
    job.add("Downloading %s %s...\n" % (entry.get("title", pkg), entry.get("version", "")))
    opener = urllib.request.build_opener(_CheckRedirects)
    h = hashlib.sha256()
    size = 0
    tmp = dest + ".part"
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
        os.remove(tmp)
        raise admin.AdminError("Checksum mismatch: the download doesn't match the store. Not installing.")
    os.replace(tmp, dest)
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
    def steps(job):
        info = deb_info(path)
        job.add("Installing %s %s (%s)\n" % (info.get("Package"), info.get("Version"),
                                             info.get("Architecture")))
        proc = admin.sudo_popen(["/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get",
                                 "install", "-y", "--no-install-recommends", os.path.abspath(path)],
                                password)
        admin.stream(job, proc)
        if job.state != "failed":
            job.add("\nInstalled. It's on the Cardputer's home screen.\n")
    return steps


def remove_steps(pkg, password):
    def steps(job):
        if pkg == SELF or pkg not in launcher_packages():
            raise admin.AdminError("Only apps on the home screen can be removed here")
        proc = admin.sudo_popen(["/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get",
                                 "remove", "-y", pkg], password)
        admin.stream(job, proc)
        if job.state != "failed":
            job.add("\nRemoved.\n")
    return steps
