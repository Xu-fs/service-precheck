"""隔离虚构配额台账压力演练；不调用任何模型，不代表业务或供应商容量。"""
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from sqlalchemy.ext.asyncio import create_async_engine
from zhengwu_precheck.infrastructure.quota_repository import QuotaRepository, QuotaLimits, QuotaExceeded


async def main():
    directory=ROOT/'.venv/operations-demo'/('fictional-capacity-'+str(uuid4()))
    directory.mkdir(parents=True)
    engine=create_async_engine('sqlite+aiosqlite:///'+(directory/'quota.db').as_posix(),connect_args={'timeout':30})
    repository=QuotaRepository(engine)
    await repository.initialize()
    limits=QuotaLimits(15,150,100,minute_requests=100)
    async def request():
        try:
            reservation=await repository.reserve('fictional-global-service',10,limits)
            await repository.settle(reservation.id,10)
            return True
        except QuotaExceeded:
            return False
    started=time.perf_counter()
    results=await asyncio.gather(*(request() for _ in range(60)))
    elapsed=time.perf_counter()-started
    usage=await repository.usage('fictional-global-service')
    await engine.dispose()
    assert sum(results)==15 and usage['tokens']==150
    report=dict(load='synthetic_local_quota_only_no_model',attempts=60,admitted=sum(results),
                denied=results.count(False),elapsed_seconds=elapsed,attempts_per_second=60/elapsed,
                daily_tokens=usage['tokens'],production_capacity_verified=False)
    (directory/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':
    asyncio.run(main())
