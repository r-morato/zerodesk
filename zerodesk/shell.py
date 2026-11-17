"""A real terminal (PTY) running your login shell as your user."""

import fcntl
import os
import pwd
import signal
import struct
import subprocess
import termios
import time

MAX_PENDING_INPUT = 1024 * 1024   # bytes queued for a program that isn't reading


class Shell:
    def __init__(self, cols=80, rows=24):
        self.master, slave = os.openpty()
        self.resize(cols, rows)
        user = pwd.getpwuid(os.getuid())
        shell = user.pw_shell if os.path.exists(user.pw_shell or "") else "/bin/sh"
        env = {k: v for k, v in os.environ.items() if not k.startswith("ZERODESK_")}
        env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "HOME": user.pw_dir,
                    "SHELL": shell, "USER": user.pw_name, "LOGNAME": user.pw_name})
        self.proc = subprocess.Popen(
            [shell, "-l"], stdin=slave, stdout=slave, stderr=slave, cwd=user.pw_dir, env=env,
            start_new_session=True, close_fds=True,
            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        os.close(slave)
        os.set_blocking(self.master, False)
        self.pending = bytearray()     # input waiting for the program to read it
        self.closed = False

    def resize(self, cols, rows):
        cols = max(10, min(int(cols), 500))
        rows = max(4, min(int(rows), 200))
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def queue_input(self, data):
        """Buffer input; returns False if too much is already waiting."""
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        if len(self.pending) + len(data) > MAX_PENDING_INPUT:
            return False
        self.pending += data
        return True

    def flush(self):
        """Write as much pending input as the PTY accepts right now (never blocks).
        Returns True when everything has been written."""
        while self.pending and not self.closed:
            try:
                n = os.write(self.master, self.pending)
            except BlockingIOError:
                return False
            except OSError:
                self.pending.clear()
                return True
            del self.pending[:n]
        return True

    # kept for simple callers and tests
    def write(self, data):
        self.queue_input(data)
        self.flush()

    def read(self):
        """Non-blocking read; b'' means the shell has exited, None means no data yet."""
        try:
            return os.read(self.master, 65536)
        except BlockingIOError:
            return None
        except OSError:
            return b""

    def alive(self):
        return self.proc.poll() is None

    def _session_pids(self):
        """Every process in the shell's session, including jobs it started in
        their own process groups."""
        sid = self.proc.pid
        pids = []
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % d) as fh:
                    stat = fh.read()
                fields = stat[stat.rindex(")") + 2:].split()
                if int(fields[3]) == sid:          # field 6: session id
                    pids.append(int(d))
            except (OSError, ValueError, IndexError):
                continue
        return pids

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.pending.clear()
        targets = self._session_pids() if self.proc.poll() is None else []
        try:
            os.close(self.master)          # the kernel hangs up the terminal
        except OSError:
            pass
        for sig in (signal.SIGHUP, signal.SIGTERM):
            for pid in targets:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and any(_alive(p) for p in targets):
            time.sleep(0.05)
        for pid in targets:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def _alive(pid):
    """True if pid is still running. A zombie (exited, not yet reaped by its
    parent) counts as dead: it can't run anything any more."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        with open("/proc/%d/stat" % pid) as fh:
            stat = fh.read()
        return stat[stat.rindex(")") + 2:][:1] != "Z"
    except (OSError, ValueError):
        return False
