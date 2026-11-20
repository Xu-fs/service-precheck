"""分类留存审查：无获批期限的类别保留，归档不删除业务记录或关联文件。"""
import json
import time
from pathlib import Path
from zhengwu_precheck.utils.redaction import redact


def classify(records, *, now=None):
    now = time.time() if now is None else now
    return [dict(id=row['id'], category=row['category'],
                 action='supervisor_review' if row['category']=='evaluation' and now-row['created_at']>=90*86400
                 else 'retain' if row['category']=='evaluation' else 'hold_policy_unconfirmed') for row in records]


def archive_reviewed(records, destination, *, supervisor_id, approved_ids, now=None):
    """调用方必须验证监督员身份；此离线接口要求逐ID批准，只写脱敏归档副本。"""
    if not supervisor_id:
        raise PermissionError('须经已认证监督员逐项批准')
    eligible = {row['id'] for row in classify(records, now=now) if row['action']=='supervisor_review'}
    if not set(approved_ids).issubset(eligible):
        raise ValueError('包含未到期或期限未确认的记录')
    payload = dict(supervisor_id=supervisor_id, occurred_at=time.time(),
                   archived_ids=sorted(approved_ids), deletion=False,
                   records=redact([row for row in records if row['id'] in approved_ids]))
    with Path(destination).open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return payload
