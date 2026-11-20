"""获批策略的隔离运行记录处置。绝不扫描业务库、上传目录或关联材料。

仅接纳明确standalone且references为空的独立evaluation/diagnostic JSON。
签名策略与监督员身份认证均由仓库外管理系统提供，默认dry-run。
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from zhengwu_precheck.utils.redaction import redact

SAFE_CLASSES={'evaluation','diagnostic'}


def verify_policy(signed, key, *, now):
    if not isinstance(key,bytes) or len(key)<32:
        raise ValueError('策略签名密钥须由外部安全提供且至少32字节')
    policy=signed['policy']
    body=json.dumps(policy,sort_keys=True,separators=(',',':')).encode()
    expected=hmac.new(key,body,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,str(signed.get('signature',''))):
        raise ValueError('留存策略签名无效')
    if not policy.get('version') or policy['expires_at']<=now:
        raise ValueError('留存策略过期或版本缺失')
    for category, rule in policy['classes'].items():
        if category not in SAFE_CLASSES or rule['action'] not in {'archive','archive_delete'} or type(rule['days']) is not int or rule['days']<=0:
            raise ValueError('策略包含未批准的数据类别或期限')
    return policy


def dispose(directory,signed,key,*,supervisor,verify_supervisor,approved,archive,audit,now=None,dry_run=True):
    """逐ID+原文件哈希批准；verify_supervisor必须验证外部实际监督员身份。

    调用方应暂停该独立记录目录写入。执行过程先持久归档，再持久处置审计，
    最后在再次核对哈希后删除；任一步失败都保留尚未删除的原件。
    """
    now=time.time() if now is None else now
    policy=verify_policy(signed,key,now=now)
    if not supervisor or not verify_supervisor(supervisor):
        raise PermissionError('需要通过外部身份系统验证的监督员批准')
    raw=Path(directory)
    if raw.is_symlink() or not raw.is_dir():
        raise ValueError('独立记录目录无效')
    directory=raw.resolve()
    archive,audit=Path(archive).resolve(),Path(audit).resolve()
    if archive.is_relative_to(directory) or audit.is_relative_to(directory) or directory.is_relative_to(archive):
        raise ValueError('归档和审计必须在扫描目录之外')
    result=[]
    for path in sorted(directory.glob('*.json')):
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ValueError('禁止符号链接记录')
        if path.stat().st_size>1024*1024:
            raise ValueError('独立记录超出1MB审查限制')
        data=path.read_bytes()
        record=json.loads(data)
        digest=hashlib.sha256(data).hexdigest()
        rule=policy['classes'].get(record.get('category'))
        eligible=(rule is not None and record.get('standalone') is True and record.get('references')==[]
                  and approved.get(record.get('id'))==digest)
        action='hold_linked_or_unapproved'
        if eligible:
            action=rule['action'] if now-record['created_at']>=rule['days']*86400 else 'retain'
        entry=dict(id=record.get('id'),action=action,source_sha256=digest,policy_version=policy['version'])
        result.append(entry)
        if dry_run or action not in {'archive','archive_delete'}:
            continue
        archive.mkdir(parents=True,exist_ok=True)
        audit.parent.mkdir(parents=True,exist_ok=True)
        archived=archive/(str(uuid4())+'.json')
        with archived.open('x',encoding='utf-8') as stream:
            json.dump(dict(record=redact(record),disposition=entry,supervisor=supervisor),stream,ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        with audit.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(dict(**entry,supervisor=supervisor,archive_id=archived.name,occurred_at=now,event='archived_delete_authorized' if action=='archive_delete' else 'archived'),ensure_ascii=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        if action=='archive_delete':
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise ValueError('原件已变化，拒绝删除')
            path.unlink()
            with audit.open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(dict(**entry,supervisor=supervisor,occurred_at=now,event='deleted'))+'\n')
                stream.flush()
                os.fsync(stream.fileno())
    return result
