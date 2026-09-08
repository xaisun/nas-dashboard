#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿主侧卷挂载快照生成器（TRIM NAS）— 容量采集 · 休眠安全版

设计目标: 让看板「存储空间」始终显示每块盘的容量(总大小 + 已用 + 百分比),
且绝不因采集而唤醒休眠的机械盘。

采集策略(全在宿主侧, sunxingtao 普通用户即可运行, 不碰 root):
  - lsblk 只读 /sys, 拿盘树 / 总容量 / 是否 SSD(ROTA) / 挂载点, 不触碰盘上数据, 不唤醒。
  - 电源状态(SSD 恒常驻; 机械盘 unknown, 真实 active/standby 由看板复用「硬盘状态」卡片推断)。
  - 用量(used/percent):
      * 读 /proc/diskstats(内核纯内存统计, 读它不向盘发任何 ATA 命令, 不唤醒) 取每块盘累计读写扇区数;
      * 与上次运行记录比较: 扇区数变化 => 盘近期有 IO => 处于活跃(醒着) => 对其挂载点 df 取真实用量;
      * 扇区数未变 => 盘空闲/已休眠 => 不 df(避免唤醒), 沿用 host_mounts.json 中上次缓存的用量。
      * SSD 永远活跃, 始终 df。
    这样: 活跃盘容量实时准确; 休眠盘显示上次活跃时的用量(容量本就变化极慢), 且永不被采集唤醒。

输出 host_mounts.json: [{name, size, ssd, volumes, power, used, percent}]
另写 host_mounts.state(隐藏) 保存上次 diskstats 扇区数, 供下次比较。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "host_mounts.json")
STATE = os.path.join(HERE, "host_mounts.state")
SKIP = {"/boot", "/boot/efi"}


def run(args, timeout=10):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.stdout
    except Exception:
        return ""


def disk_sectors():
    """返回 {disk: total_sectors(read+write)} 来自 /proc/diskstats(不唤醒盘)。"""
    res = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 11:
                    continue
                dev = parts[2]
                # 仅整盘(无数字后缀), 跳过分区
                if dev[-1].isdigit():
                    continue
                try:
                    rsect = int(parts[5])
                    wsect = int(parts[9])
                except Exception:
                    continue
                res[dev] = rsect + wsect
    except Exception:
        pass
    return res


def df_usage(mountpoints):
    """对一组挂载点 df, 返回 (used, percent) 或 (None, None)。"""
    used = 0
    tot = 0
    ok = False
    for mp in mountpoints:
        o = run(["df", "-B1", mp], timeout=8)
        lines = o.splitlines()
        if len(lines) < 2:
            continue
        parts = lines[1].split()
        # df -B1: Filesystem 1B-blocks Used Available Use% Mounted
        if len(parts) < 6:
            continue
        try:
            tot += int(parts[1])
            used += int(parts[2])
            ok = True
        except Exception:
            pass
    if ok and tot:
        return used, round(used / tot * 100, 1)
    return None, None


def main():
    out = run(["lsblk", "-b", "-J", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,ROTA"])
    if not out:
        print("lsblk failed")
        sys.exit(1)
    try:
        tree = json.loads(out).get("blockdevices", [])
    except Exception:
        print("lsblk parse failed")
        sys.exit(1)

    # 收集上次 state 与旧用量(缓存)
    prev_state = {}
    old_usage = {}
    try:
        with open(STATE) as f:
            prev_state = json.load(f)
    except Exception:
        pass
    try:
        with open(OUT) as f:
            for x in json.load(f):
                old_usage[x.get("name")] = (x.get("used"), x.get("percent"))
    except Exception:
        pass

    cur_state = disk_sectors()

    def collect_mounts(children):
        vols = []
        for x in children:
            for m in (x.get("mountpoints") or []):
                if m and m not in SKIP:
                    vols.append("系统盘" if m == "/" else m)
            vols += collect_mounts(x.get("children") or [])
        return vols

    def walk(items):
        res = []
        for b in items:
            t = b.get("type", "")
            name = b.get("name", "")
            if t == "disk" and not name.startswith(("loop", "ram")):
                size = b.get("size", 0)
                rota = b.get("rota")
                ssd = (rota == 0)
                vols = collect_mounts([b]) or ["未挂载"]
                # 判断活跃: SSD 恒活跃; 机械盘看 diskstats 扇区是否变化(无旧记录则视为活跃以首跑取数)
                if ssd:
                    active = True
                else:
                    prev = prev_state.get(name)
                    cur = cur_state.get(name)
                    active = (prev is None) or (cur != prev)
                # 取用量: 活跃 -> df 真实值; 休眠 -> 沿用缓存
                if active:
                    used, percent = df_usage(vols)
                    if used is None and old_usage.get(name) is not None:
                        used, percent = old_usage[name]  # df 偶发失败回退缓存
                else:
                    used, percent = old_usage.get(name, (None, None))
                res.append({
                    "name": name,
                    "size": size,
                    "ssd": ssd,
                    "volumes": " + ".join(vols),
                    "power": "ssd" if ssd else "unknown",
                    "used": used,
                    "percent": percent,
                })
            else:
                res += walk(b.get("children") or [])
        return res

    result = walk(tree)

    # 写 state(供下次比较)
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(cur_state, f)
    except Exception:
        pass

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    refreshed = sum(1 for x in result if x.get("ssd") or (x.get("used") is not None and old_usage.get(x["name"]) != (x.get("used"), x.get("percent"))))
    print("host_mounts.json updated:", len(result), "disks; active->df:", refreshed)


if __name__ == "__main__":
    main()
