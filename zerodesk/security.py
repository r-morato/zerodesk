"""Pairing codes, sessions and request checks.

Threat model: anyone on the same network can reach the port. Only someone who
can see the Cardputer's screen (the pairing code) can get a session. Sessions
live in memory only, so quitting the app logs everyone out.

Brute force: each code allows MAX_FAILURES wrong guesses, then pairing pauses
with growing delays, and after MAX_LOCKOUTS pauses it stops completely until
someone presses a key on the Cardputer. So a network attacker gets at most
MAX_FAILURES * MAX_LOCKOUTS guesses (1 in ~67,000 for a 6-digit code) for
each time the owner physically resets it, instead of unlimited time.
"""

import hmac
import ipaddress
import re
import secrets
import threading
import time

CODE_DIGITS = 6
CODE_LIFETIME = 5 * 60    # codes expire on their own, so a photo of the screen goes stale
MAX_FAILURES = 5          # wrong codes before pairing pauses
MAX_LOCKOUTS = 3          # pauses before pairing stops until a key is pressed on the device
LOCKOUT_SECONDS = 30      # first pause; doubles each time
IDLE_TIMEOUT = 15 * 60
ABSOLUTE_TIMEOUT = 12 * 60 * 60
MAX_SESSIONS = 4
COOKIE = "__Host-zd_session"   # __Host-: only this exact host, Secure, Path=/

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
    """Only IP literals, localhost or single-label mDNS names. Stops DNS
    rebinding, where another website points its own hostname at the
    Cardputer and tries to talk to it from your browser."""
    if not host or not _HOST_RE.match(host):
        return False
    if host.startswith("["):
        name = host[1:host.index("]")]
        try:
            return ipaddress.ip_address(name).version == 6
        except ValueError:
            return False
    name = host.rsplit(":", 1)[0]
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

    def __init__(self, token, remote, now):
        self.token, self.created, self.last_seen, self.remote = token, now, now, remote


class Guard:
    """Thread-safe: used by the web server and by the on-device screen."""

    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self.sessions = {}
        self.failures = 0
        self.lockouts = 0
        self.locked_until = 0.0
        self.stopped = False          # pairing off until a key is pressed on the device
        self.last_bad = None          # address of the last wrong guess, shown on screen
        self.last_paired = None       # address of the last browser that paired
        self.code = ""
        self.code_born = 0.0
        self._rotate()
        self.changed = threading.Event()   # tells the device screen to redraw

    def _rotate(self):
        self.code = "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))
        self.code_born = self._clock()

    def _bump(self):
        if hasattr(self, "changed"):
            self.changed.set()

    # -- pairing
    def new_code(self):
        """Physical key on the Cardputer: fresh code, and pairing re-enabled."""
        with self._lock:
            self._rotate()
            self.failures = 0
            self.lockouts = 0
            self.locked_until = 0.0
            self.stopped = False
            self.last_bad = None
            self._bump()

    def tick(self):
        """Expire an old code. Called regularly by the device screen."""
        with self._lock:
            if not self.stopped and self._clock() - self.code_born > CODE_LIFETIME:
                self._rotate()
                self._bump()

    def lockout_left(self):
        return max(0.0, self.locked_until - self._clock())

    def pair(self, code, remote):
        """Returns (token, None) or (None, error message)."""
        code = re.sub(r"\D", "", code or "")
        with self._lock:
            now = self._clock()
            if self.stopped:
                return None, "Pairing is paused. Press 4 on the Cardputer for a new code."
            if now < self.locked_until:
                return None, "Too many wrong codes. Try again in %d s." % (self.locked_until - now + 1)
            if now - self.code_born > CODE_LIFETIME:
                self._rotate()
                self._bump()
                return None, "That code expired. Use the new one on the Cardputer."
            if not hmac.compare_digest(code.encode(), self.code.encode()):
                self.failures += 1
                self.last_bad = remote
                self._bump()
                if self.failures >= MAX_FAILURES:
                    self.failures = 0
                    self.lockouts += 1
                    self._rotate()
                    if self.lockouts >= MAX_LOCKOUTS:
                        self.stopped = True
                        return None, "Pairing is paused. Press 4 on the Cardputer for a new code."
                    self.locked_until = now + LOCKOUT_SECONDS * 2 ** (self.lockouts - 1)
                    return None, "Too many wrong codes. A new code is on the Cardputer."
                return None, "Wrong code."
            self._expire(now)
            if len(self.sessions) >= MAX_SESSIONS:
                oldest = min(self.sessions.values(), key=lambda s: s.last_seen)
                del self.sessions[oldest.token]
            token = secrets.token_urlsafe(32)
            self.sessions[token] = Session(token, remote, now)
            self._rotate()                    # one-time: next device gets a new code
            self.failures = 0
            self.lockouts = 0
            self.last_paired = remote
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
            self._rotate()
            self.failures = 0
            self._bump()

    def count(self):
        with self._lock:
            self._expire(self._clock())
            return len(self.sessions)

    def remotes(self):
        with self._lock:
            return sorted({s.remote for s in self.sessions.values() if s.remote})
