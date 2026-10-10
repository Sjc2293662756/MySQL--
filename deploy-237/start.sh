#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
IMAGE='mysql-traffic-generator:1.0.4'
CONTAINER='mysql-traffic-generator'
MODE="${1:-start}"

if [[ $# -gt 1 || "$MODE" != 'start' && "$MODE" != 'check-config' ]]; then
  printf '%s\n' '用法：bash start.sh [start|check-config]' >&2
  exit 2
fi

if [[ ! -f "$APP_DIR/config.yaml" || ! -d "$APP_DIR/logs" || ! -f "$APP_DIR/compose.yaml" ]]; then
  printf '%s\n' '启动目录中需要 config.yaml、logs/ 和 compose.yaml，请先准备部署文件。' >&2
  exit 2
fi

chmod 640 "$APP_DIR/config.yaml"
docker image inspect "$IMAGE" >/dev/null
docker compose version >/dev/null
COMPOSE=(docker compose --project-directory "$APP_DIR" --file "$APP_DIR/compose.yaml")
"${COMPOSE[@]}" config --quiet

# Check the host file through a fresh mount so atomic editor saves are included.
"${COMPOSE[@]}" run --rm --no-deps --entrypoint python traffic \
  -c "from src.config import load_config; load_config('/app/config.yaml'); print('配置校验通过')"

if [[ "$MODE" == 'check-config' ]]; then
  exit 0
fi

if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  managed="$(docker inspect --format '{{ index .Config.Labels "com.netinside.mysql-traffic-generator.managed" }}' "$CONTAINER")"
  service_dir="$(docker inspect --format '{{ index .Config.Labels "com.netinside.mysql-traffic-generator.service-dir" }}' "$CONTAINER")"
  project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$CONTAINER")"
  if [[ "$managed" != 'true' || "$service_dir" != "$APP_DIR" || "$project" != 'mysql-traffic-generator' ]]; then
    printf '%s\n' '同名容器与当前部署配置不匹配，请先检查现有容器。' >&2
    exit 2
  fi
fi

"${COMPOSE[@]}" up -d --no-build
"${COMPOSE[@]}" ps -a
