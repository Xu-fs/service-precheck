"""生成只替换三个演示文件的Ubuntu更新脚本，不包含任何凭证。"""
import base64
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    'src/zhengwu_precheck/api/routes/workflow_demo.py',
    'src/zhengwu_precheck/templates/workflow_demo.html',
    'src/zhengwu_precheck/static/js/workflow_demo.js',
)


def main():
    payload = {name: base64.b64encode((ROOT / name).read_bytes()).decode('ascii') for name in FILES}
    script = '''#!/usr/bin/env bash
# 仅更新教学流程演示文件，保留账号、配置和运行数据。
set -euo pipefail
umask 077
[[ "$(id -u)" == 0 ]] || { echo '请使用root运行'; exit 1; }
[[ -f /opt/zhengwu-mvp/.managed-teaching-deploy ]] || { echo '未识别到原教学部署，未执行更新'; exit 1; }
BACKUP=$(mktemp -d /opt/zhengwu-mvp/guest-backup-XXXXXXXX)
export BACKUP
python3 - <<'PYUPDATE'
import base64, json, os
from pathlib import Path
app = Path('/opt/zhengwu-mvp/app')
backup = Path(os.environ['BACKUP'])
payload = json.loads(PAYLOAD_JSON)
assert app.resolve() == app and app.is_dir(), '应用目录不能是符号链接'
for name, encoded in payload.items():
    target = app / name
    assert target.resolve() == target and target.is_file(), '更新目标不符: ' + name
    data = base64.b64decode(encoded, validate=True)
    if name.endswith('.py'):
        compile(data, name, 'exec')
    saved = backup / name
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_bytes(target.read_bytes())
# 全部校验和备份完成后再替换；写入失败也恢复原文件。
try:
    for name, encoded in payload.items():
        target = app / name
        target.write_bytes(base64.b64decode(encoded))
        target.chmod(0o644)
except Exception:
    for name in payload:
        (app / name).write_bytes((backup / name).read_bytes())
        (app / name).chmod(0o644)
    raise
PYUPDATE
rollback() {
    echo '更新检查失败，正在恢复原演示文件。'
    cp "$BACKUP/src/zhengwu_precheck/api/routes/workflow_demo.py" /opt/zhengwu-mvp/app/src/zhengwu_precheck/api/routes/workflow_demo.py
    cp "$BACKUP/src/zhengwu_precheck/templates/workflow_demo.html" /opt/zhengwu-mvp/app/src/zhengwu_precheck/templates/workflow_demo.html
    cp "$BACKUP/src/zhengwu_precheck/static/js/workflow_demo.js" /opt/zhengwu-mvp/app/src/zhengwu_precheck/static/js/workflow_demo.js
    systemctl restart zhengwu-mvp || true
}
trap rollback ERR
systemctl restart zhengwu-mvp
python3 - <<'PYVERIFY'
from urllib.request import urlopen, Request
import time
base = 'http://127.0.0.1:18080'
for attempt in range(40):
    try:
        with urlopen(base + '/health', timeout=2) as response:
            assert response.status == 200
        break
    except Exception:
        if attempt == 39:
            raise
        time.sleep(1)
with urlopen(base + '/examples/workflow', timeout=5) as response:
    html = response.read().decode('utf-8')
assert 'id="demo-play"' in html and '请先登录' not in html, '免登录入口检查失败'
with urlopen(base + '/static/js/workflow_demo.js?v=guest-20261108', timeout=5) as response:
    assert b'crypto.getRandomValues' in response.read(), 'HTTP脚本更新未生效'
PYVERIFY
trap - ERR
echo '免登录更新成功。打开网页后按Ctrl+F5刷新，再选案例并开始自动演示。'
echo 'http://47.117.178.247/examples/workflow'
echo "原文件备份: $BACKUP"
'''.replace('PAYLOAD_JSON', repr(json.dumps(payload)))
    output = ROOT / '作业提交/src/Ubuntu部署/免登录更新.sh'
    output.write_text(script, encoding='utf-8', newline='\n')
    print(output)
    print('SHA256', hashlib.sha256(output.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
