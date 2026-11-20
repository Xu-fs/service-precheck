#!/usr/bin/env bash
# Ubuntu 24.04 教学演示部署，不连接模型、不导入本地运行数据库。
set -euo pipefail
umask 027
trap 'printf "\n部署未完成，停止在第%s行；原有应用及数据不会被清理。\n" "$LINENO" >&2' ERR
PUBLIC_HOST="${1:-47.117.178.247}"
[[ "$PUBLIC_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || { echo '主机名只能包含字母数字点和横线'; exit 1; }
[[ "$(id -u)" == 0 ]] || { echo '请使用root或sudo执行'; exit 1; }
[[ -f /etc/os-release ]] || { echo '没有识别到Linux发行版'; exit 1; }
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || { echo '本部署包仅针对Ubuntu 24.04验证设计'; exit 1; }
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE=/opt/zhengwu-mvp
APP="$BASE/app"
RUNTIME=/var/lib/zhengwu-mvp
SETTINGS=/etc/zhengwu-mvp
SERVICE=zhengwu-mvp
[[ ! -L "$BASE" && ! -L "$RUNTIME" && ! -L "$SETTINGS" ]] || { echo '部署目标不能是符号链接'; exit 1; }
if [[ -e "$BASE" && ! -f "$BASE/.managed-teaching-deploy" ]]; then
    echo "$BASE 已存在且不属于本脚本，停止以保护既有文件"; exit 1
fi
if [[ -d "$SETTINGS" && ! -f "$BASE/.managed-teaching-deploy" ]]; then
    echo "$SETTINGS 已存在但未确认归属，停止以保护既有配置"; exit 1
fi
if [[ -d "$RUNTIME" && -n "$(find "$RUNTIME" -mindepth 1 -maxdepth 1 -print -quit)" && ! -f "$RUNTIME/cloud-demo-state.json" ]]; then
    echo "$RUNTIME 已有其他资料，停止以保护既有数据"; exit 1
fi
if ss -H -lnt '( sport = :18080 )' | grep -q . && ! systemctl is-active --quiet "$SERVICE"; then
    echo '18080端口已有其他服务，未执行替换'; exit 1
fi
NGINX_SITE=/etc/nginx/conf.d/zhengwu-mvp.conf
if [[ -f "$NGINX_SITE" ]] && ! grep -q '^# managed-zhengwu-teaching-deploy$' "$NGINX_SITE"; then
    echo '同名Nginx配置已有其他内容，未覆盖'; exit 1
fi
echo '1/6 安装Ubuntu运行工具'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv python3-pip nginx curl ca-certificates libgomp1
if ! id "$SERVICE" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$RUNTIME" --shell /usr/sbin/nologin "$SERVICE"
fi
install -d -m 0755 "$BASE" "$APP"
touch "$BASE/.managed-teaching-deploy"
install -d -m 0700 -o "$SERVICE" -g "$SERVICE" "$RUNTIME"
install -d -m 0700 "$SETTINGS"
echo '2/6 安装独立源码和依赖'
python3 - "$SOURCE" "$APP" <<'PY'
from pathlib import Path
import shutil,sys
source,target=map(Path,sys.argv[1:])
for name in ('src','data','config','scripts','docs','tests','examples','.github'):
    if (source/name).is_dir():
        shutil.copytree(source/name,target/name,dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('.venv','__pycache__','.git','*.pyc','*.key','*.sqlite3','.env'))
for name in ('requirements.txt','README.md','LICENSE','AGENTS.md','.gitignore','CHANGELOG.md','CONTRIBUTING.md','启动网页.bat'):
    if (source/name).is_file(): shutil.copy2(source/name,target/name)
PY
if [[ ! -x "$BASE/venv/bin/python" ]]; then python3 -m venv "$BASE/venv"; fi
"$BASE/venv/bin/python" -m pip install --disable-pip-version-check -r "$APP/requirements.txt"
"$BASE/venv/bin/python" -m pip check
# 这里只包含公开源码和依赖，服务账号需要只读遍历权限；不作用于密钥或运行资料。
chmod -R a+rX "$BASE/venv" "$APP"
echo '3/6 创建独立虚构教学运行库'
ENV_FILE="$SETTINGS/mvp.env"
if [[ ! -f "$ENV_FILE" ]]; then
    python3 - "$ENV_FILE" "$PUBLIC_HOST" <<'PY'
import os,secrets,sys
path,host=sys.argv[1:]
values={'ZHENGWU_RUNTIME_DIR':'/var/lib/zhengwu-mvp',
        'ZHENGWU_STAFF_JWT_SECRET':secrets.token_urlsafe(48),
        'ZHENGWU_ALLOWED_HOSTS':'127.0.0.1,localhost,'+host,
        'ZHENGWU_SECURE_COOKIES':'false','ZHENGWU_TRUSTED_TEACHING_MATERIALS':'false',
        'PYTHONPATH':'/opt/zhengwu-mvp/app/src','PYTHONUTF8':'1',
        'LLM_API_KEY':'','LLM_MODEL':'','LLM_BASE_URL':'',
        'EMBEDDING_API_KEY':'','EMBEDDING_MODEL':'','EMBEDDING_BASE_URL':''}
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:
    for key,value in values.items(): f.write(key+'='+value+'\n')
PY
fi
set -a
. "$ENV_FILE"
set +a
case ",$ZHENGWU_ALLOWED_HOSTS," in
    *",$PUBLIC_HOST,"*) ;;
    *) echo '现有允许主机配置与本次地址不一致，请核对配置，未自动扩大访问'; exit 1 ;;
esac
runuser --preserve-environment -u "$SERVICE" -- "$BASE/venv/bin/python" "$APP/scripts/bootstrap_cloud_demo.py"
echo '4/6 配置开机启动与异常重启'
UNIT=/etc/systemd/system/zhengwu-mvp.service
if [[ -f "$UNIT" ]] && ! grep -q '^# managed-zhengwu-teaching-deploy$' "$UNIT"; then
    echo '同名systemd服务不属于本部署，未覆盖'; exit 1
fi
cat > "$UNIT" <<'UNIT'
# managed-zhengwu-teaching-deploy
[Unit]
Description=Zhengwu teaching MVP workflow
After=network.target
[Service]
User=zhengwu-mvp
Group=zhengwu-mvp
WorkingDirectory=/opt/zhengwu-mvp/app
EnvironmentFile=/etc/zhengwu-mvp/mvp.env
ExecStart=/opt/zhengwu-mvp/venv/bin/python -m uvicorn zhengwu_precheck.main:create_app --factory --host 127.0.0.1 --port 18080 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/zhengwu-mvp
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
for attempt in $(seq 1 30); do
    if curl --noproxy '*' -fsS http://127.0.0.1:18080/health > "$BASE/health.json"; then break; fi
    sleep 1
done
python3 - "$BASE/health.json" <<'PY'
import json,sys
data=json.load(open(sys.argv[1]))
assert data.get('status')=='ok' and data.get('teaching_only') is True
PY
echo '5/6 配置Nginx网站入口'
if [[ ! -f "$NGINX_SITE" ]]; then touch "$NGINX_SITE"; fi
cp -p "$NGINX_SITE" "$SETTINGS/nginx-before-last.conf"
cat > "$NGINX_SITE" <<NGINX
# managed-zhengwu-teaching-deploy
server {
    listen 80;
    server_name $PUBLIC_HOST;
    client_max_body_size 12m;
    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_http_version 1.1;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 150s;
    }
}
NGINX
if ! nginx -t; then
    cp -p "$SETTINGS/nginx-before-last.conf" "$NGINX_SITE"
    echo 'Nginx配置未通过，已恢复本网站此前配置，未重载Nginx'; exit 1
fi
systemctl enable --now nginx
systemctl reload nginx
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
    ufw allow 80/tcp
fi
echo '6/6 检查本机网站与登录入口'
curl --noproxy '*' -fsS -H "Host: $PUBLIC_HOST" http://127.0.0.1/health
curl --noproxy '*' -fsS -o /dev/null -H "Host: $PUBLIC_HOST" http://127.0.0.1/examples/workflow
curl --noproxy '*' -fsS -o /dev/null -H "Host: $PUBLIC_HOST" http://127.0.0.1/staff/login
printf '\n服务器本机检查通过。公网连通性仍须放行ECS安全组TCP 80并从外部验证。\n'
printf '演示地址：http://%s/examples/workflow\n' "$PUBLIC_HOST"
printf '登录资料查看：sudo cat /var/lib/zhengwu-mvp/demo-access.txt\n'
printf '运行状态：systemctl status zhengwu-mvp --no-pager\n'
printf '本入口为HTTP虚构教学演示；如需处理真实资料，须先配置域名HTTPS并另行验收。\n'
