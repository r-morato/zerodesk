"""What the Cardputer itself shows: where to connect and the pairing code."""

import os
import select
import threading
import time

from PIL import Image, ImageDraw, ImageFont

from .display import HEIGHT, WIDTH
from .screen import UINPUT_NAME

BG = (15, 42, 37)        # solder-mask green, same as the web interface
PANEL = (21, 54, 48)
LINE = (42, 90, 80)
TEXT = (238, 243, 236)
DIM = (163, 191, 181)
ACCENT = (232, 181, 77)  # gold
GREEN = (123, 224, 165)
AMBER = (243, 154, 75)
RED = (255, 122, 107)
KEY_X = (49, 105, 161, 217, 273)    # centred over keys 4..8

FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF",
             "/usr/share/fonts/truetype/jetbrains-mono")

# Linux key codes
KEY_ESC, KEY_4, KEY_6, KEY_8, KEY_F, KEY_X_, KEY_UP, KEY_DOWN = 1, 5, 7, 9, 33, 45, 103, 108


def _font(names, size):
    for d in FONT_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _w(d, text, font):
    b = d.textbbox((0, 0), text, font=font)
    return b[2] - b[0]


class DeviceScreen:
    def __init__(self, guard, runner, addresses, port, fingerprint, display, keys):
        self.guard = guard
        self.runner = runner
        self.addresses = addresses        # callable -> [(iface, ip)]
        self.port = port
        self.fingerprint = fingerprint
        self.display = display
        self.keys = keys
        self.running = True
        self.addr_index = 0
        self.message = None
        self.f_title = _font(["DejaVuSans-Bold.ttf"], 13)
        self.f_body = _font(["DejaVuSans.ttf"], 12)
        self.f_url = _font(["DejaVuSansMono-Bold.ttf", "DejaVuSans-Bold.ttf"], 14)
        self.f_code = _font(["DejaVuSansMono-Bold.ttf", "DejaVuSans-Bold.ttf"], 30)
        self.f_small = _font(["DejaVuSans.ttf"], 11)
        self.viewers = lambda: 0
        self.show_fp = False

    def render_fingerprint(self):
        img = Image.new("RGB", (WIDTH, HEIGHT), BG)
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, WIDTH, 20), fill=PANEL)
        d.text((8, 3), "Certificate fingerprint (SHA-256)", font=self.f_title, fill=TEXT)
        parts = self.fingerprint.split(":")
        mono = self.f_url
        for i in range(4):
            d.text((10, 28 + i * 20), ":".join(parts[i * 8:(i + 1) * 8]), font=mono, fill=ACCENT)
        d.text((10, 112), "Compare with your browser's certificate details", font=self.f_small, fill=DIM)
        d.text((10, 126), "before typing the code on a network you don't trust.", font=self.f_small, fill=DIM)
        d.line((0, 142, WIDTH, 142), fill=LINE)
        d.text((10, 150), "8 or Esc: back", font=self.f_small, fill=DIM)
        return img

    def render(self):
        if self.show_fp:
            return self.render_fingerprint()
        img = Image.new("RGB", (WIDTH, HEIGHT), BG)
        d = ImageDraw.Draw(img)
        n = self.guard.count()
        d.rectangle((0, 0, WIDTH, 20), fill=PANEL)
        d.text((8, 3), "ZeroDesk", font=self.f_title, fill=TEXT)
        if n:
            label = "%d connected" % n
            if self.viewers():
                label += " · watching"
            col = AMBER
        else:
            label, col = "No one connected", DIM
        lw = _w(d, label, self.f_body)
        d.ellipse((WIDTH - 18 - lw, 7, WIDTH - 12 - lw, 13), fill=col)
        d.text((WIDTH - 8 - lw, 3), label, font=self.f_body, fill=col)

        addrs = self.addresses()
        d.text((10, 26), "Open in a browser on the same network:", font=self.f_body, fill=DIM)
        if addrs:
            iface, ip = addrs[self.addr_index % len(addrs)]
            url = "https://%s:%d" % (ip, self.port)
            d.text((10, 42), url, font=self.f_url, fill=ACCENT)
            if len(addrs) > 1:
                d.text((WIDTH - 10 - _w(d, iface + " F/X", self.f_small), 45), iface + " F/X",
                       font=self.f_small, fill=DIM)
        else:
            d.text((10, 42), "Not connected to a network", font=self.f_url, fill=RED)

        d.text((10, 66), "Pairing code", font=self.f_body, fill=DIM)
        wait = self.guard.lockout_left()
        if self.guard.stopped:
            d.text((10, 84), "Paused", font=self.f_code, fill=RED)
            d.text((120, 86), "Too many wrong codes.", font=self.f_small, fill=RED)
            d.text((120, 100), "Press 4 to start again.", font=self.f_small, fill=TEXT)
        elif wait > 0:
            d.text((10, 82), "Wait %ds" % (wait + 1), font=self.f_code, fill=AMBER)
        else:
            code = self.guard.code
            d.text((10, 80), code[:3] + " " + code[3:], font=self.f_code, fill=TEXT)
        # who's here: shown so an unexpected browser stands out
        info = None
        if self.guard.last_bad and not self.guard.stopped:
            info = ("Wrong code from " + self.guard.last_bad, AMBER)
        remotes = self.guard.remotes()
        if remotes:
            info = ("Paired: " + ", ".join(remotes[:2]) + ("…" if len(remotes) > 2 else ""), DIM)
        if self.message and time.monotonic() < self.message[2]:
            info = self.message[:2]
        if info:
            d.text((10, 120), info[0], font=self.f_small, fill=info[1])
        fp = self.fingerprint.replace(":", "")[-16:] if self.fingerprint else ""
        if fp and wait <= 0 and not self.guard.stopped:
            t = ":".join(fp[i:i + 4] for i in range(0, 16, 4))
            d.text((WIDTH - 10 - _w(d, t, self.f_small), 72), t, font=self.f_small, fill=DIM)
            lab = "certificate"
            d.text((WIDTH - 10 - _w(d, lab, self.f_small), 86), lab, font=self.f_small, fill=DIM)

        d.line((0, 136, WIDTH, 136), fill=LINE)
        for label, x in (("New code", KEY_X[0]), ("Sign out all", KEY_X[2]), ("Certificate", KEY_X[4])):
            w = _w(d, label, self.f_small)
            d.text((x - w // 2, 141), label, font=self.f_small, fill=TEXT)
            d.line((x, 156, x, HEIGHT - 1), fill=DIM)
        q = "Esc: quit"
        d.text((KEY_X[0] + 28, 154), q, font=self.f_small, fill=DIM)
        return img

    def flash(self, text, color):
        self.message = (text, color, time.monotonic() + 3)

    def handle(self, code):
        if self.show_fp:
            if code in (KEY_8, KEY_ESC):
                self.show_fp = False
            return
        if code == KEY_8:
            self.show_fp = True
        elif code == KEY_ESC:
            self.running = False
        elif code == KEY_4:
            self.guard.new_code()
            self.flash("New pairing code", GREEN)
        elif code == KEY_6:
            self.guard.revoke_all()
            self.flash("Everyone signed out", GREEN)
        elif code in (KEY_F, KEY_UP):
            self.addr_index -= 1
        elif code in (KEY_X_, KEY_DOWN):
            self.addr_index += 1

    def loop(self, on_quit):
        last_draw = 0
        app_was_running = False
        while self.running:
            app = self.runner.running()
            if app:
                app_was_running = True
                self.keys.drain()          # the app owns the keyboard now
                time.sleep(0.3)
                continue
            if app_was_running:
                app_was_running = False
                self.keys.drain()
                last_draw = 0
            self.guard.tick()
            for code in self.keys.poll(0.5):
                self.handle(code)
                last_draw = 0
            changed = self.guard.changed.is_set()
            if changed or time.monotonic() - last_draw > 1.0:
                self.guard.changed.clear()
                if not self.runner.running():
                    self.display.show(self.render())
                last_draw = time.monotonic()
        on_quit()


class PhysicalKeys:
    """Key presses from the Cardputer's own keyboard (never the remote one)."""

    def __init__(self):
        from evdev import InputDevice, ecodes, list_devices
        self.ec = ecodes
        self.devices = []
        for path in list_devices():
            try:
                dev = InputDevice(path)
            except OSError:
                continue
            if dev.name == UINPUT_NAME:
                dev.close()
                continue
            caps = dev.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_ESC in caps or ecodes.KEY_ENTER in caps:
                self.devices.append(dev)
            else:
                dev.close()

    def poll(self, timeout):
        if not self.devices:
            time.sleep(timeout)
            return []
        r, _, _ = select.select([d.fd for d in self.devices], [], [], timeout)
        out = []
        for d in list(self.devices):
            if d.fd not in r:
                continue
            try:
                for ev in d.read():
                    if ev.type == self.ec.EV_KEY and ev.value == 1:
                        out.append(ev.code)
            except BlockingIOError:
                pass
            except OSError:
                self.devices.remove(d)
        return out

    def drain(self):
        for d in self.devices:
            try:
                while d.read_one():
                    pass
            except OSError:
                pass

    def close(self):
        for d in self.devices:
            try:
                d.close()
            except OSError:
                pass


def run_in_thread(screen, on_quit):
    t = threading.Thread(target=screen.loop, args=(on_quit,), daemon=True)
    t.start()
    return t
