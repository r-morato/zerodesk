"""Running admin commands (installing apps, joining Wi-Fi) and tracking them as
background jobs the browser can follow.

The admin password is typed in the browser, sent over HTTPS, kept in memory
for a few minutes per session, and handed to sudo through a tiny askpass
program via the environment: never on a command line, never on disk.
"""

import os
import secrets
import subprocess
import threading
import time

SUDO = "/usr/bin/sudo"
ASKPASS = os.environ.get("ZERODESK_ASKPASS", "/usr/lib/zerodesk/zerodesk-askpass")
PASSWORD_ENV = "ZERODESK_SUDO_PASSWORD"
PASSWORD_TTL = 5 * 60
MAX_LOG = 256 * 1024


class NeedPassword(Exception):
    def __init__(self, wrong=False):
        super().__init__("Wrong password" if wrong else "Password needed")
        self.wrong = wrong


class AdminError(Exception):
    pass


class Passwords:
    """Per-session admin passwords, remembered for a few minutes."""

    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._pw = {}

    def set(self, token, password):
        with self._lock:
            self._pw[token] = (password, self._clock())

    def get(self, token):
        with self._lock:
            item = self._pw.get(token)
            if not item:
                return None
            if self._clock() - item[1] > PASSWORD_TTL:
                del self._pw[token]
                return None
            return item[0]

    def forget(self, token):
        with self._lock:
            self._pw.pop(token, None)


def _clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZERODESK_")}
    env["LC_ALL"] = "C"
    return env


def needs_password():
    r = subprocess.run([SUDO, "-n", "true"], capture_output=True, text=True,
                       env=_clean_env(), stdin=subprocess.DEVNULL, timeout=15)
    if r.returncode == 0:
        return False
    if "password is required" not in (r.stderr or "").lower():
        raise AdminError("Your user can't use sudo, so this needs to be done another way.")
    return True


def check_password(password):
    """Raise NeedPassword unless admin actions can run (with this password)."""
    if not needs_password():
        return
    if not password:
        raise NeedPassword()
    r = subprocess.run([SUDO, "-A", "-k", "true"], capture_output=True, text=True,
                       env=dict(_clean_env(), SUDO_ASKPASS=ASKPASS, **{PASSWORD_ENV: password}),
                       stdin=subprocess.DEVNULL, timeout=30)
    if r.returncode != 0:
        raise NeedPassword(wrong=True)


def sudo_popen(argv, password):
    """Start argv as root. Tries without a password first; raises NeedPassword
    if one is required and none (or a wrong one) was given."""
    probe = subprocess.run([SUDO, "-n", "true"], capture_output=True, text=True,
                           env=_clean_env(), stdin=subprocess.DEVNULL, timeout=15)
    env = _clean_env()
    if probe.returncode == 0:
        cmd = [SUDO, "-n"] + argv
    else:
        low = (probe.stderr or "").lower()
        if "password is required" not in low:
            raise AdminError("Your user can't use sudo, so this needs to be done another way.")
        if not password:
            raise NeedPassword()
        check = subprocess.run([SUDO, "-A", "-k", "true"], capture_output=True, text=True,
                               env=dict(env, SUDO_ASKPASS=ASKPASS, **{PASSWORD_ENV: password}),
                               stdin=subprocess.DEVNULL, timeout=30)
        if check.returncode != 0:
            raise NeedPassword(wrong=True)
        cmd = [SUDO, "-A", "-k"] + argv
        env = dict(env, SUDO_ASKPASS=ASKPASS, **{PASSWORD_ENV: password})
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env, start_new_session=True)


class Job:
    def __init__(self, title):
        self.id = secrets.token_hex(6)
        self.title = title
        self.log = ""
        self.state = "running"      # running | done | failed
        self.started = time.time()

    def add(self, text):
        self.log = (self.log + text)[-MAX_LOG:]

    def view(self):
        return {"id": self.id, "title": self.title, "state": self.state, "log": self.log}


class Jobs:
    def __init__(self):
        self._lock = threading.Lock()
        self.jobs = {}
        self.busy = False

    def get(self, job_id):
        return self.jobs.get(job_id)

    def start(self, title, steps):
        """steps: callable(job) run in a thread. One admin job at a time."""
        with self._lock:
            if self.busy:
                raise AdminError("Another install or change is still running.")
            self.busy = True
            job = Job(title)
            self.jobs[job.id] = job
            for old in sorted(self.jobs.values(), key=lambda j: j.started)[:-10]:
                self.jobs.pop(old.id, None)

        def run():
            try:
                steps(job)
                if job.state == "running":
                    job.state = "done"
            except (AdminError, NeedPassword, OSError, subprocess.SubprocessError) as e:
                job.add("\n%s\n" % e)
                job.state = "failed"
            finally:
                with self._lock:
                    self.busy = False
        threading.Thread(target=run, daemon=True).start()
        return job


def stream(job, proc):
    """Copy a process's output into the job log; fail the job on a non-zero exit."""
    for line in iter(proc.stdout.readline, b""):
        job.add(line.decode("utf-8", "replace"))
    proc.wait()
    if proc.returncode != 0:
        job.add("\nExited with code %d\n" % proc.returncode)
        job.state = "failed"
