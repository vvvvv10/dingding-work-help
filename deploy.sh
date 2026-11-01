#!/usr/bin/env bash
# 一键部署：把当前目录发布到服务器并重启服务。
#
#   ./deploy.sh            正常发布（会先备份，健康检查失败自动回滚）
#   ./deploy.sh --dry-run  只显示会做什么，不动服务器
#
# 只同步代码与 systemd/nginx 配置；服务器上的 .env 和 MySQL 数据不会被覆盖。
set -euo pipefail

HOST="${KB_HOST:-root@47.99.54.0}"
REMOTE_DIR="${KB_REMOTE_DIR:-/opt/dingtalk-kb-bot}"
APP_PORT="${KB_APP_PORT:-8017}"
SITE="${KB_SITE:-kb.smartwork-inc.me}"
PY="${KB_PY:-/usr/bin/python3.9}"
SSH_OPTS=(-o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)

DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

cd "$(dirname "$0")"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
run_remote() { ssh "${SSH_OPTS[@]}" "$HOST" "export HOME=/root; $1"; }

if (( DRY )); then
  say "预演：目标 $HOST:$REMOTE_DIR"
  rsync -azn --delete --exclude '.env' --exclude 'data/' --exclude '.venv/' \
    --exclude '__pycache__/' --exclude '*.pyc' --exclude '.git/' --exclude '*.sqlite3' \
    ./ "$HOST:$REMOTE_DIR/"
  exit 0
fi

say "1/6 备份当前版本"
run_remote "cd $REMOTE_DIR && rm -rf .prev && cp -a app .prev"

say "2/6 同步代码"
rsync -az --delete \
  --exclude '.env' --exclude 'data/' --exclude '.venv/' --exclude '.prev/' \
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.git/' --exclude '*.sqlite3' \
  ./ "$HOST:$REMOTE_DIR/"

say "3/6 安装依赖（缺什么补什么）"
run_remote "[ -x $REMOTE_DIR/.venv/bin/python ] || $PY -m venv $REMOTE_DIR/.venv
  $REMOTE_DIR/.venv/bin/pip install -q pymysql -i https://mirrors.cloud.tencent.com/pypi/simple"

say "4/6 安装 systemd 单元与 nginx 配置"
rsync -az deploy/kb-admin.service deploy/kb-listen.service deploy/kb-sync.service deploy/kb-sync.timer \
  "$HOST:/etc/systemd/system/"
NGINX_CHANGED=0
if ! ssh "${SSH_OPTS[@]}" "$HOST" "cmp -s /etc/nginx/conf.d/$SITE.conf -" < deploy/nginx-kb.conf; then
  rsync -az deploy/nginx-kb.conf "$HOST:/etc/nginx/conf.d/$SITE.conf"
  NGINX_CHANGED=1
fi
run_remote "systemctl daemon-reload"
if (( NGINX_CHANGED )); then
  say "     nginx 配置有变化，校验后 reload"
  run_remote "nginx -t && nginx -s reload"
fi

say "5/6 重启服务"
run_remote "systemctl enable --now kb-sync.timer >/dev/null 2>&1 || true
  systemctl restart kb-admin kb-listen
  systemctl start kb-sync.service >/dev/null 2>&1 || true"

say "6/6 健康检查"
sleep 4
CODE=$(run_remote "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$APP_PORT/m || true")
ACTIVE=$(run_remote "systemctl is-active kb-admin kb-listen | tr '\n' ' '")

if [[ "$CODE" == "200" && "$ACTIVE" == "active active " ]]; then
  say "部署成功：HTTP ${CODE}，服务 ${ACTIVE}"
  exit 0
fi

say "健康检查失败（HTTP=${CODE}，服务=${ACTIVE}），回滚上一版"
run_remote "cd $REMOTE_DIR && rm -rf app && mv .prev app && systemctl restart kb-admin kb-listen" || true
say "已回滚，服务状态：$(run_remote "systemctl is-active kb-admin kb-listen | tr '\n' ' '")"
exit 1
