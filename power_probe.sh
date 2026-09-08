#!/bin/bash
# NAS 待机功耗采样: 凌晨无人用时测 CPU 包功耗(120s 均值) + 磁盘活动 + CPU 总占用
# 用法: 手动 bash power_probe.sh  或 crontab 每天凌晨 03:30 自动跑
OUT=/vol1/1000/docker/enterprise/power_probe.log
mkdir -p "$(dirname "$OUT")"
echo "=== $(date '+%F %T') probe start ===" >> "$OUT"
# CPU 包功耗 + sdb(1T 盘) 120 秒活动对比; RAPL 需容器内 root
docker exec nas-dashboard sh -c 's0=$(cat /sys/class/powercap/intel-rapl:0/energy_uj); r0=$(awk "\$3==\"sdb\"{print \$6+\$10}" /proc/diskstats); sleep 120; s1=$(cat /sys/class/powercap/intel-rapl:0/energy_uj); r1=$(awk "\$3==\"sdb\"{print \$6+\$10}" /proc/diskstats); awk "BEGIN{printf \"CPU包功耗 %.2f W, sdb 120s 活动 %+.0f KB\n\", ($s1-$s0)/120e6, ($r1-$r0)/2048}"' >> "$OUT"
# SMART attrlog 最新更新时间(盘休眠时 smartd -n standby 跳过不更新 -> 停更=盘在睡)
docker exec nas-dashboard sh -c 'echo "SMART attrlog 最新: $(ls -t /var/lib/smartmontools/attrlog.*.ata.csv 2>/dev/null | head -1 | xargs stat -c "%y %n" 2>/dev/null)"' >> "$OUT"
# 整机 CPU 占用(/proc/stat 两次采样精确算法, 4 核系统)
c1=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9+$10+$11+$12+$13+$14+$15}' /proc/stat)
sleep 5
c2=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9+$10+$11+$12+$13+$14+$15}' /proc/stat)
i1=$(awk '/^cpu /{print $5}' /proc/stat)
sleep 5
i2=$(awk '/^cpu /{print $5}' /proc/stat)
echo "整机 CPU 占用: $(( (c2-c1-i2+i1)*100/(c2-c1) ))%" >> "$OUT"
# CPU 占用 TOP5(跳过表头)
ps aux --sort=-%cpu | awk 'NR>1 && NR<=7{printf "TOP: %s %s%%\n", $11, $3}' >> "$OUT"
echo "=== $(date '+%F %T') done ===" >> "$OUT"
