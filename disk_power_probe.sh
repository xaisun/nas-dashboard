#!/bin/sh
# disk_power_probe.sh —— 以 root 在宿主运行, 写真实盘电源/用量到 disk_power.json
# 看板(app.py)读取该文件, 显示每块盘真实休眠/活跃状态。
#
# 非唤醒采集:
#   - smartctl -n standby -i /dev/sdX : 读取电源模式, 对休眠盘直接跳过、绝不旋转唤醒
#   - 仅当盘为 active 时才 df 取用量(休眠盘不碰, 不唤醒)
#
# 用法(需 root):
#   chmod +x disk_power_probe.sh
#   ./disk_power_probe.sh                      # 立即跑一次
#   echo '*/5 * * * * /vol1/@apphome/trim.openclaw/data/workspace/nas-dashboard/disk_power_probe.sh' > /etc/cron.d/disk_power  # 每5分钟
#
OUT=/vol1/@apphome/trim.openclaw/data/workspace/nas-dashboard/disk_power.json
TMP="$OUT.tmp"
SMART=/usr/sbin/smartctl

disk_mount() {
  # 取盘的第一个非空挂载点(只读 /sys, 不碰盘)
  lsblk -n -o MOUNTPOINT "/dev/$1" 2>/dev/null | grep -v '^$' | head -1
}

printf '[' > "$TMP"
first=1
for d in sda sdb sdc sdd sde sdf; do
  [ -b "/dev/$d" ] || continue
  st=$("$SMART" -n standby -i "/dev/$d" 2>/dev/null | grep -iE 'STANDBY|ACTIVE|IDLE' | head -1)
  if echo "$st" | grep -qi 'standby'; then
    power=standby
  elif echo "$st" | grep -qiE 'active|idle'; then
    power=active
  else
    power=unknown
  fi
  used=null
  percent=null
  if [ "$power" = "active" ]; then
    mp=$(disk_mount "$d")
    if [ -n "$mp" ]; then
      line=$(df -B1 -T "$mp" 2>/dev/null | tail -1)
      set -- $line
      # df 字段: 1=文件系统 2=类型 3=总块 4=已用 5=可用 6=容量% 7=挂载点
      if [ -n "$4" ] && [ -n "$3" ] && [ "$3" -gt 0 ] 2>/dev/null; then
        used=$4
        percent=$(awk "BEGIN{printf \"%.1f\", $4/$3*100}")
      fi
    fi
  fi
  [ "$first" -eq 0 ] && printf ',' >> "$TMP"
  first=0
  printf '{"name":"%s","power":"%s","used":%s,"percent":%s}' "$d" "$power" "$used" "$percent" >> "$TMP"
done
printf ']' >> "$TMP"
mv -f "$TMP" "$OUT"
echo "disk_power.json updated: $(cat "$OUT")"
