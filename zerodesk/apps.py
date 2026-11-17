"""List the launcher's apps and run one at a time on the Cardputer screen."""

import configparser
import os
import re
import shlex
import signal
import subprocess
import threading

APPLAUNCH = "/usr/share/APPLaunch"
SELF_ID = "zerodesk"


class AppError(Exception):
    pass


def _app_id(filename):
    return re.sub(r"\.desktop$", "", os.path.basename(filename))


def list_apps(root=APPLAUNCH):
    out = []
    d = os.path.join(root, "applications")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".desktop"):
            continue
        cp = configparser.RawConfigParser(strict=False, interpolation=None)
        try:
            cp.read(os.path.join(d, n), encoding="utf-8")
            entry = cp["Desktop Entry"]
        except (configparser.Error, KeyError, UnicodeDecodeError):
            continue
        if entry.get("Type", "Application") != "Application" or not entry.get("Exec"):
            continue
        if entry.get("NoDisplay", "").lower() == "true":
            continue
        app_id = _app_id(n)
        if app_id == SELF_ID:
            continue
        out.append({"id": app_id, "name": entry.get("Name", app_id),
                    "comment": entry.get("Comment", ""), "exec": entry.get("Exec"),
                    "icon": entry.get("Icon", ""), "terminal": entry.get("Terminal", "") == "true"})
    out.sort(key=lambda a: a["name"].lower())
    return out


def icon_path(app, root=APPLAUNCH):
    icon = app.get("icon") or ""
    if not icon:
        return None
    p = icon if os.path.isabs(icon) else os.path.join(root, icon)
    p = os.path.realpath(p)
    # Only serve images from inside the launcher's own folders.
    allowed = [os.path.realpath(root) + os.sep, "/usr/share/icons" + os.sep,
               "/usr/share/pixmaps" + os.sep]
    if not any(p.startswith(a) for a in allowed):
        return None
    if not p.lower().endswith((".png", ".jpg", ".jpeg", ".svg")) or not os.path.isfile(p):
        return None
    return p


def exec_argv(exec_line):
    parts = shlex.split(exec_line)
    # Drop desktop-entry field codes (%f, %U, ...)
    return [p for p in parts if not re.fullmatch(r"%[fFuUdDnNickvm]", p)]


class Runner:
    """Runs one launcher app at a time as a child, so ZeroDesk (and its web
    server) keeps running while the app owns the screen and keyboard.
    Starting another app swaps: the running one is stopped first."""

    def __init__(self, root=APPLAUNCH, on_change=None):
        self.root = root
        self.proc = None
        self.app = None
        self._lock = threading.Lock()
        self.on_change = on_change or (lambda **kw: None)

    def running(self):
        with self._lock:
            if self.proc and self.proc.poll() is not None:
                self.proc, self.app = None, None
                self.on_change()
            return self.app

    def start(self, app_id):
        """Start an app. If another app is running it's closed first;
        if this same app is already running it's left alone."""
        current = self.running()
        if current and current["id"] == app_id:
            return current
        if current:
            self.stop()
        with self._lock:
            if self.proc and self.proc.poll() is None:      # another start won the race
                raise AppError("%s is already running. Stop it first." % self.app["name"])
            app = next((a for a in list_apps(self.root) if a["id"] == app_id), None)
            if not app:
                raise AppError("App not found")
            try:
                argv = exec_argv(app["exec"])
            except ValueError:
                raise AppError("This app's launcher entry is broken")
            if not argv:
                raise AppError("App has no command")
            self.on_change(starting=True)
            try:
                self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             start_new_session=True, close_fds=True)
            except OSError as e:
                self.on_change()
                raise AppError("Couldn't start %s: %s" % (app["name"], e.strerror or e))
            self.app = app
            threading.Thread(target=self._wait, args=(self.proc,), daemon=True).start()
            return app

    def _wait(self, proc):
        proc.wait()
        with self._lock:
            if self.proc is proc:
                self.proc, self.app = None, None
        self.on_change()

    def stop(self):
        with self._lock:
            proc = self.proc
        if not proc or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=3)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
