"""File browser operations. Runs as your user, so normal Linux permissions
decide what can be read or changed."""

import os
import secrets
import shutil
import stat
import tempfile

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


def open_download(path):
    """Open the exact file asked for and check it *after* opening (no races,
    no substitution). Returns (file object, name, size)."""
    path = clean_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        raise _err(e)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise FileError("Not a regular file")
    return os.fdopen(fd, "rb"), os.path.basename(path), st.st_size


def preview_text(path):
    path, size = open_for_download(path)
    with open(path, "rb") as fh:
        data = fh.read(PREVIEW_TEXT_MAX + 1)
    if b"\x00" in data[:8192]:
        raise FileError("Binary file")
    return {"path": path, "size": size, "truncated": len(data) > PREVIEW_TEXT_MAX,
            "text": data[:PREVIEW_TEXT_MAX].decode("utf-8", "replace")}


def save_text(path, text):
    """Atomic save that writes *through* symlinks and keeps the file's mode."""
    path = os.path.realpath(clean_path(path))
    data = text.encode("utf-8")
    if len(data) > PREVIEW_TEXT_MAX:
        raise FileError("File too large to edit here")
    try:
        st = os.stat(path) if os.path.exists(path) else None
        fd, tmp = tempfile.mkstemp(prefix=".zerodesk-", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, stat.S_IMODE(st.st_mode) if st else 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        raise _err(e)


def mkdir(parent, name):
    p = os.path.join(clean_path(parent), clean_name(name))
    try:
        os.mkdir(p)
    except OSError as e:
        raise _err(e)
    return p


def place_no_replace(src, dst):
    """Move src to dst without ever overwriting an existing dst."""
    try:
        os.link(src, dst)              # fails if dst exists: no race
        os.unlink(src)
        return
    except FileExistsError:
        raise FileError("'%s' already exists" % os.path.basename(dst))
    except OSError:
        pass                           # e.g. FAT filesystems or directories: best effort
    if os.path.lexists(dst):
        raise FileError("'%s' already exists" % os.path.basename(dst))
    os.rename(src, dst)


def rename(path, new_name):
    path = clean_path(path)
    dst = os.path.join(os.path.dirname(path), clean_name(new_name))
    try:
        place_no_replace(path, dst)
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
    return p, os.path.join(d, ".zerodesk-upload-" + secrets.token_hex(8))
