"""A real terminal (PTY) running your login shell as your user."""

import fcntl
import os
import pwd
import signal
import struct
import subprocess
import termios


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

    def resize(self, cols, rows):
        cols = max(10, min(int(cols), 500))
        rows = max(4, min(int(rows), 200))
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.master, view)
                view = view[n:]
            except BlockingIOError:
                import select
                select.select([], [self.master], [], 1.0)

    def read(self):
        """Non-blocking read; b'' means the shell has exited."""
        try:
            return os.read(self.master, 65536)
        except BlockingIOError:
            return None
        except OSError:
            return b""

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            os.close(self.master)
        except OSError:
            pass
