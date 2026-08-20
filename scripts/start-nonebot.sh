#!/bin/bash
# NoneBot2 前台运行入口(给 launchd KeepAlive 用)。
# 不要在这里 nohup/后台: launchd 需要盯住主进程。

set -uo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="${HOME:-$(eval echo ~"$(id -un)")}"
# Playwright Chromium 默认就在 ~/Library/Caches/ms-playwright

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOT_DIR="$ROOT/bot"
PY="$BOT_DIR/.venv/bin/python"
LOG="$BOT_DIR/nonebot.log"

cd "$BOT_DIR" || exit 1

# 避免残留僵尸占用 8080(例如手工 nohup 与 launchd 双开)
if command -v lsof >/dev/null 2>&1; then
  pids=$(lsof -tiTCP:8080 -sTCP:LISTEN 2>/dev/null || true)
  if [[ -n "${pids:-}" ]]; then
    # 若是自己(launchd 重启间隙)则跳过;否则清掉占端口的旧 bot
    for p in $pids; do
      if [[ "$p" != "$$" ]] && ps -p "$p" -o args= 2>/dev/null | grep -q "bot.py"; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [nonebot] 清理占用 8080 的旧 bot.py pid=$p" >>"$LOG"
        kill "$p" 2>/dev/null || true
        sleep 1
        kill -9 "$p" 2>/dev/null || true
      fi
    done
    sleep 1
  fi
fi

# 统一写 nonebot.log(与手工启动一致);launchd.out 仅作兜底
echo "$(date '+%Y-%m-%d %H:%M:%S') [nonebot] launchd 启动 bot.py" >>"$LOG"
exec >>"$LOG" 2>&1
exec "$PY" bot.py
