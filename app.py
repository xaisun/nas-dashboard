#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NAS 监控看板 - 后端
纯 Python + Flask，读取 /proc、/sys、smartctl、Docker API 等采集主机指标
"""
import os
import re
import json
import time
import socket
import subprocess
import threading
import urllib.request
import urllib.parse
from datetime import datetime, date

from flask import Flask, jsonify, send_from_directory, request, Response

# ── cnlunar 黄历 ──
try:
    import cnlunar
    _HAS_CNLUNAR = True
except ImportError:
    _HAS_CNLUNAR = False
    cnlunar = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8904"))
WEATHER_CITY = os.environ.get("WEATHER_CITY", "")          # 可选: 指定城市, 留空自动识别
WEATHER_TTL = int(os.environ.get("WEATHER_TTL", "1800"))   # 天气缓存秒数
IP_TTL = int(os.environ.get("IP_TTL", "21600"))            # 公网IP缓存秒数

app = Flask(__name__, static_folder="static")

# ---------------------------------------------------------------- 工具
_cache = {}
_cache_lock = threading.Lock()

def cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (time.time(), val)
    return val

def read_file(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return default

def run_cmd(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# ---------------------------------------------------------------- CPU
def cpu_usage():
    def sample():
        lines = read_file("/proc/stat", "").splitlines()
        for ln in lines:
            if ln.startswith("cpu "):
                parts = [int(x) for x in ln.split()[1:]]
                idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
                return sum(parts), idle
        return 0, 0
    try:
        t1, i1 = sample()
        time.sleep(0.25)
        t2, i2 = sample()
        dt = t2 - t1
        if dt <= 0:
            return 0
        return round(100 * (1 - (i2 - i1) / dt), 1)
    except Exception:
        return 0

def cpu_model():
    txt = read_file("/proc/cpuinfo", "")
    for ln in txt.splitlines():
        if ln.lower().startswith("model name"):
            return ln.split(":", 1)[1].strip()
    return "未知"

def cpu_temp():
    best = None
    try:
        zones = os.listdir("/sys/class/thermal")
    except Exception:
        return None
    for z in zones:
        if not z.startswith("thermal_zone"):
            continue
        t = read_file(f"/sys/class/thermal/{z}/temp", "")
        if t.isdigit():
            v = int(t) // 1000
            if best is None or v > best:
                best = v
    return best

# ---------------------------------------------------------------- 内存
def memory():
    info = {}
    for ln in read_file("/proc/meminfo", "").splitlines():
        m = re.match(r"(\w+):\s+(\d+)", ln)
        if m:
            info[m.group(1)] = int(m.group(2)) * 1024
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = max(total - avail, 0)
    return {
        "total": total, "used": used, "free": avail,
        "percent": round(used / total * 100, 1) if total else 0,
    }

# ---------------------------------------------------------------- 负载/运行时间
def loadavg():
    parts = read_file("/proc/loadavg", "0 0 0").split()
    return [float(x) for x in parts[:3]]

def uptime():
    secs = float(read_file("/proc/uptime", "0").split()[0])
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d}天{h}时{m}分{s}秒"

# ---------------------------------------------------------------- 网络
_net_prev = {}

def default_iface():
    try:
        for ln in read_file("/proc/net/route", "").splitlines()[1:]:
            parts = ln.split()
            if len(parts) > 1 and parts[1] == "00000000":
                return parts[0]
    except Exception:
        pass
    return "eth0"

def net_rates():
    global _net_prev
    iface = default_iface()
    def sample():
        d = {}
        for ln in read_file("/proc/net/dev", "").splitlines():
            if ":" not in ln:
                continue
            name, rest = ln.split(":", 1)
            name = name.strip()
            vals = rest.split()
            d[name] = (int(vals[0]), int(vals[8]))  # rx, tx bytes
        return d
    now = sample()
    prev = _net_prev.get(iface)
    _net_prev[iface] = now
    rx = tx = 0.0
    if prev:
        cur = now.get(iface)
        prev_cur = prev.get(iface)
        if cur and prev_cur:
            dt = max(time.time() - _net_prev.get("_t", time.time()), 0.001)
            rx = max(cur[0] - prev_cur[0], 0) / dt
            tx = max(cur[1] - prev_cur[1], 0) / dt
    _net_prev["_t"] = time.time()
    speed = read_file(f"/sys/class/net/{iface}/speed", "")
    try:
        speed_mbps = int(speed)
    except Exception:
        speed_mbps = 0
    return {
        "iface": iface,
        "speed_mbps": speed_mbps,
        "speed_label": f"{speed_mbps/1000:.0f}G" if speed_mbps >= 1000 else f"{speed_mbps}M",
        "rx": rx, "tx": tx,
    }

# ---------------------------------------------------------------- 磁盘 IO
_disk_prev = {}
_io_lock = threading.Lock()

def disk_io():
    global _disk_prev
    def sample():
        d = {"read": 0, "write": 0}
        for ln in read_file("/proc/diskstats", "").splitlines():
            p = ln.split()
            if len(p) < 6:
                continue
            name = p[2]
            # 只统计物理盘, 跳过分区/loop/ram
            if name.startswith("nvme"):
                if not re.match(r"nvme\d+n\d+$", name):
                    continue
            elif re.search(r"\d$", name) or name.startswith(("loop", "ram", "fd")):
                continue
            try:
                d["read"] += int(p[5]) * 512
                d["write"] += int(p[9]) * 512
            except Exception:
                pass
        return d
    with _io_lock:
        now = sample()
        prev = _disk_prev
        _disk_prev = now
        if prev:
            dt = max(time.time() - prev.get("_t", time.time()), 0.001)
            rd = max(now["read"] - prev.get("read", 0), 0) / dt
            wr = max(now["write"] - prev.get("write", 0), 0) / dt
        else:
            rd = wr = 0.0
        _disk_prev["_t"] = time.time()
    return {"read": rd, "write": wr}

# ---------------------------------------------------------------- 硬盘(SMART)
SMARTDIR = "/var/lib/smartmontools"
CRITICAL_ATTRS = {5: "重映射扇区", 197: "待映射扇区", 198: "离线不可校正", 199: "UDMA CRC"}

def _smartd_info(dev):
    """从 udevadm 拿 ID_MODEL / ID_SERIAL_SHORT"""
    out = run_cmd(["udevadm", "info", "-q", "property", "-n", f"/dev/{dev}"], timeout=5)
    model, serial = "", ""
    for ln in out.splitlines():
        if ln.startswith("ID_MODEL="):
            model = ln.split("=", 1)[1]
        elif ln.startswith("ID_SERIAL_SHORT="):
            serial = ln.split("=", 1)[1]
    return model, serial

def smartd_read(serial):
    """从 smartd 的 attrlog/state 文件读取 (温度, 健康, 说明), 无需 root.
    返回: (temp_int|None, "OK"|"WARN"|None, detail|None)"""
    if not serial:
        return None, None, "无数据"
    try:
        files = os.listdir(SMARTDIR)
    except Exception:
        return None, None, "无数据"
    # 按序列号匹配 attrlog 文件(文件名: <model>-<serial>.ata.csv, smartd 会把 - 替换为 _)
    fname = None
    serial_norm = re.sub(r"[^A-Za-z0-9]", "_", serial)
    for f in files:
        if f.startswith("attrlog.") and f.endswith(".ata.csv"):
            stem = re.sub(r"[^A-Za-z0-9]", "_", f[len("attrlog."):-len(".ata.csv")])
            if stem.endswith(serial_norm):
                fname = f
                break
    if not fname:
        return None, None, "未监控"
    stem = fname[len("attrlog."):-len(".ata.csv")]
    try:
        with open(os.path.join(SMARTDIR, fname), "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().strip().splitlines()
        if not lines:
            return None, None, "无数据"
        parts = lines[-1].split(";")
        attrs = {}
        i = 1
        while i + 2 < len(parts):
            try:
                attrs[int(parts[i].strip())] = (int(parts[i + 1].strip()), int(parts[i + 2].strip()))
            except Exception:
                pass
            i += 3
    except Exception:
        return None, None, "无数据"
    # 温度: 属性194(部分盘190), 老盘 raw 带标志位, 取低字节
    temp = None
    for aid in (194, 190):
        if aid in attrs:
            temp = attrs[aid][1] & 0xFF
            break
    # 健康: 关键坏道属性(低16位为计数) + 自检错误
    bad = {}
    for k, name in CRITICAL_ATTRS.items():
        if k in attrs:
            cnt = attrs[k][1] & 0xFFFF
            if cnt > 0:
                bad[name] = cnt
    ste = 0
    try:
        with open(os.path.join(SMARTDIR, f"smartd.{stem}.ata.state"), "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                if ln.startswith("self-test-errors"):
                    ste = int(ln.split("=")[1].strip())
    except Exception:
        pass
    if bad or ste > 0:
        detail = " ".join(f"{k}{v}" for k, v in bad.items())
        if ste:
            detail = (detail + " " if detail else "") + f"自检错误{ste}"
        return temp, "WARN", detail
    return temp, "OK", None

def _smart_for(dev):
    """返回 (温度, 健康, 状态说明) 状态: None=正常, 无权限, 不支持, 无法读取"""
    out = run_cmd(["smartctl", "-n", "standby", "-j", "-H", "-A", f"/dev/{dev}"], timeout=10)
    if not out:
        return None, None, "无法读取"
    try:
        data = json.loads(out)
    except Exception:
        return None, None, "无法读取"
    msgs = " ".join(m.get("string", "") for m in data.get("smartctl", {}).get("messages", []))
    if "permission denied" in msgs.lower():
        return None, None, "无权限"
    health = None
    if "smart_status" in data:
        health = "OK" if data["smart_status"].get("passed") else "FAILED"
    temp = None
    try:
        temp = data["temperature"]["current"]
    except Exception:
        pass
    if health is None and temp is None:
        return None, None, "不支持"
    return temp, health, None

def _transport(name):
    """从 sysfs 判断磁盘传输方式: sata/usb/nvme"""
    try:
        link = os.readlink(f"/sys/block/{name}")
        if "usb" in link:
            return "usb"
        if "nvme" in link:
            return "nvme"
        if "ata" in link:
            return "sata"
    except Exception:
        pass
    return ""

def disks():
    out = run_cmd(["lsblk", "-d", "-J", "-o", "NAME,SIZE,MODEL,ROTA,TYPE"], timeout=8)
    items = []
    if not out:
        return items
    try:
        data = json.loads(out)
    except Exception:
        return items
    for b in data.get("blockdevices", []):
        name = b.get("name", "")
        if not name or b.get("type") == "loop" or name.startswith("ram"):
            continue
        rota = b.get("rota")
        if name.startswith("nvme"):
            kind = "NVMe SSD"
        elif rota == 0:
            kind = "SSD"
        else:
            kind = "机械硬盘"
        usb = _transport(name) == "usb"
        item = {
            "name": name,
            "size": b.get("size", "—"),
            "model": b.get("model", "").strip() or "未知",
            "type": kind + ("·USB" if usb else ""),
            "temp": None,
            "health": None,
            "smart": None,
        }
        # 优先 smartd 数据(无需root); 缺失时回退 smartctl(Docker 特权环境可用)
        imodel, iserial = _smartd_info(name)
        t, h, note = smartd_read(iserial)
        if h:
            item["temp"] = t
            item["health"] = h
            item["smart"] = note
        else:
            t2, h2, s2 = _smart_for(name)
            if h2:
                item["temp"] = t2
                item["health"] = h2
                item["smart"] = s2
            else:
                item["smart"] = note  # 保留 smartd 的真实说明(未监控/无数据)
        items.append(item)
    return items

# ---------------------------------------------------------------- CPU 温度历史(最近 1 小时)
_temp_hist = []  # [(ts, temp_c), ...] 每分钟一点, 保留最近 61 点

def record_temp_history():
    """由 metrics() 每次调用触发, 间隔>=60s 追加一个温度点(环形 61 点)。"""
    try:
        t = cpu_temp()
        if t is None:
            return
        now = time.time()
        if not _temp_hist or now - _temp_hist[-1][0] >= 60:
            _temp_hist.append((now, t))
            if len(_temp_hist) > 61:
                del _temp_hist[0:len(_temp_hist) - 61]
    except Exception:
        pass

def temp_history():
    return [{"t": int(ts), "v": v} for ts, v in _temp_hist]

# ---------------------------------------------------------------- CPU 实时功耗 (RAPL)
_rapl = {"t": 0.0, "e": 0}

def cpu_power_w():
    """RAPL package-0 实时功耗(瓦): 相邻两次采样的能量差 / 时间差。
    Intel N150 支持 intel-rapl:0, 容器 privileged root 可读 energy_uj。
    首次采样无历史返回 None, 之后每次调用平滑更新。"""
    try:
        with open("/sys/class/powercap/intel-rapl:0/energy_uj") as f:
            e = int(f.read().strip())
        now = time.time()
        if _rapl["t"] and now > _rapl["t"]:
            dt = now - _rapl["t"]
            de = e - _rapl["e"]
            if de < 0:  # 64 位计数器回绕(实际数千年才一次)
                de += 1 << 64
            w = de / dt / 1e6
            _rapl["t"] = now
            _rapl["e"] = e
            return round(w, 1)
        _rapl["t"] = now
        _rapl["e"] = e
        return None
    except Exception:
        return None

# ---------------------------------------------------------------- 存储空间(按硬盘聚合)
def storage():
    """每块物理硬盘 = 一个存储空间: 统计其名下所有已挂载卷的用量

    容器跑在自己的 mount namespace，看不到宿主 TRIM 的 btrfs 卷挂载
    (mdadm+LVM+btrfs, 设备 /dev/mapper/trim_xxx-0, 挂载点 /volN)，也看不到宿主盘的真实用量。
    容量(used/percent)由宿主侧 host_mounts_gen.py 负责采集(只读 /proc/diskstats 判断活跃、
    仅对活跃盘 df、休眠盘沿用缓存，绝不唤醒)，经整目录 bind 暴露为 /app/host_mounts.json。
    本函数只消费该 json，并叠加「硬盘状态」卡片被动推断的电源状态(active/standby)。
    """
    dmap = {x["name"]: x for x in cached("disks", 60, disks)}  # 复用硬盘状态卡片真实数据(被动, smartctl -n standby 不唤醒休眠盘)
    hm = None
    try:
        with open("/app/host_mounts.json", encoding="utf-8") as f:
            hm = json.load(f)
    except Exception:
        hm = None
    # 容器内 lsblk 补充盘类型信息(SSD/机械/USB)
    meta = {}
    out = run_cmd(["lsblk", "-d", "-J", "-o", "NAME,ROTA,TYPE"], timeout=8)
    if out:
        try:
            for b in json.loads(out).get("blockdevices", []):
                meta[b["name"]] = b
        except Exception:
            pass
    # root 侧真实电源/用量(可选, 由 disk_power_probe.sh 以 root 写入 /app/disk_power.json)
    dp = {}
    try:
        with open("/app/disk_power.json", encoding="utf-8") as f:
            for x in json.load(f):
                dp[x.get("name")] = x
    except Exception:
        dp = {}
    if hm:
        result = []
        for it in hm:
            name = it.get("name", "")
            ssd = it.get("ssd", False)
            d = dp.get(name, {})
            if ssd:
                power = "ssd"
            else:
                di = dmap.get(name, {})
                # 电源状态以「硬盘状态」卡片真实数据为准: 有温度/健康=smartd 成功读到=活跃;
                # 休眠盘 smartd(-n standby)跳过不显示=休眠。被动、不唤醒盘。若用户跑了 root 探测器则优先。
                has = (di.get("temp") is not None or di.get("health") is not None)
                power = d.get("power") or ("active" if has else "standby")
            result.append({
                "name": name,
                "type": ("SSD" if ssd else "机械硬盘") + ("·USB" if _transport(name) == "usb" else ""),
                "size": it.get("size", 0),
                "used": it.get("used"),
                "percent": it.get("percent"),
                "volumes": it.get("volumes", "未挂载"),
                "raid": it.get("raid"),
                "ssd": ssd,
                "power": power,
            })
        return result
    # ---- fallback(仅当 host_mounts.json 缺失): 只读 lsblk, 不 df(避免唤醒休眠盘) ----
    out = run_cmd(["lsblk", "-b", "-J", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,ROTA"], timeout=8)
    if not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    skip_mnt = {"/boot", "/boot/efi"}
    result = []
    def collect_mounts(children):
        vols = []
        for x in children:
            for m in (x.get("mountpoints") or []):
                if m and m not in skip_mnt:
                    vols.append("系统盘" if m == "/" else m)
            vols += collect_mounts(x.get("children") or [])
        return vols
    for b in data.get("blockdevices", []):
        if b.get("type") != "disk" or b.get("name", "").startswith(("loop", "ram")):
            continue
        name = b["name"]
        size = b.get("size", 0)
        rota = b.get("rota")
        ssd = (rota == 0)
        vols = collect_mounts([b])
        result.append({
            "name": name,
            "type": ("SSD" if ssd else "机械硬盘") + ("·USB" if _transport(name) == "usb" else ""),
            "size": size,
            "used": None,
            "percent": None,
            "volumes": " + ".join(vols) if vols else "未挂载",
            "raid": None,
            "ssd": ssd,
            "power": "ssd" if ssd else ("active" if (dmap.get(name, {}).get("temp") is not None or dmap.get(name, {}).get("health") is not None) else "standby"),
        })
    return result

# ---------------------------------------------------------------- 容器
def _host_ports(c):
    """提取容器映射到宿主的端口列表(降序), 供前端生成可点击链接"""
    out = []
    for bindings in (getattr(c, "ports", None) or {}).values():
        if isinstance(bindings, list):
            for b in bindings:
                if isinstance(b, dict) and b.get("HostPort"):
                    try: out.append(int(b["HostPort"]))
                    except Exception: pass
    return sorted(set(out), reverse=True)

def containers():
    """优先 Docker API; 无权限时回退 cgroup 进程级统计(无需root)"""
    try:
        import docker
        client = docker.from_env()
        all_c = client.containers.list(all=True)
        running = [c for c in all_c if c.status == "running"]
        return {
            "ok": True,
            "source": "docker",
            "running": len(running),
            "total": len(all_c),
            "list": [{"name": c.name, "state": c.status, "cmd": "", "ports": _host_ports(c)} for c in all_c],
        }
    except Exception as e:
        return _containers_from_cgroup(str(e))

def _pid_ports(pid):
    """从容器进程自己的 /proc/<pid>/net 提取监听端口(容器独立网络命名空间, 无需root)"""
    ports = set()
    for f in (f"/proc/{pid}/net/tcp", f"/proc/{pid}/net/tcp6"):
        try:
            with open(f, "r", errors="ignore") as fh:
                for ln in fh.readlines()[1:]:
                    parts = ln.split()
                    if len(parts) > 3 and parts[3] == "0A":  # LISTEN
                        try:
                            port = int(parts[1].rsplit(":", 1)[1], 16)
                        except Exception:
                            continue
                        if 0 < port < 65536:
                            ports.add(port)
        except Exception:
            pass
    return sorted(ports, reverse=True)[:8]  # 高位端口更有辨识度, 优先显示

def _containers_from_cgroup(err):
    """从 cgroup scope + /proc 获取运行中容器(进程级信息)"""
    try:
        scopes = [d for d in os.listdir("/sys/fs/cgroup/system.slice")
                  if d.startswith("docker-") and d.endswith(".scope")]
    except Exception:
        scopes = []
    items = []
    for s in sorted(scopes):
        cid = s[len("docker-"):-len(".scope")]
        procs = []
        try:
            with open(f"/sys/fs/cgroup/system.slice/{s}/cgroup.procs") as f:
                procs = sorted(int(x) for x in f.read().split())
        except Exception:
            pass
        cmd, main_pid, ports = "", None, []
        for pid in procs[:1]:  # 最小 PID ≈ 容器主进程
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore").strip()[:70]
                main_pid = pid
                ports = _pid_ports(pid)
            except Exception:
                pass
        label = cmd if cmd else cid[:12]
        items.append({"id": cid[:12], "cmd": cmd, "ports": ports, "label": label})
    # 去重: 多个容器共见的端口 = host 网络模式的宿主公共端口, 只保留独有端口
    from collections import Counter
    cnt = Counter(p for it in items for p in it["ports"])
    for it in items:
        it["ports"] = [p for p in it["ports"] if cnt[p] < 2][:8]
    return {
        "ok": True,
        "source": "cgroup",
        "running": len(items),
        "total": None,
        "list": [{"name": x["label"], "state": "running", "cmd": x["cmd"], "ports": x["ports"], "id": x["id"]} for x in items],
        "note": "Docker 套接字无权限, 仅显示运行中容器进程; 部署后显示完整名称",
        "error": err[:80],
    }

# ---------------------------------------------------------------- 天气
# 数据源: Open-Meteo (免费/无需 key/按经纬度/稳定). 旧 wttr.in 免费接口常被限速或缓存串味,
#   同坐标(22.28,113.58)竟返回 22°C 错误气温(真实珠海 28.8°C), 故弃用.
# 前端 W_ICONS / W_DESC 以 wttr 天气码为 key, 这里把 Open-Meteo 的 WMO 码映射成等价 wttr 码, 前端零改动.
WMO2WTTR = {0:113,1:113,2:116,3:122,45:143,48:143,
            51:266,53:266,55:266,56:266,57:266,
            61:296,63:299,65:302,66:311,67:314,
            71:326,73:329,75:332,77:326,
            80:353,81:356,82:359,85:368,86:371,
            95:386,96:389,99:389}
WMO_DESC = {0:"晴",1:"晴",2:"晴间多云",3:"阴",45:"雾",48:"雾",
            51:"小毛毛雨",53:"毛毛雨",55:"毛毛雨",56:"冻毛毛雨",57:"冻毛毛雨",
            61:"小雨",63:"中雨",65:"大雨",66:"冻雨",67:"冻雨",
            71:"小雪",73:"中雪",75:"大雪",77:"雪粒",
            80:"阵雨",81:"阵雨",82:"强阵雨",85:"阵雪",86:"强阵雪",
            95:"雷阵雨",96:"雷阵雨",99:"雷阵雨"}
DEFAULT_COORD = "22.27,113.57"   # 珠海兜底坐标(公网IP定位失败时用)
DEFAULT_CITY = "珠海市"

def _detect_location():
    """返回 (城市名, "lat,lon" 坐标串). 按公网IP经 ip-api 定位, 失败回退珠海."""
    try:
        req = urllib.request.Request("http://ip-api.com/json?lang=zh-CN", headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("status") == "success":
            city = data.get("city") or ""
            lat = data.get("lat"); lon = data.get("lon")
            coord = f"{lat},{lon}" if lat is not None and lon is not None else ""
            return city, coord
    except Exception:
        pass
    return "", ""

def _resolve_location():
    """解析最终 (显示城市名, 坐标串).
       WEATHER_CITY 环境变量: 填 'lat,lon' 强制坐标; 填普通城市名仅覆盖显示名; 留空自动识别."""
    env = (WEATHER_CITY or "").strip()
    name, coord = cached("loc", IP_TTL, _detect_location)
    if not coord:
        coord = DEFAULT_COORD; name = name or DEFAULT_CITY
    if re.match(r'^-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?$', env):
        coord = env                      # 显式坐标覆盖
    elif env:
        name = env                       # 仅覆盖显示名, 坐标仍自动
    return name, coord

def weather():
    city_name, coord = _resolve_location()
    if not coord or "," not in coord:
        return {"ok": False, "error": "无法识别位置"}
    lat, lon = coord.split(",")
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min"
           "&timezone=Asia%%2FShanghai&forecast_days=4" % (lat.strip(), lon.strip()))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        cur = data.get("current", {})
        wmo = int(cur.get("weather_code", -1))
        wcode = WMO2WTTR.get(wmo, 122)
        days = []
        daily = data.get("daily", {})
        dtimes = daily.get("time", [])
        dcodes = daily.get("weather_code", [])
        dmax = daily.get("temperature_2m_max", [])
        dmin = daily.get("temperature_2m_min", [])
        for i in range(min(3, len(dtimes))):
            dwmo = int(dcodes[i]) if i < len(dcodes) else -1
            days.append({
                "date": dtimes[i],
                "desc": WMO_DESC.get(dwmo, ""),
                "tmax": dmax[i] if i < len(dmax) else "",
                "tmin": dmin[i] if i < len(dmin) else "",
                "code": WMO2WTTR.get(dwmo, 122),
            })
        return {
            "ok": True,
            "city": city_name or coord,
            "temp": cur.get("temperature_2m", "—"),
            "desc": WMO_DESC.get(wmo, ""),
            "code": wcode,
            "humidity": cur.get("relative_humidity_2m", "—"),
            "wind": cur.get("wind_speed_10m", "—"),
            "days": days,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------------------------------------------------------------- 公网IP
def public_ip():
    try:
        req = urllib.request.Request("http://ip-api.com/json?lang=zh-CN", headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("status") == "success":
            return data.get("query", "—")
    except Exception:
        pass
    return "—"

# ---------------------------------------------------------------- 农历/干支
GAN = "甲乙丙丁戊己庚辛壬癸"
ZHI = "子丑寅卯辰巳午未申酉戌亥"
SHENGXIAO = "鼠牛虎兔龙蛇马羊猴鸡狗猪"
MONTHS = ["正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊"]
DAYS = ["初一","初二","初三","初四","初五","初六","初七","初八","初九","初十",
        "十一","十二","十三","十四","十五","十六","十七","十八","十九","二十",
        "廿一","廿二","廿三","廿四","廿五","廿六","廿七","廿八","廿九","三十"]

# ── 黄历 (cnlunar) ──
def almanac():
    """返回完整黄历: 宜忌/冲煞/十二神/二十八宿/吉凶等级"""
    if not _HAS_CNLUNAR:
        return {"ok": False, "error": "cnlunar 未安装"}
    try:
        a = cnlunar.Lunar(datetime.now(), godType="8char")
        items = a.angelDemon  # ((吉神, 凶神), (宜, 忌))
        good_things = items[1][0] if items and len(items) > 1 else []
        bad_things = items[1][1] if items and len(items) > 1 else []
        month = a.lunarMonthCn
        return {
            "ok": True,
            "lunar": f"农历{a.lunarMonthCn.replace('小','').replace('大','')}{a.lunarDayCn}",
            "gz_year": a.year8Char,
            "gz_month": a.month8Char,
            "gz_day": a.day8Char,
            "zodiac": a.chineseYearZodiac,
            "clash": a.chineseZodiacClash,
            "star28": a.today28Star,
            "day_officer": getattr(a, "today12DayOfficer", ""),
            "day_god": getattr(a, "today12DayGod", ""),
            "level": a.todayLevelName[:12] if a.todayLevelName else "",
            "good": good_things[:6],
            "bad": bad_things[:6],
            "solar_term": a.todaySolarTerms if getattr(a, "todaySolarTerms", "") != "无" else "",
            "next_term": f"{a.nextSolarTermDate[0]}/{a.nextSolarTermDate[1]} {a.nextSolarTerm}" if a.nextSolarTerm else "",
            "star_zodiac": a.starZodiac,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def lunar():
    try:
        if _HAS_CNLUNAR:
            # 优先用 cnlunar(容器内已装), 结果如: 丙午年六月廿八
            a = cnlunar.Lunar(datetime.now(), godType="8char")
            month = a.lunarMonthCn.replace("大", "").replace("小", "")
            leap = "闰" if a.isLunarLeapMonth else ""
            return f"{a.year8Char}年{leap}{month}{a.lunarDayCn}"
        from zhdate import ZhDate
        z = ZhDate.from_datetime(datetime.now())
        year = z.lunar_year
        idx = (year - 4) % 60
        ganzhi = GAN[idx % 10] + ZHI[idx % 12]
        shengxiao = SHENGXIAO[idx % 12]
        month = MONTHS[z.lunar_month - 1] if 1 <= z.lunar_month <= 12 else str(z.lunar_month)
        day = DAYS[z.lunar_day - 1] if 1 <= z.lunar_day <= 30 else str(z.lunar_day)
        leap = "闰" if getattr(z, "is_leap_month", False) else ""
        return f"{ganzhi}年{leap}{month}月{day}"
    except Exception:
        return "农历—"

# ---------------------------------------------------------------- API

def _weather_cached():
    """天气获取成功才缓存, 失败不缓存(下次请求重试)"""
    w = weather()
    if w.get("ok"):
        return w
    raise RuntimeError(w.get("error", "获取失败"))

def _public_ip_cached():
    ip = public_ip()
    if ip != "—":
        return ip
    raise RuntimeError("获取失败")

@app.route("/")
def index():
    resp = send_from_directory(BASE_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/api/metrics")
def api_metrics():
    return jsonify(metrics())

@app.route("/api/cpu_history")
def api_cpu_history():
    return jsonify(temp_history())

def metrics():
    # 整体 1 秒缓存: SSE 每 1 秒推一次, 这里与之对齐, CPU 每秒完整算一次(实时性优先)
    return cached("metrics_full", 1, _metrics_compute)

def _metrics_compute():
    record_temp_history()
    try:
        disks_list = cached("disks", 60, disks)
    except Exception:
        disks_list = []
    try:
        st = cached("storage", 15, storage)
    except Exception:
        st = []
    try:
        w = cached("weather", WEATHER_TTL, _weather_cached)
    except Exception:
        w = {"ok": False, "error": "天气获取失败"}
    try:
        pip_ = cached("public_ip", IP_TTL, _public_ip_cached)
    except Exception:
        pip_ = "—"
    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": "星期" + "一二三四五六日"[datetime.now().weekday()],
        "lunar": lunar(),
        "hostname": socket.gethostname(),
        "node_id": os.environ.get("NODE_ID", "NODE-001"),
        "local_ip": get_local_ip(),
        "public_ip": pip_,
        "cpu": {
            "usage": cpu_usage(),
            "model": cpu_model(),
            "temp": cpu_temp(),
            "loadavg": loadavg(),
            "power_w": cpu_power_w(),
        },
        "mem": memory(),
        "uptime": uptime(),
        "net": net_rates(),
        "diskio": disk_io(),
        "containers": containers(),
        "storage": st,
        "disks": disks_list,
        "weather": w,
        "almanac": cached("almanac", 300, almanac),
    }

@app.route("/api/stream")
def api_stream():
    """SSE 实时推送: 每 1 秒向浏览器推一份完整指标(metrics 自身带 1s 缓存)"""
    def gen():
        while True:
            try:
                payload = json.dumps(metrics(), ensure_ascii=False)
                yield "data: " + payload + "\n\n"
            except Exception as e:
                yield "event: error\ndata: " + json.dumps({"msg": str(e)}, ensure_ascii=False) + "\n\n"
            time.sleep(1)
    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


# ─────────────────────────────────────────────────────────────
# DSH Token 用量聚合 Hub（汇聚 本机 / NAS / 公网 的 token 数据）
# ─────────────────────────────────────────────────────────────
import base64 as _b64
# 凭据一律从环境变量读取，仓库内不保存任何密钥 / 账号
_HUB_KEY = os.environ.get("TOKEN_HUB_KEY", "")                  # 汇聚写入密钥；留空则关闭 /api/token/push
_DSH_NAS_URL = os.environ.get("DSH_SESSION_URL", "http://127.0.0.1:8080/api/session.list")
_DSH_NAS_BASE = os.environ.get("DSH_API_BASE", "http://127.0.0.1:8080/api/")
_DSH_NAS_AUTH = os.environ.get("DSH_BASIC_AUTH", "")            # 形如 "Basic xxxx"；留空则不带鉴权头
_LOCAL_CACHE = {"items": [], "client": "\u672c\u673a \u00b7 \u684c\u9762/Web", "updatedAt": 0}
_LOCAL_CACHE_LOCK = threading.Lock()
_LOCAL_CACHE_FILE = os.path.join(BASE_DIR, ".token_local_cache.json")


def _hub_ok():
    """汇聚密钥校验：未配置 TOKEN_HUB_KEY 时一律拒绝（Token Hub 视为关闭）。"""
    if not _HUB_KEY:
        return False
    return (request.args.get("k") or request.headers.get("x-hub-key")) == _HUB_KEY


def _load_local_cache():
    try:
        with open(_LOCAL_CACHE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        _LOCAL_CACHE["items"] = d.get("items", [])
        _LOCAL_CACHE["client"] = d.get("client", "\u672c\u673a \u00b7 \u684c\u9762/Web")
        _LOCAL_CACHE["updatedAt"] = d.get("updatedAt", 0)
    except Exception:
        pass


def _save_local_cache():
    try:
        with open(_LOCAL_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"items": _LOCAL_CACHE["items"], "client": _LOCAL_CACHE["client"],
                       "updatedAt": _LOCAL_CACHE["updatedAt"]}, f, ensure_ascii=False)
    except Exception:
        pass


_load_local_cache()


def _dsh_sessionlist(url, auth=None, timeout=6):
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"type": "client-request", "rpcId": str(int(time.time() * 1000)),
                             "method": "session.list", "payload": {}}).encode(),
            headers={"content-type": "application/json"})
        if auth:
            req.add_header("authorization", auth)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        if data and data.get("result", {}).get("ok"):
            return data["result"]["value"].get("items", [])
    except Exception as e:
        print("[token-hub] fetch failed:", url, e)
    return None


def _dsh_session_model(sid, auth):
    """调 DSH 的 session.models RPC，取该会话当前使用的模型(current.model)。
    覆盖所有会话(含未落盘转录)，比扫 zstd 转录可靠。失败返回 None。"""
    if not sid:
        return None
    try:
        req = urllib.request.Request(
            _DSH_NAS_BASE + "session.models",
            data=json.dumps({"type": "client-request", "rpcId": str(int(time.time() * 1000)),
                             "method": "session.models", "payload": {"sessionId": sid}}).encode(),
            headers={"content-type": "application/json"})
        if auth:
            req.add_header("authorization", auth)
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read().decode())
        cur = (d.get("result", {}).get("value", {}) or {}).get("current") or {}
        return cur.get("model")
    except Exception:
        return None


_NAS_MODEL_TTL = 120
def _nas_model_map():
    """缓存版：遍历 NAS 本机会话，逐个调 session.models 拿 sessionId->model。
    120s 刷新一次，避免每次聚合都打 19 次 RPC。"""
    now = time.time()
    with _cache_lock:
        c = _cache.get("nas_model_map")
        if c and now - c[0] < _NAS_MODEL_TTL:
            return c[1]
    out = {}
    try:
        items = _dsh_sessionlist(_DSH_NAS_URL, _DSH_NAS_AUTH) or []
        for it in items:
            sid = it.get("sessionId")
            if not sid:
                continue
            m = _dsh_session_model(sid, _DSH_NAS_AUTH)
            if m:
                out[sid] = m
        # 兜底：若本进程能读到 DSH 转录(同机同用户)，合并 zstd 扫描结果
        try:
            out.update(_dsh_model_map())
        except Exception:
            pass
    except Exception:
        pass
    with _cache_lock:
        _cache["nas_model_map"] = (now, out)
    return out


def _dsh_model_map():
    """扫描本机 DSH 会话转录，提取 sessionId->model。
    仅在 hub 进程能读到 DSH 会话文件时生效(如 NAS 与 DSH 同机同用户/同挂载)。
    DSH 不通过 session.list API 暴露 model，只能从 zstd 转录里提取；
    会话存储位置因版本而异，故扫描多个候选根。读不到则返回 {}，
    由 local_pusher 在客户端侧补全本地实例的 model。"""
    now = time.time()
    with _cache_lock:
        c = _cache.get("dsh_model_map")
        if c and now - c[0] < 60:
            return c[1]
    out = {}
    try:
        import zstandard as _zstd
    except Exception:
        return out
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, ".dsh", "sessions"),
        os.path.join(home, "AppData", "Roaming", "deepseek-harness-desktop"),
        os.path.join(home, "AppData", "Local", "DeepSeekHarness"),
    ]
    try:
        _ws = json.load(open(os.path.join(home, ".dsh", "storages", "workspace.json"), encoding="utf-8"))
        for _w in _ws.get("tables", {}).get("workspaces", {}).values():
            _p = _w.get("path")
            if _p and os.path.isdir(_p):
                roots.append(_p)
                roots.append(os.path.join(_p, "sessions"))
    except Exception:
        pass
    seen = set()
    dctx = _zstd.ZstdDecompressor()
    try:
        for root in roots:
            if not root or root in seen or not os.path.isdir(root):
                continue
            seen.add(root)
            for dp, _, fns in os.walk(root):
                for fn in fns:
                    if not fn.endswith(".jsonl.zstd"):
                        continue
                    sid = os.path.basename(dp)          # 会话目录名即 sessionId
                    if sid in out:
                        continue
                    try:
                        with open(os.path.join(dp, fn), "rb") as f:
                            txt = dctx.stream_reader(f).read().decode("utf-8", "replace")
                        m = re.search(r'"model"\s*:\s*"([^"]+)"', txt[:500000])
                        if m:
                            out[sid] = m.group(1)
                    except Exception:
                        pass
    except Exception:
        pass
    with _cache_lock:
        _cache["dsh_model_map"] = (now, out)
    return out


def _norm_item(it, model_map=None):
    v = (it.get("projections") or {}).get("values") or {}
    tu = v.get("tokenUsage") or {}
    model_map = model_map or {}
    sid = it.get("sessionId")
    return {
        "sessionId": sid,
        "running": bool(it.get("running")),
        "title": v.get("title") or (sid or "")[:22],
        "uncachedInputTokens": tu.get("uncachedInputTokens") or 0,
        "cacheReadTokens": tu.get("cacheReadTokens") or 0,
        "cacheWriteTokens": tu.get("cacheWriteTokens") or 0,
        "outputTokens": tu.get("outputTokens") or 0,
        "costCents": tu.get("costCents"),
        "costPeakCents": tu.get("costPeakCents") or 0,
        "costOffpeakCents": tu.get("costOffpeakCents") or 0,
        "ssTurns": (v.get("sessionStats") or {}).get("turns") or 0,
        "ssSteps": (v.get("sessionStats") or {}).get("steps") or 0,
        "ssLlmMs": (v.get("sessionStats") or {}).get("llmMs") or 0,
        "ssToolMs": (v.get("sessionStats") or {}).get("toolMs") or 0,
        "ssTtftMs": (v.get("sessionStats") or {}).get("ttftMs") or 0,
        "ssTtftSteps": (v.get("sessionStats") or {}).get("ttftSteps") or 0,
        "ssDecodeMs": (v.get("sessionStats") or {}).get("decodeMs") or 0,
        "ssDecodeTokens": (v.get("sessionStats") or {}).get("decodeTokens") or 0,
        "lastOutputAt": it.get("updatedAt") or 0,                       # 最后输出/活动时间(ms)
        "model": model_map.get(sid) or it.get("model") or None,         # 使用模型(客户端或 hub 补全)
    }


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, x-hub-key"
    resp.headers["Access-Control-Max-Age"] = "600"
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


@app.route("/token")
def token_dashboard():
    return send_from_directory(BASE_DIR, "token-dashboard.html")


@app.route("/api/token/aggregate", methods=["GET", "OPTIONS"])
def api_token_aggregate():
    if request.method == "OPTIONS":
        return _cors(Response("", 204))          # 预检不带密钥，必须放行
    if not _hub_ok():
        return _cors(Response(json.dumps({"ok": False, "error": "unauthorized"}, ensure_ascii=False), mimetype="application/json")), 403
    nas_items = _dsh_sessionlist(_DSH_NAS_URL, _DSH_NAS_AUTH)
    nas_model_map = _nas_model_map()      # 经 session.models RPC 补全 NAS 会话的 model
    instances = []
    if nas_items is not None:
        instances.append({"key": "nas", "name": "NAS \u00b7 \u624b\u673a/Web/\u516c\u7f51", "online": True,
                          "updatedAt": int(time.time() * 1000),
                          "items": [_norm_item(i, nas_model_map) for i in nas_items]})
    else:
        instances.append({"key": "nas", "name": "NAS \u00b7 \u624b\u673a/Web/\u516c\u7f51", "online": False,
                          "updatedAt": int(time.time() * 1000), "items": []})
    with _LOCAL_CACHE_LOCK:
        loc = dict(_LOCAL_CACHE)
    if loc.get("items"):
        instances.append({"key": "local", "name": loc.get("client", "\u672c\u673a \u00b7 \u684c\u9762/Web"), "online": True,
                          "updatedAt": loc.get("updatedAt", 0),
                          "items": [_norm_item(i) for i in loc["items"]],
                          "stale": (time.time() * 1000 - loc.get("updatedAt", 0)) > 5 * 60 * 1000})
    return _cors(Response(json.dumps({"instances": instances}, ensure_ascii=False), mimetype="application/json"))


@app.route("/api/token/push", methods=["POST", "OPTIONS"])
def api_token_push():
    if request.method == "OPTIONS":
        return _cors(Response("", 204))
    if not _hub_ok():                            # 未配置密钥时一律拒绝写入
        return _cors(Response(json.dumps({"ok": False, "error": "unauthorized"}, ensure_ascii=False), mimetype="application/json")), 403
    try:
        body = request.get_json(force=True, silent=True) or {}
        items = body.get("items") or []
        client = body.get("client") or "\u672c\u673a \u00b7 \u684c\u9762/Web"
        with _LOCAL_CACHE_LOCK:
            _LOCAL_CACHE["items"] = items
            _LOCAL_CACHE["client"] = client
            _LOCAL_CACHE["updatedAt"] = int(time.time() * 1000)
        _save_local_cache()
        return _cors(Response(json.dumps({"ok": True, "cached": len(items)}, ensure_ascii=False), mimetype="application/json"))
    except Exception as e:
        return _cors(Response(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), mimetype="application/json")), 400


if __name__ == "__main__":
    # 热更新看门狗: 配合宿主机 bind-mount, app.py 被修改后自动重启
    # 容器内直接退出由 docker restart 策略拉起(全新进程, 无旧 socket 冲突)
    _app_file = os.path.abspath(__file__)
    _last_mtime = os.stat(_app_file).st_mtime
    _in_docker = os.path.exists("/.dockerenv")

    def _watchdog():
        global _last_mtime
        while True:
            time.sleep(2)
            try:
                m = os.stat(_app_file).st_mtime
                if m != _last_mtime:
                    _last_mtime = m
                    time.sleep(0.5)
                    print("[热更新] app.py 已变更, 重启中...")
                    if _in_docker:
                        os._exit(0)  # 容器内: 退出后由 docker restart 策略拉起
                    else:
                        import sys as _s
                        os.execv(_s.executable, [_s.executable] + _s.argv)
            except Exception:
                pass

    threading.Thread(target=_watchdog, daemon=True).start()
    print(f"NAS 看板已启动: http://0.0.0.0:{PORT} (热更新已开启)")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
