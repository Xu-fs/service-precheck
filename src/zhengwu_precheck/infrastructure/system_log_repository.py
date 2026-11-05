"""持久化最少技术字段；查询任务时不读取业务正文、文件名或账号。"""
from datetime import date, datetime, timedelta, timezone
import logging
import re
import time
from uuid import UUID, uuid4

from sqlalchemy import text

from zhengwu_precheck.core.errors import AppError

LOCAL_TIMEZONE = timezone(timedelta(hours=8))
TASK_NAMES = {"chat": "问答任务", "knowledge": "知识处理", "ticket": "工单后台任务"}
LEVEL_NAMES = {"info": "正常", "warning": "警告", "error": "错误"}
logger = logging.getLogger(__name__)


def query_options(values, *, failures=False):
    """日期按北京时间自然日解释，结束日期包含当日。"""
    try:
        if failures and values.get("level"):
            raise ValueError
        page, size = int(values.get("page", 1)), int(values.get("page_size", 25))
        if not 1 <= page <= 100000 or not 1 <= size <= 100:
            raise ValueError
        result = {"page": page, "page_size": size}
        for name, allowed in (("level", LEVEL_NAMES), ("task_type", TASK_NAMES)):
            if values.get(name):
                if values[name] not in allowed:
                    raise ValueError
                result[name] = values[name]
        for name in ("request_id", "task_id"):
            if values.get(name):
                result[name] = str(UUID(values[name]))
        if values.get("error_code"):
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", values["error_code"]):
                raise ValueError
            result["error_code"] = values["error_code"]
        for name in ("start", "end"):
            if values.get(name):
                day = date.fromisoformat(values[name])
                value = datetime.combine(day, datetime.min.time(), LOCAL_TIMEZONE)
                result[name] = (value + (timedelta(days=1) if name == "end" else timedelta())).timestamp()
        if "start" in result and "end" in result and result["start"] >= result["end"]:
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError):
        raise AppError(422, "INVALID_LOG_FILTER", "请检查日期范围、错误码、编号及分页参数") from None


class SystemLogRepository:
    def __init__(self, engine):
        self.engine = engine

    async def initialize(self):
        async with self.engine.begin() as conn:
            await conn.execute(text("""CREATE TABLE IF NOT EXISTS system_logs(
                id TEXT PRIMARY KEY,occurred_at REAL NOT NULL,level TEXT NOT NULL,event TEXT NOT NULL,
                method TEXT,route TEXT,status_code INTEGER,duration_ms REAL,request_id TEXT,
                error_code TEXT,task_type TEXT,task_id TEXT)"""))
            for columns in ("occurred_at,id", "request_id", "error_code,occurred_at"):
                name = columns.replace(",", "_")
                await conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_system_logs_{name} ON system_logs({columns})"))

    async def record(self, *, event="request", level="info", method=None, route=None,
                     status_code=None, duration_ms=None, request_id=None, error_code=None,
                     task_type=None, task_id=None):
        # 调用方只传框架路由模板和内部错误码，绝不传URL、异常消息或用户正文。
        values = {"id": str(uuid4()), "occurred_at": time.time(), "level": level, "event": event,
                  "method": method, "route": route, "status_code": status_code, "duration_ms": duration_ms,
                  "request_id": request_id, "error_code": error_code, "task_type": task_type, "task_id": task_id}
        try:
            async with self.engine.begin() as conn:
                await conn.execute(text("""INSERT INTO system_logs VALUES(:id,:occurred_at,:level,:event,
                    :method,:route,:status_code,:duration_ms,:request_id,:error_code,:task_type,:task_id)"""), values)
                # 只轮转本表：保留近30天且最多20000条，不触碰业务或审计记录。
                await conn.execute(text("DELETE FROM system_logs WHERE occurred_at<:cutoff"),
                                   {"cutoff": time.time() - 30 * 86400})
                await conn.execute(text("""DELETE FROM system_logs WHERE id IN
                    (SELECT id FROM system_logs ORDER BY occurred_at DESC,id DESC LIMIT -1 OFFSET 20000)"""))
        except Exception:
            # 数据库不可用时不影响原请求；控制台仅输出固定技术码。
            logger.warning("SYSTEM_LOG_WRITE_FAILED")

    async def task_failure(self, task_type, error_code, task_id=None):
        await self.record(event="task_failed", level="error", task_type=task_type,
                          error_code=error_code, task_id=task_id)

    async def logs(self, values):
        return await self._query("SELECT * FROM system_logs", query_options(values))

    async def failures(self, values):
        # 每个来源仅投影技术字段；旧版本已存在的失败任务同样可查。
        source = """SELECT id AS task_id,'chat' AS task_type,'failed' AS status,
                error_code,request_id,created_at AS occurred_at,NULL AS attempts,NULL AS stage
            FROM messages WHERE sender_type='assistant' AND status='failed'
            UNION ALL
            SELECT id,'knowledge',status,error_code,NULL,COALESCE(finished_at,created_at),attempts,stage
            FROM knowledge_jobs WHERE status='failed'
            UNION ALL
            SELECT id,task_type,'worker_error',error_code,request_id,occurred_at,NULL,NULL
            FROM system_logs WHERE event='task_failed' AND task_id IS NULL"""
        return await self._query(source, query_options(values, failures=True))

    async def _query(self, source, options):
        clauses, params = [], dict(options)
        for key in ("level", "error_code", "request_id", "task_type", "task_id"):
            if key in options:
                clauses.append(f"{key}=:{key}")
        for key, operator in (("start", ">="), ("end", "<")):
            if key in options:
                clauses.append(f"occurred_at{operator}:{key}")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = f"WITH records AS ({source}) SELECT {{columns}} FROM records{where}"
        params.update(limit=options["page_size"], offset=(options["page"] - 1) * options["page_size"])
        async with self.engine.connect() as conn:
            total = (await conn.execute(text(query.format(columns="COUNT(*)")), params)).scalar_one()
            # id与task_id都唯一；第二排序键避免同时间记录在分页时跳动。
            order = "task_type,task_id" if "AS task_id" in source else "id"
            rows = (await conn.execute(text(query.format(columns="*") +
                f" ORDER BY occurred_at DESC,{order} DESC LIMIT :limit OFFSET :offset"), params)).mappings().all()
        return {"items": [dict(row) for row in rows], "total": total,
                "page": options["page"], "page_size": options["page_size"]}


def display_time(timestamp):
    return datetime.fromtimestamp(timestamp, LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
