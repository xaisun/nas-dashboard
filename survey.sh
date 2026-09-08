#!/bin/bash
# NAS 全面摸底: 容器/CPU/盘休眠/功耗 (清理后状态)
L=/vol1/1000/docker/enterprise/survey.log
echo "=== $(date '+%F %T') 摸底开始 ===" > "$L"
echo '--- 1. 所有容器 ---' >> "$L"
docker ps -a --format '{{.Names}}|{{.Status}}|{{.Ports}}' 2>/dev/null >> "$L"
echo '--- 2. 运行容器 CPU TOP10 ---' >> "$L"
docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}' 2>/dev/null | sort -t'|' -k2 -rn | head -10 >> "$L"
echo '--- 3. 宿主进程 CPU TOP8 ---' >> "$L"
ps aux --sort=-%cpu 2>/dev/null | head -8 >> "$L"
echo '--- 4. 盘休眠(attrlog 停更=睡) ---' >> "$L"
ls -la --time-style='+%m-%d %H:%M' /var/lib/smartmontools/attrlog.*.ata.csv 2>/dev/null | awk '{print $6, $7}' >> "$L"
echo "now $(date '+%m-%d %H:%M:%S')" >> "$L"
echo '--- 5. 整机 CPU 占用(/proc/stat) ---' >> "$L"
c1=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9+$10+$11+$12+$13+$14+$15}' /proc/stat)
sleep 5
c2=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9+$10+$11+$12+$13+$14+$15}' /proc/stat)
i1=$(awk '/^cpu /{print $5}' /proc/stat)
sleep 5
i2=$(awk '/^cpu /{print $5}' /proc/stat)
echo "整机 CPU 占用: $(( (c2-c1-i2+i1)*100/(c2-c1) ))%" >> "$L"
echo '--- 6. CPU 包功耗(60s 均值) ---' >> "$L"
docker exec nas-dashboard sh -c 's0=$(cat /sys/class/powercap/intel-rapl:0/energy_uj); sleep 60; s1=$(cat /sys/class/powercap/intel-rapl:0/energy_uj); awk "BEGIN{printf \"%.2f W\n\", ($s1-$s0)/60e6}"' >> "$L" 2>&1
echo '--- 7. 机械盘活动(各20s) ---' >> "$L"
for d in sdb sdc sdd sde sdf; do
  a1=$(awk -v d="$d" '$3==d{print $6+$10}' /proc/diskstats)
  sleep 20
  a2=$(awk -v d="$d" '$3==d{print $6+$10}' /proc/diskstats)
  echo "$d: $(( (a2-a1)/2048 ))KB/20s" >> "$L"
done
echo '--- 8. 内存 ---' >> "$L"
free -m 2>/dev/null | head -2 >> "$L"
echo "=== $(date '+%F %T') 结束 ===" >> "$L"
