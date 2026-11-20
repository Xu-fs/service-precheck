"""模型统一持久预算；未知消耗保守保留，不保存请求正文。"""
import asyncio
from contextvars import ContextVar
import json
from urllib.parse import urlsplit
from zhengwu_precheck.infrastructure.quota_repository import QuotaExceeded
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.utils.redaction import redact

model_principal=ContextVar('model_principal',default='background-knowledge')


def minimize(value):
    if isinstance(value,str):
        return redact(value)
    if isinstance(value,list):
        return [minimize(v) for v in value]
    if isinstance(value,dict):
        return {k:minimize(v) for k,v in value.items()}
    return value


class ModelBudget:
    def __init__(self,repository,limits,owner_limits=None):
        self.repo,self.limits,self.owner_limits=repository,limits,owner_limits or limits

    async def send(self,client,url,payload,headers):
        clean=minimize(payload)
        maximum=clean.get('max_tokens',clean.get('options',{}).get('num_predict',0))
        estimate=len(json.dumps(clean,ensure_ascii=False).encode('utf-8'))+(maximum if type(maximum) is int else 0)
        service='embedding' if urlsplit(str(url)).path.rstrip('/').split('/')[-1] in {'embeddings','embed'} else 'llm'
        owner=model_principal.get()
        try:
            reservation=await self.repo.reserve_many([('all-model-services',self.limits),
                ('model-owner:'+owner,self.owner_limits),
                ('model-service:'+service,self.limits),
                ('model-owner-service:'+owner+':'+service,self.owner_limits)],estimate)
        except QuotaExceeded:
            raise AppError(429,'MODEL_QUOTA_EXCEEDED','调用额度已达到上限，请稍后重试或使用人工咨询。',True) from None
        usage=None
        try:
            response=await client.post(url,json=clean,headers=headers)
            try:
                candidate=(response.json().get('usage') or {}).get('total_tokens')
                if type(candidate) is int and candidate>=0:
                    usage=candidate
            except (ValueError,AttributeError,TypeError):
                pass
            return response
        finally:
            settle=asyncio.create_task(self.repo.settle(reservation.id,usage))
            try:
                await asyncio.shield(settle)
            except asyncio.CancelledError:
                await asyncio.shield(settle)
                raise
