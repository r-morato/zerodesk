"""Read the Cardputer's screen (framebuffer) and type into it."""

import fcntl
import re
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
    "Insert": "KEY_INSERT",
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


# The Cardputer's own keyboard, and the file listing the codes it sends for
# symbols (it doesn't use the standard codes: "," is 90, "!" is 26, ...).
KEYBOARD_DEVICE = "/dev/input/by-path/platform-3f804000.i2c-event"
KEYMAP_FILE = "/usr/share/keymaps/tca8418_keypad_m5stack_keymap.map"
SYMBOL_NAMES = {
    "exclam": "!", "at": "@", "numbersign": "#", "dollar": "$", "percent": "%", "asciicircum": "^",
    "ampersand": "&", "asterisk": "*", "parenleft": "(", "parenright": ")", "asciitilde": "~",
    "grave": "`", "underscore": "_", "minus": "-", "plus": "+", "equal": "=", "bracketleft": "[",
    "bracketright": "]", "braceleft": "{", "braceright": "}", "semicolon": ";", "colon": ":",
    "apostrophe": "'", "quotedbl": '"', "less": "<", "greater": ">", "backslash": "\\", "bar": "|",
    "comma": ",", "period": ".", "slash": "/", "question": "?",
}
# what the M5Stack keyboard library uses when the keymap file is missing
DEFAULT_SYMBOL_CODES = {
    "!": 26, "@": 27, "#": 39, "$": 40, "%": 41, "^": 43, "&": 51, "*": 52, "(": 53, ")": 94,
    "~": 55, "`": 69, "_": 70, "-": 71, "+": 72, "=": 73, "[": 74, "]": 75, "{": 76, "}": 77,
    ";": 79, ":": 80, "'": 81, '"': 82, "<": 83, ">": 85, "\\": 86, "|": 89, ",": 90, ".": 91,
    "/": 92, "?": 93,
}


def load_symbol_codes(path=KEYMAP_FILE):
    """{character: keycode} from the Cardputer's keymap file
    (lines like `keycode 90 = comma`)."""
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                m = re.match(r"\s*keycode\s+(\d+)\s*=\s*(\S+)", line.split("#")[0])
                if m and m.group(2) in SYMBOL_NAMES:
                    out[SYMBOL_NAMES[m.group(2)]] = int(m.group(1))
    except OSError:
        pass
    return out or dict(DEFAULT_SYMBOL_CODES)


def _char_of():
    """(browser_code, shift held) -> the character that key types."""
    out = {}
    for ch, code in _PLAIN.items():
        out[(code, False)] = ch
    for ch, code in _SHIFTED.items():
        out[(code, True)] = ch
    return out


class RemoteKeyboard:
    """Types into the Cardputer the way its own keyboard does.

    Keys are written into the Cardputer's keyboard device itself, so every app
    sees them exactly as real key presses: the built-in apps only listen to
    that one device. Symbols use the Cardputer's own codes. Keys its keyboard
    can't produce go to a separate virtual keyboard (uinput) instead, as does
    everything if the keyboard device can't be opened. Each key goes one way
    only, so no app sees it twice.

    Needs the `input` group (both devices); the package's udev rule gives the
    group access to /dev/uinput. The virtual keyboard has no SysRq key: a
    keyboard with Alt and SysRq is watched by the kernel's magic-SysRq handler,
    and remote typing shouldn't come near that."""

    SHIFTS = ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT")

    def __init__(self, device_path=None, keymap=KEYMAP_FILE):
        from evdev import UInput, ecodes
        self.e = ecodes
        self.codes = {name: getattr(ecodes, name) for name in set(BROWSER_KEYS.values())}
        self.ui = UInput({ecodes.EV_KEY: sorted(self.codes.values())}, name=UINPUT_NAME)
        self.down = set()           # keys logically held (for the blocked combos)
        self.active = {}            # browser code -> (device, code sent, sent as a symbol)
        self.suspended_shift = False
        self.native, self.native_keys, self.symbols = None, set(), {}
        path = device_path or os.environ.get("LV_LINUX_KEYBOARD_DEVICE") or KEYBOARD_DEVICE
        dev = self._open_native(path)
        if dev is not None:
            self.native = dev
            self.native_keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
            self.symbols = {ch: c for ch, c in load_symbol_codes(keymap).items() if c in self.native_keys}
        self.char_of = _char_of()

    @staticmethod
    def _open_native(path):
        """The Cardputer's keyboard, opened for writing, or None."""
        from evdev import InputDevice
        try:
            dev = InputDevice(path)
        except OSError:
            return None
        if fcntl.fcntl(dev.fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR:
            dev.close()             # read-only: can't type into it
            return None
        return dev

    def virtual_only(self):
        """Browser keys the Cardputer's keyboard can't produce: those reach
        apps that read every keyboard, but not the built-in apps."""
        if not self.native:
            return sorted(BROWSER_KEYS)
        out = []
        for b, name in BROWSER_KEYS.items():
            chars = [self.char_of.get((b, sh)) for sh in (False, True)]
            if self.codes[name] in self.native_keys or any(c in self.symbols for c in chars if c):
                continue
            out.append(b)
        return sorted(out)

    @property
    def mode(self):
        return "keyboard" if self.native else "virtual"

    MODS_CTRL = ("KEY_LEFTCTRL", "KEY_RIGHTCTRL")
    MODS_ALT = ("KEY_LEFTALT", "KEY_RIGHTALT")
    ALT_BLOCKED = {"KEY_LEFT", "KEY_RIGHT"} | {"KEY_F%d" % i for i in range(1, 13)}

    def _held(self, names):
        return any(self.codes.get(n) in self.down for n in names)

    def blocked(self, name):
        """Combinations that act on the whole system rather than the app:
        Ctrl+Alt+Del (reboot) and Alt/Ctrl+Alt + F-keys or arrows (switch to
        another console)."""
        if not self._held(self.MODS_ALT):
            return False
        if name == "KEY_DELETE" and self._held(self.MODS_CTRL):
            return True
        return name in self.ALT_BLOCKED

    def _send(self, dev, code, value):
        dev.write(self.e.EV_KEY, code, value)
        dev.syn()

    def _shift_keys(self):
        return [b for b in self.active if BROWSER_KEYS.get(b) in self.SHIFTS]

    def key(self, browser_code, pressed):
        name = BROWSER_KEYS.get(browser_code)
        if not name:
            return False
        code = self.codes[name]
        if not pressed:
            dev, sent, symbol = self.active.pop(browser_code, (self.ui, code, False))
            self.down.discard(code)
            self._send(dev, sent, 0)
            if symbol and self.suspended_shift:
                # Shift was lifted for the symbol; put it back if it's still held
                for b in self._shift_keys():
                    self._send(self.active[b][0], self.active[b][1], 1)
                self.suspended_shift = False
            return True
        if self.blocked(name):
            return False
        if browser_code in self.active:                     # already down
            return True
        ch = self.char_of.get((browser_code, self._held(self.SHIFTS)))
        symbol = bool(self.native) and ch in self.symbols
        if symbol:
            # The Cardputer sends one code per symbol, with no Shift
            for b in self._shift_keys():
                self._send(self.active[b][0], self.active[b][1], 0)
                self.suspended_shift = True
            dev, sent = self.native, self.symbols[ch]
        elif self.native and code in self.native_keys:
            dev, sent = self.native, code
        else:
            dev, sent = self.ui, code
        self.down.add(code)
        self.active[browser_code] = (dev, sent, symbol)
        self._send(dev, sent, 1)
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
        for dev, sent, _ in list(self.active.values()):
            try:
                self._send(dev, sent, 0)
            except OSError:
                pass
        self.active.clear()
        self.down.clear()
        self.suspended_shift = False

    def close(self):
        try:
            self.release_all()
        finally:
            self.ui.close()
            if self.native:
                self.native.close()
