"""同机 SQLite 共享配额。只存不可逆作用域摘要，不存提示词或供应商凭证。"""
from dataclasses import dataclass
import hashlib
import time
from uuid import uuid4
from sqlalchemy import text


class QuotaExceeded(Exception):
    """请求未获准调用供应商。"""


@dataclass(frozen=True)
class QuotaLimits:
    daily_requests: int
    daily_tokens: int
    concurrent: int
    lease_seconds: int = 180
    minute_requests: int = 60

    def __post_init__(self):
        if any(type(x) is not int or x <= 0 for x in vars(self).values()):
            raise ValueError('配额必须是正整数')


@dataclass(frozen=True)
class Reservation:
    id: str
    estimate_tokens: int


class QuotaRepository:
    def __init__(self, engine, clock=time.time):
        self.engine, self.clock = engine, clock

    async def initialize(self):
        async with self.engine.begin() as conn:
            await conn.execute(text('''CREATE TABLE IF NOT EXISTS quota_reservations(
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, created_at REAL NOT NULL,
                day INTEGER NOT NULL, tokens INTEGER NOT NULL, lease_until REAL NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('reserved','settled','unknown')))'''))
            await conn.execute(text('CREATE INDEX IF NOT EXISTS ix_quota_scope ON quota_reservations(scope,day)'))

    @staticmethod
    def _scope(scope):
        if not isinstance(scope, str) or not scope:
            raise ValueError('配额作用域不能为空')
        return hashlib.sha256(scope.encode()).hexdigest()

    async def reserve(self, scope, estimate_tokens, limits):
        return await self.reserve_many([(scope, limits)], estimate_tokens)

    async def reserve_many(self, scopes, estimate_tokens):
        """原子预留操作者和服务总额；任一超额全部回滚。返回共享预留组ID。"""
        if type(estimate_tokens) is not int or estimate_tokens < 0 or not scopes:
            raise ValueError('预估token须为非负整数且作用域不能为空')
        now, identifier = self.clock(), str(uuid4())
        day = int(now // 86400)  # UTC自然日，部署文档明确边界。
        normalized = [(self._scope(scope), limit) for scope, limit in scopes]
        if len({scope for scope, _ in normalized}) != len(normalized):
            raise ValueError('重复配额作用域')
        async with self.engine.connect() as conn:
            await conn.execute(text('BEGIN IMMEDIATE'))
            try:
                await conn.execute(text("UPDATE quota_reservations SET status='unknown' WHERE status='reserved' AND lease_until<=:now"), {'now': now})
                for index, (scope, limits) in enumerate(normalized):
                    stats = (await conn.execute(text('''SELECT COUNT(*) AS requests,
                        COALESCE(SUM(CASE WHEN day=:day THEN tokens ELSE 0 END),0) AS tokens,
                        COALESCE(SUM(CASE WHEN status='reserved' AND lease_until>:now THEN 1 ELSE 0 END),0) AS active,
                        COALESCE(SUM(CASE WHEN created_at>:minute THEN 1 ELSE 0 END),0) AS minute
                        FROM quota_reservations WHERE scope=:scope AND (day=:day OR lease_until>:now OR created_at>:minute)'''),
                        dict(scope=scope, day=day, now=now, minute=now-60))).mappings().one()
                    # Daily count excludes an active reservation carried from yesterday.
                    daily = (await conn.execute(text('SELECT COUNT(*) FROM quota_reservations WHERE scope=:scope AND day=:day'), dict(scope=scope,day=day))).scalar_one()
                    if daily >= limits.daily_requests or stats['tokens'] + estimate_tokens > limits.daily_tokens or stats['active'] >= limits.concurrent or stats['minute'] >= limits.minute_requests:
                        raise QuotaExceeded('当前请求达到共享调用额度，请稍后重试或使用人工入口')
                    await conn.execute(text('INSERT INTO quota_reservations VALUES(:id,:scope,:now,:day,:tokens,:lease,\'reserved\')'),
                        dict(id=identifier + ':' + str(index), scope=scope, now=now, day=day, tokens=estimate_tokens, lease=now+limits.lease_seconds))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return Reservation(identifier, estimate_tokens)

    async def settle(self, reservation_id, actual_tokens=None):
        if actual_tokens is not None and (type(actual_tokens) is not int or actual_tokens < 0):
            raise ValueError('usage token须为非负整数')
        async with self.engine.begin() as conn:
            await conn.execute(text('''UPDATE quota_reservations SET
                tokens=CASE WHEN :actual IS NULL THEN tokens ELSE :actual END,
                status=CASE WHEN :actual IS NULL THEN 'unknown' ELSE 'settled' END,
                lease_until=0 WHERE id LIKE :prefix AND status!='settled' '''),
                {'actual': actual_tokens, 'prefix': reservation_id + ':%'})

    async def release(self, reservation_id):
        """仅当调用尚未发送时使用；请求次数仍计数，token退还。"""
        await self.settle(reservation_id, actual_tokens=0)

    async def usage(self, scope):
        async with self.engine.connect() as conn:
            row = (await conn.execute(text('''SELECT COUNT(*) AS requests,COALESCE(SUM(tokens),0) AS tokens,
                COALESCE(SUM(CASE WHEN status='unknown' OR (status='reserved' AND lease_until<=:now) THEN 1 ELSE 0 END),0) AS unresolved
                FROM quota_reservations WHERE scope=:scope AND day=:day'''),
                {'scope': self._scope(scope), 'day': int(self.clock()//86400), 'now': self.clock()})).mappings().one()
        return dict(row)
