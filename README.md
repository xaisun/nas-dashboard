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
├── app.py              # Flask 后端: 采集 /proc、smartctl、Docker API
├── index.html          # 前端单页(纯原生, 无外部依赖)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
