"""只为新建、归属明确的云端教学运行库准备账号和工单。"""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from zhengwu_precheck.settings import Settings
from zhengwu_precheck.utils.security import hash_password
from create_demo_accounts import DEMO_ROLES
from create_demo_cases import provision_cases


def private_write(path, content):
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
        stream.write(content)


async def bootstrap(settings):
    runtime=Path(settings.runtime_dir)
    marker=runtime/'cloud-demo-state.json'
    if runtime.is_symlink() or marker.is_symlink():
        raise ValueError('运行目录和归属标记不能是符号链接')
    if runtime.exists() and any(runtime.iterdir()) and not marker.is_file():
        raise ValueError('运行目录已有其他资料，已停止且不会覆盖')
    settings.validate()
    if marker.exists():
        state=json.loads(marker.read_text(encoding='utf-8'))
        if state.get('schema')!=1 or state.get('teaching_only') is not True:
            raise ValueError('运行目录不属于本教学部署')
        if state.get('result'):
            if not (runtime/'demo-password.txt').is_file():
                raise ValueError('已初始化运行库缺少原登录资料，请人工检查；不会重设密码')
            return state['result']
    else:
        private_write(marker,json.dumps({'schema':1,'teaching_only':True}))
    password_file=runtime/'demo-password.txt'
    if not password_file.exists():
        private_write(password_file,secrets.token_urlsafe(24)+'\n')
    if password_file.is_symlink():
        raise ValueError('登录资料不能是符号链接')
    password=password_file.read_text(encoding='utf-8').strip()
    if len(password)<20:
        raise ValueError('教学登录口令长度不足，已停止')
    cases=await provision_cases(settings)
    owned=json.loads((runtime/'demo-accounts.json').read_text(encoding='utf-8'))['accounts']
    with closing(sqlite3.connect(runtime/'chat.sqlite3')) as conn, conn:
        for role in DEMO_ROLES:
            username='demo_'+role
            identifier=owned.get(username)
            if not identifier:
                raise ValueError('示例账号归属不完整')
            changed=conn.execute('UPDATE users SET password_hash=? WHERE id=? AND username=?',
                                 (hash_password(password),identifier,username)).rowcount
            if changed!=1:
                raise ValueError('示例账号归属校验失败')
    access=runtime/'demo-access.txt'
    if not access.exists():
        private_write(access,'教学演示账号，仅用于虚构资料\n统一演示密码：'+password+'\n'+
                      '\n'.join('demo_'+role for role in DEMO_ROLES)+'\n')
    result={'teaching_only':True,'accounts':['demo_'+role for role in DEMO_ROLES],
            'cases':cases,'access_file':str(access)}
    temporary=marker.with_name(marker.name+'.'+secrets.token_hex(6)+'.tmp')
    private_write(temporary,json.dumps({'schema':1,'teaching_only':True,'result':result},ensure_ascii=False,indent=2))
    temporary.replace(marker)
    return result


if __name__=='__main__':
    result=asyncio.run(bootstrap(Settings()))
    print('云端虚构示例账号与两个工单已准备；登录资料保存在服务器私有文件：'+result['access_file'])
