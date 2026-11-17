"""File browser operations. Runs as your user, so normal Linux permissions
decide what can be read or changed."""

import os
import shutil
import stat
import time

PREVIEW_TEXT_MAX = 512 * 1024


class FileError(Exception):
    pass


def clean_path(path, default=None):
    path = path or default or os.path.expanduser("~")
    if "\x00" in path:
        raise FileError("Invalid path")
    if not os.path.isabs(path):
        raise FileError("Path must be absolute")
    return os.path.normpath(path)


def clean_name(name):
    if not name or name in (".", "..") or "/" in name or "\x00" in name or len(name) > 255:
        raise FileError("Invalid name")
    return name


def _err(e):
    return FileError(e.strerror or str(e))


def listdir(path):
    path = clean_path(path)
    try:
        names = os.listdir(path)
    except OSError as e:
        raise _err(e)
    entries = []
    for n in names:
        p = os.path.join(path, n)
        try:
            st = os.lstat(p)
        except OSError:
            continue
        kind = "dir" if stat.S_ISDIR(st.st_mode) else "link" if stat.S_ISLNK(st.st_mode) else \
            "file" if stat.S_ISREG(st.st_mode) else "other"
        target_dir = False
        if kind == "link":
            try:
                target_dir = os.path.isdir(p)
            except OSError:
                pass
        entries.append({"name": n, "type": kind, "dir": kind == "dir" or target_dir,
                        "size": st.st_size, "mtime": int(st.st_mtime),
                        "mode": stat.filemode(st.st_mode)})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    parent = os.path.dirname(path) if path != "/" else None
    return {"path": path, "parent": parent, "home": os.path.expanduser("~"), "entries": entries}


def open_for_download(path):
    path = clean_path(path)
    try:
        st = os.stat(path)
    except OSError as e:
        raise _err(e)
    if not stat.S_ISREG(st.st_mode):
        raise FileError("Not a regular file")
    if not os.access(path, os.R_OK):
        raise FileError("Permission denied")
    return path, st.st_size


def preview_text(path):
    path, size = open_for_download(path)
    with open(path, "rb") as fh:
        data = fh.read(PREVIEW_TEXT_MAX + 1)
    if b"\x00" in data[:8192]:
        raise FileError("Binary file")
    return {"path": path, "size": size, "truncated": len(data) > PREVIEW_TEXT_MAX,
            "text": data[:PREVIEW_TEXT_MAX].decode("utf-8", "replace")}


def save_text(path, text):
    path = clean_path(path)
    if len(text.encode("utf-8")) > PREVIEW_TEXT_MAX:
        raise FileError("File too large to edit here")
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o644
        tmp = path + ".zerodesk-tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError as e:
        raise _err(e)


def mkdir(parent, name):
    p = os.path.join(clean_path(parent), clean_name(name))
    try:
        os.mkdir(p)
    except OSError as e:
        raise _err(e)
    return p


def rename(path, new_name):
    path = clean_path(path)
    dst = os.path.join(os.path.dirname(path), clean_name(new_name))
    if os.path.lexists(dst):
        raise FileError("Something with that name already exists")
    try:
        os.rename(path, dst)
    except OSError as e:
        raise _err(e)
    return dst


def delete(path):
    path = clean_path(path)
    if path == "/" or path == os.path.expanduser("~"):
        raise FileError("Refusing to delete that")
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError as e:
        raise _err(e)


def upload_target(directory, name, overwrite=False):
    d = clean_path(directory)
    p = os.path.join(d, clean_name(name))
    if os.path.lexists(p) and not overwrite:
        raise FileError("'%s' already exists" % name)
    if not os.path.isdir(d):
        raise FileError("Folder not found")
    return p, "%s.zerodesk-upload-%d" % (p, int(time.time() * 1000))
