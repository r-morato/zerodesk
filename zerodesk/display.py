"""Output to the CardputerZero's 320x170 RGB565 framebuffer (or PNGs in the simulator)."""

import fcntl
import mmap
import os
import struct

from PIL import Image, ImageChops

WIDTH, HEIGHT = 320, 170

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
_FIX_FMT = "16sL" + "I" * 6 + "HHHI"


def _lut(fn):
    return [fn(v) & 0xFF for v in range(256)]


_R_HI = _lut(lambda v: v & 0xF8)
_G_HI = _lut(lambda v: v >> 5)
_G_LO = _lut(lambda v: (v << 3) & 0xE0)
_B_LO = _lut(lambda v: v >> 3)


def to_rgb565(img):
    """RGB image -> little-endian RGB565 bytes, done in C via Pillow (no numpy)."""
    r, g, b = img.convert("RGB").split()
    hi = ImageChops.add(r.point(_R_HI), g.point(_G_HI))
    lo = ImageChops.add(g.point(_G_LO), b.point(_B_LO))
    return Image.merge("LA", (lo, hi)).tobytes()


class FramebufferDisplay:
    def __init__(self, device=None):
        device = device or os.environ.get("APPLAUNCH_LINUX_FBDEV_DEVICE", "/dev/fb0")
        self.fd = os.open(device, os.O_RDWR)
        try:
            var = bytearray(160)
            fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, var)
            self.xres, self.yres, _, _, _, _, self.bpp = struct.unpack_from("7I", var, 0)
            fix = bytearray(struct.calcsize(_FIX_FMT))
            fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, fix)
            fields = struct.unpack(_FIX_FMT, fix)
            self.smem_len, self.line_length = fields[2], fields[8]
            if self.bpp != 16:
                raise RuntimeError("Expected a 16 bpp framebuffer, got %d" % self.bpp)
            self.mm = mmap.mmap(self.fd, self.smem_len, mmap.MAP_SHARED,
                                mmap.PROT_READ | mmap.PROT_WRITE)
        except Exception:
            os.close(self.fd)
            raise

    def show(self, img):
        w, h = min(self.xres, img.width), min(self.yres, img.height)
        if (w, h) != img.size:
            img = img.crop((0, 0, w, h))
        data = to_rgb565(img)
        row = w * 2
        if self.line_length == row:
            self.mm.seek(0)
            self.mm.write(data)
        else:
            for y in range(h):
                self.mm.seek(y * self.line_length)
                self.mm.write(data[y * row:(y + 1) * row])

    def clear(self):
        self.mm.seek(0)
        self.mm.write(b"\x00" * self.smem_len)

    def close(self):
        try:
            self.clear()
        except Exception:
            pass
        self.mm.close()
        os.close(self.fd)


class PNGDisplay:
    """Simulator output: keeps the last frame and can save it."""

    def __init__(self):
        self.last = None

    def show(self, img):
        self.last = img.copy()

    def save(self, path, scale=1):
        img = self.last
        if scale != 1:
            img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
        img.save(path)

    def close(self):
        pass
