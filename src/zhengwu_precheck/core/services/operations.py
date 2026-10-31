"""发布环境检查；只验证本地先决条件，不代表远端已部署。"""
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


def release_preflight(environment, *, repository):
    required = ('ZHENGWU_RUNTIME_DIR', 'ZHENGWU_PUBLIC_URL', 'ZHENGWU_STAFF_JWT_SECRET',
                'ZHENGWU_TLS_CERT', 'ZHENGWU_TLS_KEY', 'ZHENGWU_CONFIG_VERSION')
    if any(not environment.get(key) for key in required):
        raise ValueError('正式运行缺少外部配置；请依发布模板逐项设置')
    if environment.get('ZHENGWU_SECURE_COOKIES', '').lower() != 'true':
        raise ValueError('正式运行须启用安全Cookie')
    url = urlsplit(environment['ZHENGWU_PUBLIC_URL'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('正式服务须明确配置HTTPS地址')
    allowed={host.strip().strip('[]').lower() for host in environment.get('ZHENGWU_ALLOWED_HOSTS','').split(',')}
    if url.hostname.lower() not in allowed:
        raise ValueError('ZHENGWU_ALLOWED_HOSTS须明确包含正式HTTPS地址的域名')
    if len(environment['ZHENGWU_STAFF_JWT_SECRET']) < 32:
        raise ValueError('工作人员签名密钥至少32字符并须随机生成')
    runtime = Path(environment['ZHENGWU_RUNTIME_DIR'])
    if not runtime.is_absolute():
        raise ValueError('持久目录须为绝对路径')
    runtime = runtime.resolve()
    if runtime.is_relative_to(Path(repository).resolve()) or runtime.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError('正式持久目录须位于仓库和临时目录外')
    if not runtime.is_dir() or not os.access(runtime, os.W_OK):
        raise ValueError('须先建立可写私有持久目录并由管理员核对权限')
    if environment.get('ZHENGWU_PRIVATE_STORAGE_CONFIRMED') != 'true':
        raise ValueError('管理员须确认私有目录ACL/权限和磁盘持久性')
    for key in ('ZHENGWU_TLS_CERT','ZHENGWU_TLS_KEY'):
        path = Path(environment[key]).resolve()
        if not path.is_file() or path.is_relative_to(Path(repository).resolve()) or path.is_relative_to(runtime):
            raise ValueError('TLS证书和私钥须由管理员配置在仓库及业务运行目录外')
    return {'status':'ready_for_local_launch', 'config_version':environment['ZHENGWU_CONFIG_VERSION'],
            'hosting_verified':False, 'storage_permissions':'operator_attested'}
