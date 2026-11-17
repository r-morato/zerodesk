"""The HTTPS web server: login with the on-screen code, then stats, shell,
files and the live screen."""

import asyncio
import json
import os
import re
import time
import urllib.parse
import zlib

from aiohttp import WSMsgType, web

from . import admin, network, packages
from . import files as F
from .apps import AppError, icon_path, list_apps
from .screen import MAX_PASTE, text_to_keys, to_png
from .security import COOKIE, ABSOLUTE_TIMEOUT, host_ok, is_local_address, origin_ok

WEB_ROOT = os.environ.get("ZERODESK_WEB") or os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "web")
MAX_UPLOAD = 2 * 1024 ** 3
MAX_SHELLS = 4
PUBLIC_PATHS = ("/login", "/api/pair", "/static/")

CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; "
       "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")


def _json_error(status, msg):
    return web.json_response({"error": msg}, status=status)


class Context:
    """Everything the handlers need, so tests can swap parts out."""

    def __init__(self, guard, stats, runner, fingerprint="", framebuffer=None, keyboard=None,
                 apps_root=None, shell_factory=None):
        self.guard = guard
        self.stats = stats
        self.runner = runner
        self.fingerprint = fingerprint
        self.fb = framebuffer
        self.keyboard = keyboard
        self.keyboard_error = None
        self.apps_root = apps_root
        if shell_factory is None:
            from .shell import Shell
            shell_factory = Shell
        self.shell_factory = shell_factory
        self.shells = 0
        self._stats_cache = (0.0, None)
        self.viewers = 0
        self.passwords = admin.Passwords()
        self.jobs = admin.Jobs()
        self.store = packages.Store()


# ------------------------------------------------------------------ middleware
@web.middleware
async def security_mw(request, handler):
    ctx = request.app["ctx"]
    if not is_local_address(request.remote):
        return web.Response(status=403, text="ZeroDesk only accepts local network connections.")
    if not host_ok(request.host):
        return web.Response(status=421, text="Use the address shown on the Cardputer.")
    path = request.path
    public = path == "/login" or path.startswith("/static/") or path == "/api/pair"
    is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
    if request.method not in ("GET", "HEAD") or is_ws:
        if not origin_ok(request.headers.get("Origin"), request.host):
            return _json_error(403, "Bad origin")
    session = None
    if not public:
        session = ctx.guard.check(request.cookies.get(COOKIE))
        if not session:
            if path.startswith(("/api/", "/ws/")):
                return _json_error(401, "Not paired")
            raise web.HTTPFound("/login")
    request["session"] = session
    resp = await handler(request)
    if isinstance(resp, web.StreamResponse) and not resp.prepared:
        h = resp.headers
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h["Server"] = "ZeroDesk"
        if path.startswith("/api/") or path in ("/", "/login"):
            h.setdefault("Cache-Control", "no-store")
    return resp


# ------------------------------------------------------------------ pages
async def page_index(request):
    return web.FileResponse(os.path.join(WEB_ROOT, "index.html"))


async def page_login(request):
    if request.app["ctx"].guard.check(request.cookies.get(COOKIE), touch=False):
        raise web.HTTPFound("/")
    return web.FileResponse(os.path.join(WEB_ROOT, "login.html"))


async def api_pair(request):
    ctx = request.app["ctx"]
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        return _json_error(400, "Bad request")
    token, err = ctx.guard.pair(str(body.get("code", ""))[:32], request.remote)
    if not token:
        return _json_error(403, err)
    resp = web.json_response({"ok": True})
    resp.set_cookie(COOKIE, token, secure=True, httponly=True, samesite="Strict", path="/",
                    max_age=ABSOLUTE_TIMEOUT)
    return resp


async def api_logout(request):
    ctx = request.app["ctx"]
    ctx.passwords.forget(request["session"].token)
    ctx.guard.logout(request["session"].token)
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


async def api_info(request):
    ctx = request.app["ctx"]
    running = ctx.runner.running()
    return web.json_response({
        "fingerprint": ctx.fingerprint, "sessions": ctx.guard.count(),
        "screen": ctx.fb is not None,
        "keyboard": ctx.keyboard is not None, "keyboard_error": ctx.keyboard_error,
        "running": running and {"id": running["id"], "name": running["name"]}})


async def api_stats(request):
    ctx = request.app["ctx"]
    t, snap = ctx._stats_cache
    if snap is None or time.monotonic() - t > 1.0:
        snap = await asyncio.get_running_loop().run_in_executor(None, ctx.stats.snapshot)
        ctx._stats_cache = (time.monotonic(), snap)
    return web.json_response(snap)


# ------------------------------------------------------------------ files
def _run(fn, *a):
    return asyncio.get_running_loop().run_in_executor(None, fn, *a)


async def _files_call(fn, *a):
    try:
        return await _run(fn, *a)
    except F.FileError as e:
        raise web.HTTPBadRequest(text=json.dumps({"error": str(e)}), content_type="application/json")


async def files_list(request):
    return web.json_response(await _files_call(F.listdir, request.query.get("path")))


async def files_preview(request):
    return web.json_response(await _files_call(F.preview_text, request.query.get("path")))


async def files_download(request):
    try:
        fh, name, size = await _run(F.open_download, request.query.get("path"))
    except F.FileError as e:
        return _json_error(400, str(e))
    quoted = urllib.parse.quote(name, safe="")
    resp = web.StreamResponse(headers={
        "Content-Type": "application/octet-stream",
        "Content-Disposition": "attachment; filename*=UTF-8''%s" % quoted,
        "Content-Length": str(size), "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox"})
    try:
        await resp.prepare(request)
        while True:
            chunk = await _run(fh.read, 256 * 1024)
            if not chunk:
                break
            await resp.write(chunk)
        await resp.write_eof()
    finally:
        fh.close()
    return resp


async def _body(request):
    try:
        b = await request.json()
    except (ValueError, UnicodeDecodeError):
        b = None
    if not isinstance(b, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "Bad request"}),
                                 content_type="application/json")
    return b


async def files_mkdir(request):
    b = await _body(request)
    return web.json_response({"path": await _files_call(F.mkdir, b.get("path"), b.get("name"))})


async def files_rename(request):
    b = await _body(request)
    return web.json_response({"path": await _files_call(F.rename, b.get("path"), b.get("name"))})


async def files_delete(request):
    b = await _body(request)
    await _files_call(F.delete, b.get("path"))
    return web.json_response({"ok": True})


async def files_save(request):
    b = await _body(request)
    text = b.get("text")
    if not isinstance(text, str):
        return _json_error(400, "Bad request")
    await _files_call(F.save_text, b.get("path"), text)
    return web.json_response({"ok": True})


async def files_upload(request):
    directory = request.query.get("dir")
    overwrite = request.query.get("overwrite") == "1"
    reader = await request.multipart()
    saved = []
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name != "file" or not part.filename:
            continue
        final, tmp = await _files_call(F.upload_target, directory,
                                       os.path.basename(part.filename), overwrite)
        size = 0
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as fh:
                while True:
                    chunk = await part.read_chunk(256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise F.FileError("File is larger than 2 GB")
                    await _run(fh.write, chunk)
            if overwrite:
                os.replace(tmp, final)
            else:
                F.place_no_replace(tmp, final)
        except (OSError, F.FileError) as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            msg = str(e) if isinstance(e, F.FileError) else (e.strerror or str(e))
            return _json_error(400, msg)
        saved.append({"path": final, "size": size})
    return web.json_response({"saved": saved})


# ------------------------------------------------------------------ apps
async def apps_list(request):
    ctx = request.app["ctx"]
    apps = await _run(list_apps, ctx.apps_root) if ctx.apps_root else await _run(list_apps)
    running = ctx.runner.running()
    return web.json_response({
        "apps": [{"id": a["id"], "name": a["name"], "comment": a["comment"],
                  "icon": bool(icon_path(a, ctx.apps_root) if ctx.apps_root else icon_path(a))}
                 for a in apps],
        "running": running and running["id"]})


async def apps_icon(request):
    ctx = request.app["ctx"]
    apps = await _run(list_apps, ctx.apps_root) if ctx.apps_root else await _run(list_apps)
    app = next((a for a in apps if a["id"] == request.match_info["app_id"]), None)
    p = app and (icon_path(app, ctx.apps_root) if ctx.apps_root else icon_path(app))
    if not p:
        raise web.HTTPNotFound()
    ctype = "image/svg+xml" if p.endswith(".svg") else "image/jpeg" if p.endswith((".jpg", ".jpeg")) \
        else "image/png"
    return web.FileResponse(p, headers={"Content-Type": ctype, "Cache-Control": "max-age=3600",
                                        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})


async def apps_start(request):
    ctx = request.app["ctx"]
    try:
        app = await _run(ctx.runner.start, request.match_info["app_id"])
    except AppError as e:
        return _json_error(409, str(e))
    if ctx.fb:
        ctx.fb.force_next()
    return web.json_response({"running": {"id": app["id"], "name": app["name"]}})


async def apps_stop(request):
    ctx = request.app["ctx"]
    await _run(ctx.runner.stop)
    return web.json_response({"ok": True})


# ------------------------------------------------------------------ websockets
def _ws_json(data):
    try:
        m = json.loads(data)
    except ValueError:
        return {}
    return m if isinstance(m, dict) else {}


def _touch(request):
    """Websocket activity counts as using the app (at most every 30 s)."""
    s = request["session"]
    now = time.monotonic()
    if now - s.last_seen > 30:
        request.app["ctx"].guard.check(s.token, touch=True)


async def _session_watch(request, ws):
    """Close the socket if the session is revoked or expires."""
    ctx = request.app["ctx"]
    token = request["session"].token
    while not ws.closed:
        await asyncio.sleep(5)
        if not ctx.guard.check(token, touch=False):
            await ws.close(code=4401, message=b"session ended")
            return


OUT_HIGH = 512 * 1024        # stop reading the shell when this much output is waiting


async def ws_shell(request):
    ctx = request.app["ctx"]
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1024 * 1024)
    await ws.prepare(request)
    if ctx.shells >= MAX_SHELLS:
        await ws.close(code=4429, message=b"too many shells")
        return ws
    try:
        cols = int(request.query.get("cols", 80))
        rows = int(request.query.get("rows", 24))
    except ValueError:
        cols, rows = 80, 24
    try:
        sh = await _run(ctx.shell_factory, cols, rows)
    except OSError as e:
        await ws.send_str(json.dumps({"type": "error", "message": str(e)}))
        await ws.close()
        return ws
    ctx.shells += 1
    loop = asyncio.get_running_loop()
    out_q = asyncio.Queue()
    state = {"queued": 0, "reading": False, "writing": False}

    def start_reading():
        if not state["reading"] and not sh.closed:
            loop.add_reader(sh.master, on_readable)
            state["reading"] = True

    def stop_reading():
        if state["reading"]:
            loop.remove_reader(sh.master)
            state["reading"] = False

    def on_readable():
        data = sh.read()
        if data is None:
            return
        out_q.put_nowait(data)
        if data == b"":
            stop_reading()
            return
        state["queued"] += len(data)
        if state["queued"] > OUT_HIGH:
            stop_reading()             # back-pressure: wait for the browser to catch up

    def on_writable():
        if sh.flush():
            loop.remove_writer(sh.master)
            state["writing"] = False

    def send_input(data):
        if not sh.queue_input(data):
            return                     # program isn't reading its input: drop the paste
        if not sh.flush() and not state["writing"]:
            loop.add_writer(sh.master, on_writable)
            state["writing"] = True

    async def pump():
        while True:
            data = await out_q.get()
            if data == b"":
                await ws.close(code=1000, message=b"shell exited")
                return
            await ws.send_bytes(data)
            state["queued"] -= len(data)
            if state["queued"] < OUT_HIGH // 4:
                start_reading()

    start_reading()
    pump_task = asyncio.ensure_future(pump())
    watch_task = asyncio.ensure_future(_session_watch(request, ws))
    try:
        async for msg in ws:
            _touch(request)
            if msg.type == WSMsgType.BINARY:
                send_input(msg.data)
            elif msg.type == WSMsgType.TEXT:
                m = _ws_json(msg.data)
                if m.get("type") == "input" and isinstance(m.get("data"), str):
                    send_input(m["data"])
                elif m.get("type") == "resize":
                    try:
                        sh.resize(m.get("cols", 80), m.get("rows", 24))
                    except (TypeError, ValueError, OSError):
                        pass
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        for fn in (stop_reading, lambda: loop.remove_writer(sh.master)):
            try:
                fn()
            except (ValueError, OSError):
                pass
        pump_task.cancel()
        watch_task.cancel()
        await _run(sh.close)
        ctx.shells -= 1
    return ws


async def ws_screen(request):
    ctx = request.app["ctx"]
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024)
    await ws.prepare(request)
    if not ctx.fb:
        await ws.send_str(json.dumps({"type": "error", "message": "Screen not available"}))
        await ws.close()
        return ws
    ctx.viewers += 1
    ctx.guard.changed.set()
    last_crc = [None]

    async def frames():
        while not ws.closed:
            raw = await _run(ctx.fb.raw)
            crc = zlib.crc32(raw)
            if crc != last_crc[0]:
                last_crc[0] = crc
                png = await _run(lambda: to_png(ctx.fb.image(raw)))
                await ws.send_bytes(png)
            await asyncio.sleep(0.08)

    frame_task = asyncio.ensure_future(frames())
    watch_task = asyncio.ensure_future(_session_watch(request, ws))
    kb = ctx.keyboard
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            _touch(request)
            m = _ws_json(msg.data)
            if m.get("type") == "key" and kb and isinstance(m.get("code"), str):
                kb.key(m["code"], bool(m.get("down")))
            elif m.get("type") == "refresh":
                last_crc[0] = None
            elif m.get("type") == "text" and kb and isinstance(m.get("text"), str):
                for code, shift in text_to_keys(m["text"][:MAX_PASTE]):
                    if ws.closed:
                        break
                    kb.tap(code, shift)
                    await asyncio.sleep(0.012)
    finally:
        frame_task.cancel()
        watch_task.cancel()
        if kb:
            kb.release_all()
        ctx.viewers -= 1
        ctx.guard.changed.set()
    return ws


# ------------------------------------------------------------------ admin jobs
def _token(request):
    return request["session"].token


async def admin_status(request):
    ctx = request.app["ctx"]
    try:
        needs = await _run(admin.needs_password)
    except admin.AdminError as e:
        return _json_error(400, str(e))
    return web.json_response({"needs_password": needs,
                              "remembered": ctx.passwords.get(_token(request)) is not None})


async def admin_password(request):
    ctx = request.app["ctx"]
    b = await _body(request)
    pw = b.get("password")
    if not isinstance(pw, str) or not pw or len(pw) > 256:
        return _json_error(400, "Bad request")
    try:
        await _run(admin.check_password, pw)
    except admin.NeedPassword:
        return _json_error(403, "Wrong password")
    except admin.AdminError as e:
        return _json_error(400, str(e))
    ctx.passwords.set(_token(request), pw)
    return web.json_response({"ok": True})


async def admin_forget(request):
    request.app["ctx"].passwords.forget(_token(request))
    return web.json_response({"ok": True})


async def _start_job(request, title, steps_factory):
    """Check the admin password for this session, then start the job."""
    ctx = request.app["ctx"]
    pw = ctx.passwords.get(_token(request))
    try:
        await _run(admin.check_password, pw)
    except admin.NeedPassword as e:
        if e.wrong:
            ctx.passwords.forget(_token(request))
        return web.json_response({"need_password": True, "wrong": e.wrong}, status=428)
    except admin.AdminError as e:
        return _json_error(400, str(e))
    try:
        steps = steps_factory(pw)
        job = ctx.jobs.start(title, steps)
    except (admin.AdminError, network.NetError) as e:
        return _json_error(409, str(e))
    return web.json_response({"job": job.id})


async def job_view(request):
    job = request.app["ctx"].jobs.get(request.match_info["job_id"])
    if not job:
        raise web.HTTPNotFound()
    return web.json_response(job.view())


# ------------------------------------------------------------------ packages
async def store_list(request):
    ctx = request.app["ctx"]
    try:
        apps = await _run(ctx.store.apps)
    except Exception as e:  # network errors of all kinds
        return _json_error(502, "Couldn't reach the store: %s" % e)
    installed = await _run(packages.installed_versions)
    for a in apps:
        a["installed"] = installed.get(a["package"])
    return web.json_response({"apps": apps})


async def store_install(request):
    ctx = request.app["ctx"]
    code = request.match_info["code"]
    try:
        entry = await _run(ctx.store.entry, code)
    except Exception as e:
        return _json_error(404, str(e))

    def factory(pw):
        def steps(job):
            path = packages.download_store_deb(job, entry)
            packages.install_steps(path, pw)(job)
        return steps
    return await _start_job(request, "Install %s" % entry.get("title", code), factory)


async def deb_upload(request):
    reader = await request.multipart()
    part = await reader.next()
    while part is not None and (part.name != "file" or not part.filename):
        part = await reader.next()
    if part is None or not part.filename.lower().endswith(".deb"):
        return _json_error(400, "Choose a .deb file")
    dest = os.path.join(await _run(packages.cache_dir), "upload-%d.deb" % int(time.time() * 1000))
    size = 0
    with open(dest, "wb") as fh:
        while True:
            chunk = await part.read_chunk(256 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > packages.MAX_DEB:
                fh.close()
                os.remove(dest)
                return _json_error(400, "Package is too large")
            await _run(fh.write, chunk)
    try:
        info = await _run(packages.deb_info, dest)
    except admin.AdminError as e:
        os.remove(dest)
        return _json_error(400, str(e))
    resp = await _start_job(request, "Install %s" % info["Package"],
                            lambda pw: packages.install_steps(dest, pw))
    if resp.status != 200 and resp.status != 428:
        os.remove(dest)
    if resp.status == 428:
        # keep the upload so the browser can retry after the password
        resp = web.json_response({"need_password": True, "upload": os.path.basename(dest)}, status=428)
    return resp


async def deb_install_cached(request):
    """Retry installing a just-uploaded package after the password was given."""
    b = await _body(request)
    name = b.get("upload")
    if not isinstance(name, str) or not re.fullmatch(r"upload-\d+\.deb", name):
        return _json_error(400, "Bad request")
    path = os.path.join(await _run(packages.cache_dir), name)
    if not os.path.isfile(path):
        return _json_error(404, "Upload expired, please upload again")
    info = await _run(packages.deb_info, path)
    return await _start_job(request, "Install %s" % info["Package"],
                            lambda pw: packages.install_steps(path, pw))


async def installed_apps(request):
    owned = await _run(packages.launcher_packages)
    ctx = request.app["ctx"]
    apps = await _run(list_apps, ctx.apps_root) if ctx.apps_root else await _run(list_apps)
    names = {a["id"]: a["name"] for a in apps}
    return web.json_response({"apps": sorted(
        [{"package": p, "name": names.get(d, p)} for p, d in owned.items()],
        key=lambda a: a["name"].lower())})


async def package_remove(request):
    pkg = request.match_info["pkg"]
    if not packages.PKG_RE.fullmatch(pkg):
        return _json_error(400, "Bad package name")
    return await _start_job(request, "Remove %s" % pkg, lambda pw: packages.remove_steps(pkg, pw))


# ------------------------------------------------------------------ network
async def net_overview(request):
    try:
        return web.json_response(await _run(network.overview))
    except network.NetError as e:
        return _json_error(400, str(e))


async def net_scan(request):
    try:
        nets = await _run(network.scan, request.query.get("rescan") == "1")
    except network.NetError as e:
        return _json_error(400, str(e))
    return web.json_response({"networks": nets})


async def net_up(request):
    b = await _body(request)
    try:
        await _run(network.up, b.get("uuid"))
    except network.NetError as e:
        return _json_error(400, str(e))
    return web.json_response({"ok": True})


async def net_down(request):
    b = await _body(request)
    try:
        await _run(network.down, b.get("uuid"))
    except network.NetError as e:
        return _json_error(400, str(e))
    return web.json_response({"ok": True})


async def net_join(request):
    b = await _body(request)
    ssid, psk = b.get("ssid"), b.get("password") or ""
    if not isinstance(ssid, str) or not isinstance(psk, str):
        return _json_error(400, "Bad request")
    try:
        network.join_steps(ssid, psk, None)        # validate before asking for anything
    except network.NetError as e:
        return _json_error(400, str(e))
    return await _start_job(request, "Join %s" % ssid, lambda pw: network.join_steps(ssid, psk, pw))


async def net_forget(request):
    b = await _body(request)
    uuid = b.get("uuid")
    try:
        network.forget_steps(uuid, None)
    except network.NetError as e:
        return _json_error(400, str(e))
    return await _start_job(request, "Forget network", lambda pw: network.forget_steps(uuid, pw))


# ------------------------------------------------------------------ app
def make_app(ctx):
    app = web.Application(middlewares=[security_mw], client_max_size=2 * 1024 * 1024)
    app["ctx"] = ctx
    r = app.router
    r.add_get("/", page_index)
    r.add_get("/login", page_login)
    r.add_static("/static/", WEB_ROOT, show_index=False, follow_symlinks=False)
    r.add_post("/api/pair", api_pair)
    r.add_post("/api/logout", api_logout)
    r.add_get("/api/info", api_info)
    r.add_get("/api/stats", api_stats)
    r.add_get("/api/files/list", files_list)
    r.add_get("/api/files/preview", files_preview)
    r.add_get("/api/files/download", files_download)
    r.add_post("/api/files/mkdir", files_mkdir)
    r.add_post("/api/files/rename", files_rename)
    r.add_post("/api/files/delete", files_delete)
    r.add_post("/api/files/save", files_save)
    r.add_post("/api/files/upload", files_upload)
    r.add_get("/api/apps", apps_list)
    r.add_get("/api/apps/{app_id}/icon", apps_icon)
    r.add_post("/api/apps/{app_id}/start", apps_start)
    r.add_post("/api/apps/stop", apps_stop)
    r.add_get("/api/admin/status", admin_status)
    r.add_post("/api/admin/password", admin_password)
    r.add_post("/api/admin/forget", admin_forget)
    r.add_get("/api/jobs/{job_id}", job_view)
    r.add_get("/api/store", store_list)
    r.add_post("/api/store/{code}/install", store_install)
    r.add_post("/api/packages/upload", deb_upload)
    r.add_post("/api/packages/install-upload", deb_install_cached)
    r.add_get("/api/packages/installed", installed_apps)
    r.add_post("/api/packages/{pkg}/remove", package_remove)
    r.add_get("/api/network", net_overview)
    r.add_get("/api/network/scan", net_scan)
    r.add_post("/api/network/up", net_up)
    r.add_post("/api/network/down", net_down)
    r.add_post("/api/network/join", net_join)
    r.add_post("/api/network/forget", net_forget)
    r.add_get("/ws/shell", ws_shell)
    r.add_get("/ws/screen", ws_screen)
    return app
