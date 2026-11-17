"""Pairing codes, sessions and request checks.

Threat model: anyone on the same network can reach the port. Only someone who
can see the Cardputer's screen (the pairing code) can get a session. Sessions
live in memory only, so quitting the app logs everyone out.
"""

import hmac
import ipaddress
import re
import secrets
import threading
import time

CODE_DIGITS = 6
MAX_FAILURES = 5          # wrong codes before the code is replaced and pairing pauses
LOCKOUT_SECONDS = 30
IDLE_TIMEOUT = 15 * 60
ABSOLUTE_TIMEOUT = 12 * 60 * 60
MAX_SESSIONS = 4
COOKIE = "zd_session"

# Addresses that are "local": private LANs, link-local, loopback, carrier-grade
# NAT (used by Tailscale) and IPv6 unique-local.
LOCAL_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8",
    "100.64.0.0/10", "::1/128", "fe80::/10", "fc00::/7")]


def is_local_address(addr):
    try:
        ip = ipaddress.ip_address((addr or "").split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in LOCAL_NETS)


_HOST_RE = re.compile(r"^(\[[0-9a-fA-F:.]+\]|[0-9.]+|localhost|[A-Za-z0-9-]+\.local)(:\d{1,5})?$")


def host_ok(host):
    """Only IP literals, localhost or mDNS names. Stops DNS-rebinding attacks,
    where a web page on some other site points its own hostname at the
    Cardputer and tries to talk to it from your browser."""
    if not host or not _HOST_RE.match(host):
        return False
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0][1:]
    if name.replace(".", "").isdigit():
        try:
            ipaddress.ip_address(name)
        except ValueError:
            return False
    return True


def origin_ok(origin, host):
    """State-changing requests and websockets must come from our own page."""
    return bool(origin) and bool(host) and origin == "https://" + host


class Session:
    __slots__ = ("token", "created", "last_seen", "remote")

    def __init__(self, token, remote):
        now = time.monotonic()
        self.token, self.created, self.last_seen, self.remote = token, now, now, remote


class Guard:
    """Thread-safe: used by the web server and by the on-device screen."""

    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self.sessions = {}
        self.failures = 0
        self.locked_until = 0.0
        self.code = self._new_code()
        self.changed = threading.Event()   # tells the device screen to redraw

    @staticmethod
    def _new_code():
        return "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))

    def _bump(self):
        self.changed.set()

    # -- pairing
    def new_code(self):
        with self._lock:
            self.code = self._new_code()
            self.failures = 0
            self._bump()

    def lockout_left(self):
        return max(0.0, self.locked_until - self._clock())

    def pair(self, code, remote):
        """Returns (token, None) or (None, error message)."""
        code = re.sub(r"\D", "", code or "")
        with self._lock:
            now = self._clock()
            if now < self.locked_until:
                return None, "Too many wrong codes. Try again in %d s." % (self.locked_until - now + 1)
            if not hmac.compare_digest(code.encode(), self.code.encode()):
                self.failures += 1
                if self.failures >= MAX_FAILURES:
                    # Replace the code so guessing can't continue against it.
                    self.code = self._new_code()
                    self.failures = 0
                    self.locked_until = now + LOCKOUT_SECONDS
                    self._bump()
                    return None, "Too many wrong codes. A new code is on the Cardputer."
                return None, "Wrong code."
            self._expire(now)
            if len(self.sessions) >= MAX_SESSIONS:
                oldest = min(self.sessions.values(), key=lambda s: s.last_seen)
                del self.sessions[oldest.token]
            token = secrets.token_urlsafe(32)
            self.sessions[token] = Session(token, remote)
            self.code = self._new_code()      # one-time: next device gets a new code
            self.failures = 0
            self._bump()
            return token, None

    # -- sessions
    def _expire(self, now):
        dead = [t for t, s in self.sessions.items()
                if now - s.last_seen > IDLE_TIMEOUT or now - s.created > ABSOLUTE_TIMEOUT]
        for t in dead:
            del self.sessions[t]
        if dead:
            self._bump()

    def check(self, token, touch=True):
        if not token:
            return None
        with self._lock:
            now = self._clock()
            self._expire(now)
            for t, s in self.sessions.items():
                if hmac.compare_digest(t.encode(), token.encode()):
                    if touch:
                        s.last_seen = now
                    return s
        return None

    def logout(self, token):
        with self._lock:
            if self.sessions.pop(token, None):
                self._bump()

    def revoke_all(self):
        with self._lock:
            self.sessions.clear()
            self.code = self._new_code()
            self.failures = 0
            self._bump()

    def count(self):
        with self._lock:
            self._expire(self._clock())
            return len(self.sessions)
