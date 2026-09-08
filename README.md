# NAS 监控看板 🖥️

中文界面的 NAS 实时监控看板，Docker 一键部署。仿经典 NAS 监控面板布局：

- 📅 **日期/农历/干支**（如：丙午年六月廿八）
- 🌤 **天气**（当前 + 今明后三天预报，按公网 IP 自动识别城市，也可手动指定）
- 🖥 **设备信息**（主机名、在线状态、节点 ID、公网/内网 IP、运行时间）
- ⚙️ **CPU**（使用率环形图、型号、负载均值、温度）
- 💾 **内存**（已用/总量、百分比、可用）
- 🌐 **网络**（网卡、接口速率、实时上下行、磁盘读写）
- 🐳 **容器**（Docker 运行中/总数、容器状态列表）
- 📁 **存储空间**（各分区/卷：文件系统、RAID、已用/总量）
- 💽 **硬盘**（型号、容量、SSD/机械盘类型、SMART 温度、健康状态）

数据每 5 秒自动刷新。

---

## 快速部署

```bash
cd nas-dashboard
sudo docker compose up -d --build
```

启动后访问：**http://<NAS的IP>:8904**

> 若当前用户已在 docker 组，可去掉 `sudo`。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8904` | 监听端口 |
| `NODE_ID` | `NODE-001` | 看板上显示的节点 ID |
| `WEATHER_CITY` | 空(自动) | 天气：留空按公网 IP 自动识别；填 `lat,lon`(如 `22.27,113.57`)强制坐标；填城市名仅覆盖显示名。数据源 Open-Meteo（免费/无需 key） |

修改后 `sudo docker compose up -d` 重建即可。

## 权限说明

- `privileged: true`：用于读取硬盘 SMART 温度/健康（smartctl 需要访问 `/dev`）。
  如果不希望用特权模式，去掉该行后其余功能正常，仅硬盘温度显示 "—"。
- 挂载 `/var/run/docker.sock`：用于显示容器状态。
- `network_mode: host`：直接监控宿主机真实网卡与端口。

## 存储空间容量采集（`host_mounts_gen.py`）

看板的**「存储空间 / 硬盘」容量**（已用 / 总量 / 百分比）不是容器里算的，而是由宿主侧脚本 `host_mounts_gen.py` 采集后写入 `host_mounts.json`，再通过整目录 bind 挂载暴露给容器（容器读取 `/app/host_mounts.json`）。

**为什么不在容器里直接 `df`？** 两个原因：

1. 容器看不到宿主的真实挂载点，`df` 结果不准；
2. **会唤醒休眠的机械盘** —— 一次容量采集把正在休眠的盘叫醒，省电和静音就都白费了。

这个脚本的采集策略是「休眠安全」的：

| 步骤 | 做法 | 是否会唤醒硬盘 |
|------|------|----------------|
| 拿盘树 / 总容量 / 是否 SSD | `lsblk -b -J`（只读 `/sys`） | ❌ 不会 |
| 判断机械盘是否醒着 | 读 `/proc/diskstats`（内核纯内存统计）比较累计扇区数是否变化 | ❌ 不会 |
| 取真实用量 | **只对活跃盘** `df`；SSD 恒视为活跃 | ✅ 仅对已醒的盘 |
| 休眠盘 | 沿用 `host_mounts.json` 里上次的缓存用量 | ❌ 不会 |

### 用法

在**宿主（NAS）上**执行，普通用户即可（不需要 root）：

```bash
cd <你的部署目录>/nas-dashboard
python3 host_mounts_gen.py
```

输出示例：

```
host_mounts.json updated: 6 disks; active->df: 2
```

会生成两个文件：

- `host_mounts.json` —— 给看板读的数据（原子写入：先写 `.tmp` 再 `os.replace`，避免读到半截文件）
- `host_mounts.state` —— 上次 `/proc/diskstats` 的扇区快照，供下次比较判断活跃

只需 Python 3 标准库，**无第三方依赖**。

### 定时采集（推荐）

容量变化很慢，每 10 分钟跑一次就够。加一条 crontab：

```bash
*/10 * * * * cd <你的部署目录>/nas-dashboard && /usr/bin/python3 host_mounts_gen.py >> host_mounts.log 2>&1
```

> 首次运行建议手动先跑一次，确认 `host_mounts.json` 生成正常再加定时任务。

### 数据字段

`host_mounts.json` 是一个数组，每项：

```json
{
  "name": "sdb",
  "size": 4000787030016,
  "ssd": false,
  "volumes": "/vol2 + /vol3",
  "power": "unknown",
  "used": 1200137064448,
  "percent": 30.0
}
```

| 字段 | 说明 |
|------|------|
| `name` | 整盘设备名（`loop` / `ram` 开头的虚拟盘已跳过） |
| `size` | 总容量（字节） |
| `ssd` | 是否固态（`lsblk` 的 `ROTA=0` 判定） |
| `volumes` | 该盘上的挂载点，`/` 显示为「系统盘」，无挂载显示「未挂载」 |
| `power` | 电源状态：`ssd` 或 `unknown`（真实 active/standby 由看板「硬盘状态」卡片推断） |
| `used` / `percent` | 已用字节 / 百分比；**休眠盘为上次缓存值**，首次采集前可能为 `null` |

### 没有这个文件会怎样

看板不会报错，`app.py` 会自动回退到「容器内只读 `lsblk`」模式：盘的型号、容量、类型都正常显示，但**已用 / 百分比显示 "—"**。想要容量数字，就把这个脚本跑起来。

## 不带 Docker 直接运行（调试用）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PORT=8904 .venv/bin/python app.py
```

## 常见问题

- **硬盘温度显示 "—" / 健康显示 "不支持"**：磁盘不支持 SMART，或容器未以特权模式运行。
- **天气获取失败**：容器需要能访问外网（api.open-meteo.com 取气温、ip-api.com 取定位）。
- **端口被占用**：修改 `docker-compose.yml` 中 `PORT` 环境变量。
- **CPU 温度显示 "—"**：部分设备无 ACPI/coretemp 传感器。

## 项目结构

```
nas-dashboard/
├── app.py                # Flask 后端: 采集 /proc、smartctl、Docker API
├── index.html            # 前端单页(纯原生, 无外部依赖)
├── host_mounts_gen.py    # 宿主侧容量采集(休眠安全), 生成 host_mounts.json
├── deploy.sh             # 一键部署脚本(检查 Docker + compose up)
├── power_probe.sh        # 硬盘电源状态探测(调试用)
├── disk_power_probe.sh   # 整盘电源状态批量探测(调试用)
├── survey.sh             # 宿主环境勘察(调试用)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

运行时生成（已在 `.gitignore` 中忽略或在部署目录产生，不入仓库）：

- `host_mounts.json` —— 容量数据，由 `host_mounts_gen.py` 生成，经目录挂载供容器读取
- `host_mounts.state` —— 上次 diskstats 扇区快照
- `__pycache__/` —— Python 缓存
