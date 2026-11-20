"""同主机多进程共享HTTP滑动窗口；不保存IP、用户名或Cookie原文。"""
import hashlib
import math
import time
from sqlalchemy import text

from zhengwu_precheck.core.errors import AppError


class SharedRateLimiter:
    def __init__(self,engine,clock=time.time):
        self.engine,self.clock=engine,clock

    async def initialize(self):
        async with self.engine.begin() as connection:
            await connection.execute(text('''CREATE TABLE IF NOT EXISTS request_rate_events(
                id INTEGER PRIMARY KEY, scope_hash TEXT NOT NULL,
                occurred_at REAL NOT NULL, expires_at REAL NOT NULL)'''))
            await connection.execute(text('CREATE INDEX IF NOT EXISTS ix_request_rate_scope ON request_rate_events(scope_hash,occurred_at)'))
            await connection.execute(text('CREATE INDEX IF NOT EXISTS ix_request_rate_expiry ON request_rate_events(expires_at)'))

    async def check(self,key,count=20,window=60):
        """每个服务端key+window独立计数；边界到期即释放，拒绝不延长窗口。

        count/window来自服务端固定路由配置，不能由请求参数控制。
        每次最多回收256条到期事件；持续拒绝不会生成新事件。
        """
        if not isinstance(key,str) or not key or type(count) is not int or count<=0:
            raise ValueError('限流key不能为空且次数须为正整数')
        if isinstance(window,bool) or not isinstance(window,(int,float)) or not math.isfinite(window) or window<=0:
            raise ValueError('限流窗口须为有限正数')
        scope=hashlib.sha256((str(float(window))+'\x00'+key).encode()).hexdigest()
        now=self.clock()
        async with self.engine.connect() as connection:
            await connection.execute(text('BEGIN IMMEDIATE'))
            try:
                await connection.execute(text('''DELETE FROM request_rate_events WHERE id IN
                    (SELECT id FROM request_rate_events WHERE expires_at<=:now ORDER BY expires_at LIMIT 256)'''),{'now':now})
                active=(await connection.execute(text('''SELECT COUNT(*) FROM request_rate_events
                    WHERE scope_hash=:scope AND occurred_at>:start AND expires_at>:now'''),
                    {'scope':scope,'start':now-window,'now':now})).scalar_one()
                limited=active>=count
                if not limited:
                    await connection.execute(text('''INSERT INTO request_rate_events(scope_hash,occurred_at,expires_at)
                        VALUES(:scope,:now,:expires)'''),{'scope':scope,'now':now,'expires':now+window})
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        if limited:
            raise AppError(429,'RATE_LIMITED','请求过于频繁，请稍后重试或使用人工服务入口',retryable=True)
