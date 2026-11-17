"""Running admin commands (installing apps, joining Wi-Fi) and tracking them as
background jobs the browser can follow.

The admin password is typed in the browser, sent over HTTPS, kept in memory
for a few minutes per session, and written to sudo's stdin (`sudo -S`), which
is then closed. It is never on a command line, in an environment variable or
on disk, so neither other programs nor the commands sudo runs can read it.
"""

import os
import secrets
import subprocess
import threading
import time

SUDO = "/usr/bin/sudo"
PASSWORD_TTL = 5 * 60
MAX_LOG = 256 * 1024
SUDO_ERROR = "Your user can't use sudo, so this needs to be done another way."


class NeedPassword(Exception):
    def __init__(self, wrong=False, wait=0):
        super().__init__("Wrong password" if wrong else "Password needed")
        self.wrong = wrong
        self.wait = wait


class AdminError(Exception):
    pass


def valid_password(pw):
    return isinstance(pw, str) and 0 < len(pw) <= 256 and not any(c in pw for c in "\x00\r\n")


class Passwords:
    """Per-session admin passwords, remembered for a few minutes, plus a
    per-session back-off after wrong guesses."""

    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._pw = {}
        self._fails = {}        # token -> (count, not_before)

    def set(self, token, password):
        with self._lock:
            self._pw[token] = (password, self._clock())
            self._fails.pop(token, None)

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

    def wait(self, token):
        """Seconds this session must wait before trying another password."""
        with self._lock:
            n, until = self._fails.get(token, (0, 0))
            return max(0, int(until - self._clock() + 0.999))

    def failed(self, token):
        with self._lock:
            self._pw.pop(token, None)
            n, _ = self._fails.get(token, (0, 0))
            n += 1
            self._fails[token] = (n, self._clock() + (0 if n == 1 else min(60, 2 ** (n - 1))))


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
        raise AdminError(SUDO_ERROR)
    return True


_check_lock = threading.Lock()


def check_password(password):
    """Raise NeedPassword unless admin actions can run (with this password).
    One check at a time, so guesses can't be run in parallel."""
    if not needs_password():
        return
    if not password:
        raise NeedPassword()
    if not valid_password(password):
        raise NeedPassword(wrong=True)
    with _check_lock:
        r = subprocess.run([SUDO, "-S", "-k", "-p", "", "true"], input=password + "\n",
                           capture_output=True, text=True, env=_clean_env(), timeout=30)
    if r.returncode != 0:
        raise NeedPassword(wrong=True)


def sudo_popen(argv, password):
    """Start argv as root. The caller has already checked the password.
    Stdin is closed right after the password, so the command sees EOF."""
    env = _clean_env()
    if not needs_password():
        return subprocess.Popen([SUDO, "-n"] + argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    if not password:
        raise NeedPassword()
    if not valid_password(password):
        raise NeedPassword(wrong=True)
    proc = subprocess.Popen([SUDO, "-S", "-k", "-p", ""] + argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.PIPE, env=env,
                            start_new_session=True)
    try:
        proc.stdin.write((password + "\n").encode())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    return proc


def sudo_run(job, argv, password, timeout=120):
    """Run argv as root, copy its output into the job log, return the exit code."""
    proc = sudo_popen(argv, password)
    try:
        out = proc.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        out = proc.communicate()[0]
        job.add(out.decode("utf-8", "replace"))
        raise AdminError("Timed out")
    job.add(out.decode("utf-8", "replace"))
    return proc.returncode


class Job:
    def __init__(self, title, owner=None):
        self.id = secrets.token_hex(8)
        self.owner = owner
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

    def get(self, job_id, owner=None):
        job = self.jobs.get(job_id)
        if job is None or (job.owner is not None and not secrets.compare_digest(job.owner, owner or "")):
            return None
        return job

    def start(self, title, steps, owner=None):
        """steps: callable(job) run in a thread. One admin job at a time."""
        with self._lock:
            if self.busy:
                raise AdminError("Another install or change is still running.")
            self.busy = True
            job = Job(title, owner)
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
            except Exception as e:  # never leave a job "running" forever
                job.add("\nSomething went wrong: %s\n" % (str(e) or type(e).__name__))
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
