"""Wi-Fi and VPN through NetworkManager (nmcli). Reading and switching saved
connections works as the normal user; adding a new Wi-Fi network needs the
admin password."""

import os
import re
import subprocess

from . import admin

NMCLI = "/usr/bin/nmcli"


class NetError(Exception):
    pass


def split_terse(line):
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def _run(*args, timeout=30):
    try:
        p = subprocess.run([NMCLI] + list(args), capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    except FileNotFoundError:
        raise NetError("NetworkManager isn't installed")
    except subprocess.TimeoutExpired:
        raise NetError("NetworkManager didn't answer in time")
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "nmcli failed").strip().splitlines()[0]
        raise NetError(re.sub(r"^Error:\s*", "", msg))
    return p.stdout


def overview():
    conns = []
    for line in _run("-t", "-f", "NAME,UUID,TYPE,ACTIVE,DEVICE", "connection", "show").splitlines():
        f = split_terse(line)
        if len(f) >= 5 and f[2] in ("802-11-wireless", "wireguard", "vpn", "802-3-ethernet"):
            kind = {"802-11-wireless": "wifi", "802-3-ethernet": "ethernet"}.get(f[2], "vpn")
            conns.append({"name": f[0], "uuid": f[1], "type": kind, "active": f[3] == "yes",
                          "device": f[4] if f[4] not in ("", "--") else ""})
    return {"connections": conns}


def scan(rescan=False):
    nets = {}
    out = _run("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
               "--rescan", "yes" if rescan else "auto", timeout=40)
    for line in out.splitlines():
        f = split_terse(line)
        if len(f) < 4 or not f[1]:
            continue
        ssid = f[1]
        try:
            signal = int(f[2])
        except ValueError:
            signal = 0
        cur = nets.get(ssid)
        if not cur or signal > cur["signal"]:
            nets[ssid] = {"ssid": ssid, "signal": signal, "secure": f[3] not in ("", "--"),
                          "security": f[3] if f[3] != "--" else "", "in_use": f[0] == "*"}
    return sorted(nets.values(), key=lambda n: (not n["in_use"], -n["signal"]))


UUID_RE = re.compile(r"[0-9a-fA-F-]{36}")


def up(uuid):
    if not UUID_RE.fullmatch(uuid or ""):
        raise NetError("Invalid connection")
    _run("--wait", "30", "connection", "up", "uuid", uuid, timeout=45)


def down(uuid):
    if not UUID_RE.fullmatch(uuid or ""):
        raise NetError("Invalid connection")
    _run("connection", "down", "uuid", uuid)


SYSTEM_CONNECTIONS = "/etc/NetworkManager/system-connections"
INSTALL = "/usr/bin/install"


def _key_value(text):
    """Escape a value for a NetworkManager keyfile (GKeyFile syntax)."""
    text = text.replace("\\", "\\\\")
    if text.startswith(" "):
        text = "\\s" + text[1:]
    return text


def keyfile(ssid, psk, uuid, sae=False):
    """A NetworkManager connection file. The SSID is written as a byte list so
    any name works without escaping; the PSK is validated printable ASCII."""
    name = "".join(c if c.isprintable() else "?" for c in ssid)
    lines = ["[connection]", "id=%s" % _key_value(name), "uuid=%s" % uuid, "type=wifi", "",
             "[wifi]", "mode=infrastructure", "ssid=%s;" % ";".join(str(b) for b in ssid.encode()), ""]
    if psk:
        lines += ["[wifi-security]", "key-mgmt=%s" % ("sae" if sae else "wpa-psk"),
                  "psk=%s" % _key_value(psk), ""]
    lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    return "\n".join(lines)


def validate_join(ssid, psk):
    if not isinstance(ssid, str) or not ssid or len(ssid.encode()) > 32 or "\x00" in ssid:
        raise NetError("Invalid network name")
    if psk and not (re.fullmatch(r"[\x20-\x7e]{8,63}", psk) or re.fullmatch(r"[0-9a-fA-F]{64}", psk)):
        raise NetError("Wi-Fi passwords are 8 to 63 characters")


def _security_of(ssid):
    try:
        for n in scan():
            if n["ssid"] == ssid:
                return n["security"]
    except NetError:
        pass
    return ""


def join_steps(ssid, psk, password):
    """Join a new Wi-Fi network (a system-wide change, so it needs the admin
    password). The Wi-Fi password goes into a root-only connection file and is
    never put on a command line."""
    validate_join(ssid, psk)

    def steps(job):
        import tempfile
        import uuid as uuidlib
        job.add("Joining %s...\n" % ssid)
        sec = _security_of(ssid).upper()
        sae = "WPA3" in sec and "WPA2" not in sec and "WPA1" not in sec
        uid = str(uuidlib.uuid4())
        target = os.path.join(SYSTEM_CONNECTIONS, "zerodesk-%s.nmconnection" % uid)
        fd, tmp = tempfile.mkstemp(prefix=".zerodesk-wifi-", dir=packages_cache())
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(keyfile(ssid, psk, uid, sae))
            if admin.sudo_run(job, [INSTALL, "-m", "600", "-o", "root", "-g", "root", tmp, target],
                              password, timeout=30) != 0:
                raise admin.AdminError("Couldn't save the network")
        finally:
            os.unlink(tmp)
        ok = (admin.sudo_run(job, [NMCLI, "connection", "load", target], password, timeout=30) == 0 and
              admin.sudo_run(job, [NMCLI, "--wait", "40", "connection", "up", "uuid", uid],
                             password, timeout=60) == 0)
        if not ok:
            admin.sudo_run(job, [NMCLI, "connection", "delete", "uuid", uid], password, timeout=30)
            job.add("\nCouldn't connect. Check the password and try again.\n")
            job.state = "failed"
        else:
            job.add("Connected. If this browser loses contact, use the new address on the Cardputer.\n")
    return steps


def packages_cache():
    from .packages import cache_dir
    return cache_dir()


def forget_steps(uuid, password):
    if not UUID_RE.fullmatch(uuid or ""):
        raise NetError("Invalid connection")

    def steps(job):
        try:
            kind = _run("-g", "connection.type", "connection", "show", "uuid", uuid).strip()
        except NetError as e:
            raise admin.AdminError(str(e))
        if kind != "802-11-wireless":
            raise admin.AdminError("Only Wi-Fi networks can be forgotten here")
        proc = admin.sudo_popen([NMCLI, "connection", "delete", "uuid", uuid], password)
        admin.stream(job, proc)
    return steps
