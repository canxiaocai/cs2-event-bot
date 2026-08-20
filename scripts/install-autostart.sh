#!/bin/bash
# 安装/更新 QQBot 开机(登录)自启 LaunchAgents
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/deploy/launchd"
DEST="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"

mkdir -p "$DEST"
chmod +x "$ROOT/scripts/start-napcat.sh" "$ROOT/scripts/start-nonebot.sh" "$ROOT/scripts/install-autostart.sh"

for label in com.qqbot.napcat com.qqbot.nonebot; do
  plist="$DEST/${label}.plist"
  # 已加载则先卸下
  if launchctl print "${DOMAIN}/${label}" >/dev/null 2>&1; then
    launchctl bootout "${DOMAIN}/${label}" 2>/dev/null || true
  fi
  # 模板里的占位符替换成本机真实路径(launchd 运行时要求绝对路径)
  sed -e "s|__QQBOT_ROOT__|$ROOT|g" -e "s|__HOME__|$HOME|g" "$SRC/${label}.plist" >"$plist"
  launchctl bootstrap "$DOMAIN" "$plist"
  launchctl enable "${DOMAIN}/${label}" 2>/dev/null || true
  echo "installed & loaded: $label"
done

echo "done. 查看状态:"
echo "  launchctl print gui/\$(id -u)/com.qqbot.nonebot | head -40"
echo "  launchctl print gui/\$(id -u)/com.qqbot.napcat | head -40"
echo "  tail -f $ROOT/bot/nonebot.log"
