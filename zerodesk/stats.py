"""System stats read straight from /proc and /sys (no extra packages)."""

import glob
import os
import socket
import time

PROC = "/proc"


def _read(path, default=""):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return default


class Stats:
    def __init__(self, proc=PROC, sys_root="/sys"):
        self.proc = proc
        self.sys = sys_root
        self._cpu_prev = None
        self._proc_prev = {}
        self._proc_time = None
        self._net_prev = None
        self._hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

    # -- CPU
    def _cpu_times(self):
        cores = {}
        for line in _read(os.path.join(self.proc, "stat")).splitlines():
            if line.startswith("cpu"):
                parts = line.split()
                vals = [int(x) for x in parts[1:9]]
                idle = vals[3] + vals[4]
                cores[parts[0]] = (sum(vals), idle)
        return cores

    def cpu(self):
        now = self._cpu_times()
        prev, self._cpu_prev = self._cpu_prev, now
        if not prev:
            return {"total": 0.0, "cores": [0.0] * max(0, len(now) - 1)}

        def pct(k):
            if k not in prev or k not in now:
                return 0.0
            dt = now[k][0] - prev[k][0]
            di = now[k][1] - prev[k][1]
            return round(100.0 * (dt - di) / dt, 1) if dt > 0 else 0.0
        cores = sorted((k for k in now if k != "cpu"), key=lambda k: int(k[3:]))
        return {"total": pct("cpu"), "cores": [pct(k) for k in cores]}

    # -- memory
    def memory(self):
        info = {}
        for line in _read(os.path.join(self.proc, "meminfo")).splitlines():
            k, _, v = line.partition(":")
            try:
                info[k] = int(v.split()[0]) * 1024
            except (ValueError, IndexError):
                pass
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        swap_t, swap_f = info.get("SwapTotal", 0), info.get("SwapFree", 0)
        return {"total": total, "used": total - avail, "swap_total": swap_t,
                "swap_used": swap_t - swap_f}

    # -- disk
    @staticmethod
    def disk(path="/"):
        try:
            st = os.statvfs(path)
        except OSError:
            return {"total": 0, "used": 0}
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return {"total": total, "used": total - free}

    # -- temperature, battery, uptime, load
    def temperature(self):
        for zone in sorted(glob.glob(os.path.join(self.sys, "class/thermal/thermal_zone*/temp"))):
            try:
                return round(int(_read(zone).strip()) / 1000.0, 1)
            except ValueError:
                continue
        return None

    def battery(self):
        for ps in sorted(glob.glob(os.path.join(self.sys, "class/power_supply/*"))):
            if _read(os.path.join(ps, "type")).strip() != "Battery":
                continue
            cap = _read(os.path.join(ps, "capacity")).strip()
            status = _read(os.path.join(ps, "status")).strip() or None
            try:
                return {"percent": int(cap), "status": status}
            except ValueError:
                continue
        return None

    def uptime(self):
        try:
            return int(float(_read(os.path.join(self.proc, "uptime")).split()[0]))
        except (ValueError, IndexError):
            return 0

    def load(self):
        try:
            return [float(x) for x in _read(os.path.join(self.proc, "loadavg")).split()[:3]]
        except ValueError:
            return [0.0, 0.0, 0.0]

    # -- network
    def network(self):
        now_t = time.monotonic()
        totals = {}
        for line in _read(os.path.join(self.proc, "net/dev")).splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            parts = rest.split()
            if name == "lo" or len(parts) < 9:
                continue
            totals[name] = (int(parts[0]), int(parts[8]))
        prev, self._net_prev = self._net_prev, (now_t, totals)
        out = []
        for name, (rx, tx) in sorted(totals.items()):
            rate_rx = rate_tx = 0
            if prev and name in prev[1] and now_t > prev[0]:
                dt = now_t - prev[0]
                rate_rx = max(0, int((rx - prev[1][name][0]) / dt))
                rate_tx = max(0, int((tx - prev[1][name][1]) / dt))
            out.append({"name": name, "rx": rx, "tx": tx, "rx_rate": rate_rx, "tx_rate": rate_tx})
        return out

    # -- processes
    def processes(self, limit=8):
        now_t = time.monotonic()
        cur = {}
        rows = []
        for d in os.listdir(self.proc):
            if not d.isdigit():
                continue
            stat = _read(os.path.join(self.proc, d, "stat"))
            if not stat:
                continue
            try:
                name = stat[stat.index("(") + 1:stat.rindex(")")]
                fields = stat[stat.rindex(")") + 2:].split()
                ticks = int(fields[11]) + int(fields[12])
                rss = int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
            except (ValueError, IndexError):
                continue
            cur[d] = ticks
            rows.append((d, name, ticks, rss))
        prev_t, prev = self._proc_time, self._proc_prev
        self._proc_time, self._proc_prev = now_t, cur
        out = []
        for pid, name, ticks, rss in rows:
            cpu = 0.0
            if prev_t and pid in prev and now_t > prev_t:
                cpu = 100.0 * (ticks - prev[pid]) / self._hz / (now_t - prev_t)
            out.append({"pid": int(pid), "name": name, "cpu": round(cpu, 1), "rss": rss})
        out.sort(key=lambda p: (p["cpu"], p["rss"]), reverse=True)
        return out[:limit]

    def snapshot(self):
        return {"hostname": socket.gethostname(), "cpu": self.cpu(), "memory": self.memory(),
                "disk": self.disk(), "temperature": self.temperature(), "battery": self.battery(),
                "uptime": self.uptime(), "load": self.load(), "network": self.network(),
                "processes": self.processes()}


def local_addresses():
    """IPv4 addresses of this device that a browser on the LAN could use."""
    import fcntl
    import struct
    out = []
    for name in sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else []:
        if name == "lo":
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                packed = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
            finally:
                s.close()
            out.append((name, socket.inet_ntoa(packed[20:24])))
        except OSError:
            continue
    # Prefer Wi-Fi/Ethernet over VPN tunnels for the address shown on screen.
    out.sort(key=lambda x: (0 if x[0].startswith(("wl", "en", "eth")) else 1, x[0]))
    return out
