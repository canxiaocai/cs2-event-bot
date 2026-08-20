#!/bin/bash
# 登录后确保 NapCat 容器在跑(等 Docker Desktop 就绪再 compose up)。
# 由 LaunchAgent com.qqbot.napcat 调用。

set -uo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAPCAT_DIR="$ROOT/napcat"
LOG="$NAPCAT_DIR/launchd.log"
MAX_WAIT=120   # 最多等 10 分钟(120 * 5s)

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') [napcat] $*" >>"$LOG"
}

log "start-napcat.sh begin"

# Docker 未起则尝试打开 Docker Desktop
if ! docker info >/dev/null 2>&1; then
  log "Docker 未就绪,尝试 open -a Docker"
  open -a Docker >/dev/null 2>&1 || true
fi

ready=0
for i in $(seq 1 "$MAX_WAIT"); do
  if docker info >/dev/null 2>&1; then
    ready=1
    log "Docker 就绪 (第 ${i} 次探测)"
    break
  fi
  # 每 50 秒再试一次打开 Docker
  if (( i % 10 == 0 )); then
    open -a Docker >/dev/null 2>&1 || true
    log "仍在等待 Docker... (${i}/${MAX_WAIT})"
  fi
  sleep 5
done

if (( ready != 1 )); then
  log "ERROR: Docker 在 ${MAX_WAIT}*5s 内未就绪,放弃"
  exit 1
fi

cd "$NAPCAT_DIR" || exit 1
if docker compose up -d >>"$LOG" 2>&1; then
  log "docker compose up -d 成功"
  docker ps --filter name=napcat --format '{{.Names}} {{.Status}}' >>"$LOG" 2>&1 || true
  exit 0
else
  log "ERROR: docker compose up -d 失败"
  exit 1
fi
