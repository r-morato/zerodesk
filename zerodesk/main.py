"""ZeroDesk entry point.

    python3 -m zerodesk              # on the CardputerZero
    python3 -m zerodesk --sim DIR    # render the device screen to PNGs
    python3 -m zerodesk --dev        # run the web server on a PC with fake screen
"""

import argparse
import asyncio
import os
import signal
import socket
import sys

from . import __version__

PORTS = range(8443, 8454)


def _free_port(host="0.0.0.0"):
    for p in PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise OSError("No free port between 8443 and 8453")


async def _serve(ctx, port, cert, key, stop_event):
    from aiohttp import web
    from .server import make_app
    from .tls import server_context
    try:
        runner = web.AppRunner(make_app(ctx), access_log=None, shutdown_timeout=2.0)
    except TypeError:                    # older aiohttp
        runner = web.AppRunner(make_app(ctx), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port, ssl_context=server_context(cert, key))
    await site.start()
    await stop_event.wait()
    await runner.cleanup()


def run_device():
    from .apps import Runner
    from .device import DeviceScreen, PhysicalKeys, run_in_thread
    from .display import FramebufferDisplay
    from .screen import Framebuffer, RemoteKeyboard
    from .security import Guard
    from .server import Context
    from .stats import Stats, local_addresses
    from .tls import ensure_cert, fingerprint

    cert, key = ensure_cert()
    fp = fingerprint(cert)
    port = _free_port()
    guard = Guard()

    try:
        fb = Framebuffer()
    except OSError as e:
        print("zerodesk: screen mirroring unavailable: %s" % e, file=sys.stderr)
        fb = None
    keyboard, kb_err = None, None
    try:
        keyboard = RemoteKeyboard()        # before apps start, so they see it
    except Exception as e:
        kb_err = ("Remote typing needs access to /dev/uinput. Reboot once after "
                  "installing ZeroDesk. (%s)" % e)
    runner = Runner(on_change=lambda **kw: guard.changed.set())
    ctx = Context(guard, Stats(), runner, fingerprint=fp, framebuffer=fb, keyboard=keyboard)
    ctx.keyboard_error = kb_err

    display = FramebufferDisplay()
    keys = PhysicalKeys()
    screen = DeviceScreen(guard, runner, local_addresses, port, fp, display, keys)
    screen.viewers = lambda: ctx.viewers

    loop = asyncio.new_event_loop()
    stop_event = asyncio.Event()

    def quit_():
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, quit_)
    run_in_thread(screen, quit_)
    try:
        loop.run_until_complete(_serve(ctx, port, cert, key, stop_event))
    finally:
        screen.running = False
        runner.stop()
        if keyboard:
            keyboard.close()
        keys.close()
        display.close()
        if fb:
            fb.close()
    # Background work still running (a Wi-Fi scan, a store download) would keep
    # the process alive until it finishes; everything that matters is closed.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def run_dev(port):
    """Web server on a desktop with fake screen, for developing the UI."""
    from PIL import Image, ImageDraw
    from .apps import Runner
    from .security import Guard
    from .server import Context
    from .stats import Stats
    from .tls import ensure_cert, fingerprint

    class FakeFB:
        def __init__(self):
            self.n = 0

        def raw(self):
            self.n += 1
            return bytes([self.n // 10 % 256])

        def image(self, raw=None):
            if os.environ.get("ZERODESK_DEV_SCREEN"):         # a picture to mirror (dev only)
                return Image.open(os.environ["ZERODESK_DEV_SCREEN"]).convert("RGB").resize((320, 170))
            img = Image.new("RGB", (320, 170), (13, 17, 23))
            d = ImageDraw.Draw(img)
            d.text((10, 10), "Fake Cardputer screen %d" % (self.n // 10), fill=(230, 237, 243))
            return img

        def force_next(self):
            pass

    from .tls import config_dir
    cert, key = ensure_cert(os.environ.get("ZERODESK_DEV_DIR", config_dir() + "-dev"))
    guard = Guard()
    print("ZeroDesk dev server on https://127.0.0.1:%d  code: %s" % (port, guard.code))
    root = os.environ.get("ZERODESK_APPS_ROOT")
    ctx = Context(guard, Stats(), Runner(root) if root else Runner(), fingerprint=fingerprint(cert),
                  framebuffer=FakeFB(), apps_root=root)
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.run_until_complete(_serve(ctx, port, cert, key, stop))
    return 0


def run_sim(outdir):
    from .apps import Runner
    from .device import DeviceScreen
    from .security import Guard
    os.makedirs(outdir, exist_ok=True)
    g = Guard()
    g.code = "482913"

    class NoRunner(Runner):
        def running(self):
            return None
    s = DeviceScreen(g, NoRunner(), lambda: [("wlan0", "192.168.1.50"), ("home", "10.8.0.4")],
                     8443, "AB:" * 31 + "7F", None, None)
    s.render().save(os.path.join(outdir, "device.png"))
    s.render().resize((960, 510), 0).save(os.path.join(outdir, "device@3x.png"))
    g.count = lambda: 1
    g.remotes = lambda: ["192.168.1.23"]
    s.viewers = lambda: 1
    s.render().resize((960, 510), 0).save(os.path.join(outdir, "device-connected@3x.png"))
    s.show_fp = True
    s.fingerprint = ":".join("%02X" % ((i * 37 + 11) % 256) for i in range(32))
    s.render().resize((960, 510), 0).save(os.path.join(outdir, "device-cert@3x.png"))
    s.render().save(os.path.join(outdir, "device-cert.png"))
    print("saved to", outdir)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="zerodesk")
    p.add_argument("--sim", metavar="DIR")
    p.add_argument("--dev", metavar="PORT", type=int, nargs="?", const=8443)
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    a = p.parse_args(argv)
    if a.sim:
        return run_sim(a.sim)
    if a.dev:
        return run_dev(a.dev)
    return run_device()


if __name__ == "__main__":
    sys.exit(main())
