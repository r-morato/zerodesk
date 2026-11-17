"""Read the Cardputer's screen (framebuffer) and type into it (uinput)."""

import fcntl
import io
import mmap
import os
import time
import struct
import zlib

from PIL import Image

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
_FIX_FMT = "16sL" + "I" * 6 + "HHHI"
UINPUT_NAME = "zerodesk-remote-keyboard"


class Framebuffer:
    def __init__(self, device=None):
        device = device or os.environ.get("APPLAUNCH_LINUX_FBDEV_DEVICE", "/dev/fb0")
        self.fd = os.open(device, os.O_RDONLY)
        try:
            var = bytearray(160)
            fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, var)
            self.width, self.height, _, _, _, self.yoffset, self.bpp = struct.unpack_from("7I", var, 0)
            fix = bytearray(struct.calcsize(_FIX_FMT))
            fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, fix)
            fields = struct.unpack(_FIX_FMT, fix)
            self.smem_len, self.line_length = fields[2], fields[8]
            if self.bpp not in (16, 32):
                raise OSError("Unsupported framebuffer depth %d" % self.bpp)
            self.mm = mmap.mmap(self.fd, self.smem_len, mmap.MAP_SHARED, mmap.PROT_READ)
        except Exception:
            os.close(self.fd)
            raise
        self._last_crc = None

    def raw(self):
        start = self.yoffset * self.line_length
        return self.mm[start:start + self.line_length * self.height]

    def image(self, raw=None):
        raw = raw if raw is not None else self.raw()
        mode = "BGR;16" if self.bpp == 16 else "BGRX"
        return Image.frombuffer("RGB", (self.width, self.height), raw, "raw", mode,
                                self.line_length, 1)

    def changed_png(self):
        """PNG bytes if the screen changed since last call, else None."""
        raw = self.raw()
        crc = zlib.crc32(raw)
        if crc == self._last_crc:
            return None
        self._last_crc = crc
        return to_png(self.image(raw))

    def force_next(self):
        self._last_crc = None

    def close(self):
        try:
            self.mm.close()
        finally:
            os.close(self.fd)


def to_png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=3)
    return buf.getvalue()


# Browser KeyboardEvent.code -> Linux key name. Anything not listed is ignored.
BROWSER_KEYS = {
    "Escape": "KEY_ESC", "Tab": "KEY_TAB", "Enter": "KEY_ENTER", "NumpadEnter": "KEY_ENTER",
    "Backspace": "KEY_BACKSPACE", "Delete": "KEY_DELETE", "Space": "KEY_SPACE",
    "ArrowUp": "KEY_UP", "ArrowDown": "KEY_DOWN", "ArrowLeft": "KEY_LEFT", "ArrowRight": "KEY_RIGHT",
    "Home": "KEY_HOME", "End": "KEY_END", "PageUp": "KEY_PAGEUP", "PageDown": "KEY_PAGEDOWN",
    "Insert": "KEY_INSERT", "PrintScreen": "KEY_SYSRQ",
    "ShiftLeft": "KEY_LEFTSHIFT", "ShiftRight": "KEY_RIGHTSHIFT",
    "ControlLeft": "KEY_LEFTCTRL", "ControlRight": "KEY_RIGHTCTRL",
    "AltLeft": "KEY_LEFTALT", "AltRight": "KEY_RIGHTALT",
    "Minus": "KEY_MINUS", "Equal": "KEY_EQUAL", "BracketLeft": "KEY_LEFTBRACE",
    "BracketRight": "KEY_RIGHTBRACE", "Semicolon": "KEY_SEMICOLON", "Quote": "KEY_APOSTROPHE",
    "Backquote": "KEY_GRAVE", "Backslash": "KEY_BACKSLASH", "Comma": "KEY_COMMA",
    "Period": "KEY_DOT", "Slash": "KEY_SLASH",
}
BROWSER_KEYS.update({"Key" + c: "KEY_" + c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})
BROWSER_KEYS.update({"Digit" + d: "KEY_" + d for d in "0123456789"})
BROWSER_KEYS.update({"F%d" % i: "KEY_F%d" % i for i in range(1, 13)})


_PLAIN = {" ": "Space", "\n": "Enter", "\t": "Tab", "-": "Minus", "=": "Equal", "[": "BracketLeft",
          "]": "BracketRight", ";": "Semicolon", "'": "Quote", "`": "Backquote", "\\": "Backslash",
          ",": "Comma", ".": "Period", "/": "Slash"}
_SHIFTED = {"_": "Minus", "+": "Equal", "{": "BracketLeft", "}": "BracketRight", ":": "Semicolon",
            '"': "Quote", "~": "Backquote", "|": "Backslash", "<": "Comma", ">": "Period", "?": "Slash"}
_SHIFTED.update(dict(zip("!@#$%^&*()", ["Digit" + d for d in "1234567890"])))
MAX_PASTE = 4000


def text_to_keys(text):
    """US-layout key strokes for text: list of (browser_code, shift)."""
    out = []
    for ch in text.replace("\r\n", "\n"):
        if "a" <= ch <= "z":
            out.append(("Key" + ch.upper(), False))
        elif "A" <= ch <= "Z":
            out.append(("Key" + ch, True))
        elif "0" <= ch <= "9":
            out.append(("Digit" + ch, False))
        elif ch in _PLAIN:
            out.append((_PLAIN[ch], False))
        elif ch in _SHIFTED:
            out.append((_SHIFTED[ch], True))
        # anything else (emoji, accents) has no key on the Cardputer: skipped
    return out


class RemoteKeyboard:
    """A virtual keyboard. Needs write access to /dev/uinput (the package's
    udev rule gives that to the `input` group)."""

    def __init__(self):
        from evdev import UInput, ecodes
        self.e = ecodes
        self.codes = {name: getattr(ecodes, name) for name in set(BROWSER_KEYS.values())}
        self.ui = UInput({ecodes.EV_KEY: sorted(self.codes.values())}, name=UINPUT_NAME)
        self.down = set()

    MODS_CTRL = ("KEY_LEFTCTRL", "KEY_RIGHTCTRL")
    MODS_ALT = ("KEY_LEFTALT", "KEY_RIGHTALT")
    # Alt+SysRq is the kernel's magic key (reboot and the like), so never with Alt
    ALT_BLOCKED = {"KEY_LEFT", "KEY_RIGHT", "KEY_SYSRQ"} | {"KEY_F%d" % i for i in range(1, 13)}

    def _held(self, names):
        return any(self.codes.get(n) in self.down for n in names)

    def blocked(self, name):
        """Combinations that act on the whole system rather than the app:
        Ctrl+Alt+Del (reboot) and Alt/Ctrl+Alt + F-keys or arrows (switch to
        another console), and Alt+SysRq (the kernel's magic key)."""
        if not self._held(self.MODS_ALT):
            return False
        if name == "KEY_DELETE" and self._held(self.MODS_CTRL):
            return True
        return name in self.ALT_BLOCKED

    def key(self, browser_code, pressed):
        name = BROWSER_KEYS.get(browser_code)
        if not name:
            return False
        code = self.codes[name]
        if pressed and self.blocked(name):
            return False
        if pressed:
            self.down.add(code)
        else:
            self.down.discard(code)
        self.ui.write(self.e.EV_KEY, code, 1 if pressed else 0)
        self.ui.syn()
        return True

    def tap(self, browser_code, shift=False):
        if shift:
            self.key("ShiftLeft", True)
        self.key(browser_code, True)
        time.sleep(0.004)           # some apps poll the keyboard and miss instant taps
        self.key(browser_code, False)
        if shift:
            self.key("ShiftLeft", False)

    def release_all(self):
        for code in list(self.down):
            self.ui.write(self.e.EV_KEY, code, 0)
        self.down.clear()
        self.ui.syn()

    def close(self):
        try:
            self.release_all()
        finally:
            self.ui.close()
