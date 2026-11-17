import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from zerodesk import files as F  # noqa: E402
from zerodesk import security as S  # noqa: E402
from zerodesk.apps import Runner, exec_argv, icon_path, list_apps  # noqa: E402
from zerodesk.server import Context, make_app  # noqa: E402
from zerodesk.stats import Stats  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestGuard(unittest.TestCase):
    def test_code_is_one_time(self):
        g = S.Guard()
        code = g.code
        tok, err = g.pair(code, "192.168.1.2")
        self.assertTrue(tok and not err)
        self.assertNotEqual(g.code, code)
        self.assertEqual(g.pair(code, "192.168.1.3"), (None, "Wrong code."))

    def test_brute_force_is_capped_until_physical_reset(self):
        clock = Clock()
        g = S.Guard(clock)
        guesses = 0
        for lockout in range(S.MAX_LOCKOUTS):
            for _ in range(S.MAX_FAILURES):
                wrong = "000000" if g.code != "000000" else "111111"
                g.pair(wrong, "10.0.0.66")
                guesses += 1
            if lockout < S.MAX_LOCKOUTS - 1:
                self.assertIsNone(g.pair(g.code, "x")[0])          # paused: even the right code waits
                clock.t += S.LOCKOUT_SECONDS * 2 ** lockout + 1   # delays grow
        self.assertTrue(g.stopped)
        clock.t += 10 ** 6                                          # time alone doesn't help
        tok, err = g.pair(g.code, "x")
        self.assertIsNone(tok)
        self.assertIn("Press 4", err)
        self.assertEqual(guesses, S.MAX_FAILURES * S.MAX_LOCKOUTS)
        self.assertEqual(g.last_bad, "10.0.0.66")
        g.new_code()                                                # key on the Cardputer
        self.assertIsNotNone(g.pair(g.code, "x")[0])

    def test_code_expires(self):
        clock = Clock()
        g = S.Guard(clock)
        code = g.code
        clock.t += S.CODE_LIFETIME + 1
        self.assertIn("expired", g.pair(code, "x")[1])
        g.tick()
        self.assertIsNotNone(g.pair(g.code, "x")[0])

    def test_sessions_expire_and_revoke(self):
        clock = Clock()
        g = S.Guard(clock)
        tok, _ = g.pair(g.code, "x")
        self.assertIsNotNone(g.check(tok))
        clock.t += S.IDLE_TIMEOUT + 1
        self.assertIsNone(g.check(tok))
        tok, _ = g.pair(g.code, "x")
        g.revoke_all()
        self.assertIsNone(g.check(tok))
        self.assertIsNone(g.check("guess"))

    def test_session_cap(self):
        g = S.Guard()
        toks = [g.pair(g.code, "x")[0] for _ in range(S.MAX_SESSIONS + 1)]
        self.assertIsNone(g.check(toks[0], touch=False))
        self.assertEqual(g.count(), S.MAX_SESSIONS)

    def test_local_addresses(self):
        for ok in ("192.168.1.5", "10.1.2.3", "172.20.0.1", "100.101.1.1", "127.0.0.1", "::1",
                   "fe80::1%wlan0", "fd00::5", "::ffff:192.168.1.9"):
            self.assertTrue(S.is_local_address(ok), ok)
        for bad in ("8.8.8.8", "203.0.113.7", "2001:4860::8888", "::ffff:8.8.8.8", "", None, "x"):
            self.assertFalse(S.is_local_address(bad), bad)

    def test_host_header(self):
        for ok in ("192.168.1.5:8443", "127.0.0.1:8443", "[fe80::1]:8443", "cardputer.local:8443", "localhost"):
            self.assertTrue(S.host_ok(ok), ok)
        for bad in ("evil.com", "evil.com:8443", "192.168.1.5.evil.com", "999.1.1.1:8443", "", None,
                    "a.local.evil.com:8443", "[1:2]:8443", "[:::::]:8443", "192.168.1.5.:8443"):
            self.assertFalse(S.host_ok(bad), bad)

    def test_origin(self):
        self.assertTrue(S.origin_ok("https://192.168.1.5:8443", "192.168.1.5:8443"))
        self.assertFalse(S.origin_ok("https://evil.com", "192.168.1.5:8443"))
        self.assertFalse(S.origin_ok(None, "192.168.1.5:8443"))
        self.assertFalse(S.origin_ok("http://192.168.1.5:8443", "192.168.1.5:8443"))


class TestFiles(unittest.TestCase):
    def test_ops(self):
        with tempfile.TemporaryDirectory() as d:
            F.mkdir(d, "a")
            with open(os.path.join(d, "a", "t.txt"), "w") as fh:
                fh.write("hi")
            lst = F.listdir(d)
            self.assertEqual(lst["entries"][0]["name"], "a")
            self.assertEqual(F.preview_text(os.path.join(d, "a", "t.txt"))["text"], "hi")
            os.symlink(os.path.join(d, "a", "t.txt"), os.path.join(d, "link.txt"))
            F.save_text(os.path.join(d, "link.txt"), "new")            # writes through the link
            self.assertTrue(os.path.islink(os.path.join(d, "link.txt")))
            with open(os.path.join(d, "a", "t.txt")) as fh:
                self.assertEqual(fh.read(), "new")
            os.remove(os.path.join(d, "link.txt"))
            open(os.path.join(d, "a", "u.txt"), "w").close()
            with self.assertRaises(F.FileError):
                F.rename(os.path.join(d, "a", "u.txt"), "t.txt")      # never overwrites
            F.rename(os.path.join(d, "a"), "b")
            F.delete(os.path.join(d, "b"))
            self.assertEqual(F.listdir(d)["entries"], [])

    def test_rejects_bad_names_and_paths(self):
        for bad in ("../x", "a/b", "..", ".", "", "x\x00"):
            with self.assertRaises(F.FileError):
                F.clean_name(bad)
        with self.assertRaises(F.FileError):
            F.clean_path("relative/path")
        with self.assertRaises(F.FileError):
            F.delete("/")

    def test_binary_not_previewed(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"\x00\x01\x02")
        try:
            with self.assertRaises(F.FileError):
                F.preview_text(fh.name)
        finally:
            os.remove(fh.name)


class TestApps(unittest.TestCase):
    def test_list_and_icons(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "applications"))
            os.makedirs(os.path.join(root, "share/images"))
            open(os.path.join(root, "share/images/a.png"), "wb").write(b"png")
            with open(os.path.join(root, "applications/a.desktop"), "w") as fh:
                fh.write("[Desktop Entry]\nName=Alpha\nExec=/bin/true %f\nIcon=share/images/a.png\n")
            with open(os.path.join(root, "applications/evil.desktop"), "w") as fh:
                fh.write("[Desktop Entry]\nName=Evil\nExec=/bin/true\nIcon=../../../../etc/passwd\n")
            with open(os.path.join(root, "applications/zerodesk.desktop"), "w") as fh:
                fh.write("[Desktop Entry]\nName=ZeroDesk\nExec=/bin/true\n")
            apps = list_apps(root)
            self.assertEqual([a["name"] for a in apps], ["Alpha", "Evil"])   # not ourselves
            self.assertTrue(icon_path(apps[0], root))
            self.assertIsNone(icon_path(apps[1], root))                     # outside the tree
            self.assertEqual(exec_argv(apps[0]["exec"]), ["/bin/true"])

    def test_runner_one_at_a_time(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "applications"))
            with open(os.path.join(root, "applications/s.desktop"), "w") as fh:
                fh.write("[Desktop Entry]\nName=S\nExec=/bin/sleep 30\n")
            r = Runner(root)
            r.start("s")
            from zerodesk.apps import AppError
            with self.assertRaises(AppError):
                r.start("s")
            r.stop()
            import time
            time.sleep(0.3)
            self.assertIsNone(r.running())


class TestShellSafety(unittest.TestCase):
    def test_close_kills_background_jobs(self):
        from zerodesk.shell import Shell, _alive
        import time
        sh = Shell()
        sh.write("sleep 7777 & setsid -w sleep 7778 & sleep 7779\n")
        time.sleep(0.8)
        pids = [p for p in sh._session_pids() if p != sh.proc.pid]
        self.assertTrue(pids)
        sh.close()
        time.sleep(0.3)
        self.assertFalse([p for p in pids if _alive(p)])

    def test_input_is_bounded_and_never_blocks(self):
        from zerodesk.shell import MAX_PENDING_INPUT, Shell
        import time
        sh = Shell()
        sh.write("stty raw -echo; sleep 30\n")
        time.sleep(0.5)
        t = time.monotonic()
        accepted = 0
        while sh.queue_input(b"x" * 65536):
            accepted += 65536
            sh.flush()
        self.assertLess(time.monotonic() - t, 2)          # no blocking
        self.assertLessEqual(len(sh.pending), MAX_PENDING_INPUT)
        sh.close()


class TestKeyFilter(unittest.TestCase):
    def test_system_combos_blocked(self):
        from zerodesk.screen import RemoteKeyboard

        class E:
            EV_KEY = 1
        kb = RemoteKeyboard.__new__(RemoteKeyboard)
        kb.e = E()
        names = ["KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_DELETE", "KEY_F2", "KEY_LEFT", "KEY_A"]
        kb.codes = {n: i + 10 for i, n in enumerate(names)}
        sent = []

        class UI:
            def write(self, t, c, v):
                sent.append((c, v))

            def syn(self):
                pass
        kb.ui, kb.down = UI(), set()
        import zerodesk.screen as scr
        old = scr.BROWSER_KEYS
        scr.BROWSER_KEYS = {"C": "KEY_LEFTCTRL", "A": "KEY_LEFTALT", "Del": "KEY_DELETE",
                            "F2": "KEY_F2", "L": "KEY_LEFT", "X": "KEY_A"}
        try:
            kb.key("C", True); kb.key("A", True)
            self.assertFalse(kb.key("Del", True))       # Ctrl+Alt+Del
            self.assertFalse(kb.key("F2", True))        # console switch
            self.assertFalse(kb.key("L", True))
            self.assertTrue(kb.key("X", True))          # ordinary keys still work
            kb.key("A", False)
            self.assertTrue(kb.key("Del", True))        # Ctrl+Del alone is fine
        finally:
            scr.BROWSER_KEYS = old


class TestImage(unittest.TestCase):
    def test_rgb565_decode(self):
        from zerodesk.screen import Framebuffer
        fb = Framebuffer.__new__(Framebuffer)
        fb.width, fb.height, fb.bpp, fb.line_length = 2, 1, 16, 4
        img = fb.image(bytes([0x00, 0xF8, 0x1F, 0x00]))
        self.assertEqual(img.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(img.getpixel((1, 0)), (0, 0, 255))


class FakeShell:
    def __init__(self, cols, rows):
        self.master, self._w = os.pipe()
        self.closed = False

    def queue_input(self, d):
        return True

    def flush(self):
        return True

    def read(self):
        return None

    def write(self, d):
        pass

    def resize(self, c, r):
        pass

    def close(self):
        self.closed = True
        os.close(self.master)
        os.close(self._w)


class TestServer_(unittest.TestCase):
    """Drives the real aiohttp app (without TLS) to check every gate."""

    HOST = "192.168.1.5:8443"
    ORIGIN = "https://192.168.1.5:8443"

    def run_async(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def make(self):
        self.guard = S.Guard()
        ctx = Context(self.guard, Stats(), Runner("/nonexistent"), shell_factory=FakeShell)
        return make_app(ctx)

    async def _client(self):
        server = TestServer(self.make())
        client = TestClient(server)
        await client.start_server()
        return client

    def test_gates(self):
        async def go():
            c = await self._client()
            h = {"Host": self.HOST}
            try:
                r = await c.get("/api/stats", headers=h)
                self.assertEqual(r.status, 401)
                r = await c.get("/", headers=h, allow_redirects=False)
                self.assertEqual(r.status, 302)
                # pairing needs our Origin
                r = await c.post("/api/pair", json={"code": self.guard.code}, headers=h)
                self.assertEqual(r.status, 403)
                r = await c.post("/api/pair", json={"code": "x"}, headers=dict(h, Origin=self.ORIGIN))
                self.assertEqual(r.status, 403)
                code = self.guard.code
                r = await c.post("/api/pair", json={"code": code}, headers=dict(h, Origin=self.ORIGIN))
                self.assertEqual(r.status, 200)
                cookie = r.cookies[S.COOKIE]
                self.assertTrue(cookie["httponly"] and cookie["secure"])
                self.assertEqual(cookie["samesite"], "Strict")
                ck = {"Cookie": "%s=%s" % (S.COOKIE, cookie.value)}
                r = await c.get("/api/stats", headers=dict(h, **ck))
                self.assertEqual(r.status, 200)
                self.assertIn("cpu", await r.json())
                self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
                # DNS rebinding: wrong Host is refused even with a valid cookie
                r = await c.get("/api/stats", headers={"Host": "evil.com", **ck})
                self.assertEqual(r.status, 421)
                # cross-site POST with the cookie is refused
                r = await c.post("/api/files/delete", json={"path": "/tmp/x"},
                                 headers=dict(h, Origin="https://evil.com", **ck))
                self.assertEqual(r.status, 403)
                # websocket from another origin is refused
                r = await c.get("/ws/shell", headers=dict(h, Origin="https://evil.com", Upgrade="websocket",
                                                          Connection="Upgrade", **ck))
                self.assertEqual(r.status, 403)
                # downloads are always attachments, and exactly the file asked for
                with tempfile.TemporaryDirectory() as dd:
                    fp = os.path.join(dd, "page.html")
                    with open(fp, "w") as fh:
                        fh.write("<script>alert(1)</script>")
                    with open(fp + ".gz", "wb") as fh:
                        fh.write(b"\x1f\x8bDIFFERENT")
                    r = await c.get("/api/files/download", params={"path": fp},
                                    headers=dict(h, **ck, **{"Accept-Encoding": "gzip"}))
                    self.assertEqual(r.headers["Content-Type"], "application/octet-stream")
                    self.assertTrue(r.headers["Content-Disposition"].startswith("attachment"))
                    self.assertNotIn("Content-Encoding", r.headers)
                    self.assertEqual(await r.read(), b"<script>alert(1)</script>")
                # malformed JSON shapes are 400, not 500
                r = await c.post("/api/pair", data="[1]", headers=dict(h, Origin=self.ORIGIN,
                                 **{"Content-Type": "application/json"}))
                self.assertEqual(r.status, 400)
                self.assertNotIn("Python", r.headers.get("Server", ""))
                # logout ends the session
                r = await c.post("/api/logout", headers=dict(h, Origin=self.ORIGIN, **ck))
                self.assertEqual(r.status, 200)
                r = await c.get("/api/stats", headers=dict(h, **ck))
                self.assertEqual(r.status, 401)
            finally:
                await c.close()
        self.run_async(go())

    def test_non_local_client_refused(self):
        async def go():
            app = self.make()
            from aiohttp.test_utils import make_mocked_request
            from zerodesk.server import security_mw

            class T:
                def __init__(self):
                    self.peername = ("8.8.8.8", 1234)

                def get_extra_info(self, k, default=None):
                    return self.peername if k == "peername" else default
            req = make_mocked_request("GET", "/api/stats", headers={"Host": self.HOST}, app=app,
                                      transport=T())

            async def handler(r):
                raise AssertionError("must not reach handler")
            resp = await security_mw(req, handler)
            self.assertEqual(resp.status, 403)
        self.run_async(go())

    def test_upload_and_shell_ws(self):
        async def go():
            c = await self._client()
            code = self.guard.code
            h = {"Host": self.HOST, "Origin": self.ORIGIN}
            r = await c.post("/api/pair", json={"code": code}, headers=h)
            ck = {"Cookie": "%s=%s" % (S.COOKIE, r.cookies[S.COOKIE].value)}
            try:
                with tempfile.TemporaryDirectory() as d:
                    import aiohttp
                    form = aiohttp.FormData()
                    form.add_field("file", b"data!", filename="../../evil.txt")
                    r = await c.post("/api/files/upload", params={"dir": d}, data=form, headers=dict(h, **ck))
                    self.assertEqual(r.status, 200, await r.text())
                    inside = os.listdir(d)
                    self.assertEqual(len(inside), 1)                      # stayed in the folder
                    self.assertFalse(os.path.exists(os.path.join(os.path.dirname(os.path.dirname(d)), "evil.txt")))
                    for expect in (200, 400):                             # no silent overwrite
                        form = aiohttp.FormData()
                        form.add_field("file", b"again", filename="plain.txt")
                        r = await c.post("/api/files/upload", params={"dir": d}, data=form, headers=dict(h, **ck))
                        self.assertEqual(r.status, expect)
                ws = await c.ws_connect("/ws/shell", headers=dict(h, **ck))
                await ws.send_str(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
                await ws.close()
            finally:
                await c.close()
        self.run_async(go())


if __name__ == "__main__":
    unittest.main()
