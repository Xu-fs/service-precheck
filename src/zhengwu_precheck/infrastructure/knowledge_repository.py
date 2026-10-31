"""知识版本、审核发布及任务仓储；发布内容不可原地修改或物理删除。"""
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4
from sqlalchemy import text, bindparam

from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.core.staff_permissions import allows

SCENARIOS = {"residence-permit": "rental-demo", "sole-trader-registration": "standard-demo"}
KINDS = {"guide": "办事指南", "checklist": "材料清单", "rule": "预检规则", "workflow": "人工处理流程"}
SCHEMA = [
    "CREATE TABLE IF NOT EXISTS knowledge_schema_versions(version INTEGER PRIMARY KEY)",
    """CREATE TABLE IF NOT EXISTS knowledge_documents(id TEXT PRIMARY KEY,title TEXT NOT NULL,matter_id TEXT NOT NULL,
        scenario_id TEXT NOT NULL,kind TEXT NOT NULL,visibility TEXT NOT NULL DEFAULT 'public',archived INTEGER NOT NULL DEFAULT 0,created_by TEXT NOT NULL,created_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_versions(id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
        version_no INTEGER NOT NULL,rule_version TEXT NOT NULL,filename TEXT NOT NULL,source_uri TEXT NOT NULL,
        storage_key TEXT,manual_pages TEXT,pages_json TEXT,processing_status TEXT NOT NULL DEFAULT 'queued',
        state TEXT NOT NULL DEFAULT 'draft',created_by TEXT NOT NULL,created_at REAL NOT NULL,
        checksum TEXT,index_key TEXT,embedding_signature TEXT,notes TEXT,UNIQUE(document_id,version_no),UNIQUE(document_id,rule_version))""",
    """CREATE TABLE IF NOT EXISTS knowledge_chunks(id TEXT PRIMARY KEY,version_id TEXT NOT NULL REFERENCES knowledge_versions(id),
        chunk_no INTEGER NOT NULL,page_no INTEGER,text_content TEXT NOT NULL,starts_mid INTEGER NOT NULL,ends_mid INTEGER NOT NULL,
        UNIQUE(version_id,chunk_no))""",
    """CREATE TABLE IF NOT EXISTS knowledge_jobs(id TEXT PRIMARY KEY,version_id TEXT NOT NULL REFERENCES knowledge_versions(id),
        status TEXT NOT NULL DEFAULT 'queued',stage TEXT NOT NULL DEFAULT 'queued',attempts INTEGER NOT NULL DEFAULT 0,
        lease_until REAL,error_code TEXT,created_at REAL NOT NULL,finished_at REAL,lease_token TEXT)""",
    """CREATE TABLE IF NOT EXISTS knowledge_releases(id TEXT PRIMARY KEY,title TEXT NOT NULL,matter_id TEXT NOT NULL,
        scenario_id TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'draft',created_by TEXT NOT NULL,reviewed_by TEXT,
        created_at REAL NOT NULL,effective_from REAL NOT NULL,effective_to REAL,revision INTEGER NOT NULL DEFAULT 1,submitted_by TEXT)""",
    "CREATE TABLE IF NOT EXISTS knowledge_release_items(release_id TEXT REFERENCES knowledge_releases(id),version_id TEXT REFERENCES knowledge_versions(id),PRIMARY KEY(release_id,version_id))",
    "CREATE TABLE IF NOT EXISTS knowledge_scope_settings(matter_id TEXT NOT NULL,scenario_id TEXT NOT NULL,managed INTEGER NOT NULL,PRIMARY KEY(matter_id,scenario_id))",
    """CREATE TABLE IF NOT EXISTS knowledge_actions(id TEXT PRIMARY KEY,release_id TEXT REFERENCES knowledge_releases(id),
        actor_id TEXT NOT NULL,action TEXT NOT NULL,comment TEXT NOT NULL,created_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_feedback(id TEXT PRIMARY KEY,matter_id TEXT NOT NULL,version_id TEXT REFERENCES knowledge_versions(id),
        content TEXT NOT NULL,created_by TEXT NOT NULL,created_at REAL NOT NULL,resolution TEXT,resolved_by TEXT,resolved_at REAL)""",
    """CREATE TABLE IF NOT EXISTS qa_request_snapshots(request_id TEXT PRIMARY KEY,message_id TEXT NOT NULL REFERENCES messages(id),
        matter_id TEXT NOT NULL,scenario_id TEXT NOT NULL,prompt_version_id TEXT NOT NULL REFERENCES prompt_versions(id),
        prompt_content TEXT NOT NULL,knowledge_json TEXT NOT NULL,created_at REAL NOT NULL)""",
]


class KnowledgeRepository:
    def __init__(self, engine, runtime_dir):
        self.engine, self.runtime_dir = engine, Path(runtime_dir)

    async def initialize(self):
        async with self.engine.begin() as conn:
            for statement in SCHEMA:
                await conn.execute(text(statement))
            versions = (await conn.execute(text("SELECT version FROM knowledge_schema_versions"))).scalars().all()
            if versions and versions not in ([1], [2]):
                raise RuntimeError("知识表版本不兼容，须先显式迁移；不会删除已有资料")
            # 兼容本批中间版：只增列，不重建/删除用户表或已保存的历史。
            additions = {
                "knowledge_documents": {"visibility": "TEXT NOT NULL DEFAULT 'public'"},
                "knowledge_versions": {"embedding_signature": "TEXT"},
                "knowledge_releases": {"submitted_by": "TEXT"},
                "knowledge_jobs": {"lease_token": "TEXT"},
            }
            for table, columns in additions.items():
                existing = {row[1] for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).all()}
                for name, definition in columns.items():
                    if name not in existing:
                        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
            # 中间版内部流程可能沿用public默认；修正为内部，保留正文和历史快照。
            await conn.execute(text("UPDATE knowledge_documents SET visibility='staff' WHERE kind='workflow'"))
            # 老包无submitted_by时从追加动作恢复，不能让历史提交人绕过自审禁令。
            await conn.execute(text("""UPDATE knowledge_releases SET submitted_by=(SELECT actor_id FROM knowledge_actions
                WHERE release_id=knowledge_releases.id AND action='submit' ORDER BY created_at DESC LIMIT 1) WHERE submitted_by IS NULL"""))
            if versions == [1]:
                await conn.execute(text("UPDATE knowledge_schema_versions SET version=2 WHERE version=1"))
            else:
                await conn.execute(text("INSERT OR IGNORE INTO knowledge_schema_versions VALUES(2)"))

    async def _lock(self, conn):
        await conn.execute(text("UPDATE knowledge_schema_versions SET version=version WHERE version=2"))

    async def _log(self, conn, identity, action, entity, comment=""):
        await conn.execute(text("INSERT INTO audit_logs VALUES(:id,:actor,:action,'knowledge',:entity,'success',NULL,:now)"),
            {"id": str(uuid4()), "actor": identity["user_id"], "action": action, "entity": entity, "now": time.time()})

    def authorize(self, identity, permission, matter):
        if not allows(identity["grants"], permission, matter):
            raise AppError(403, "STAFF_PERMISSION_DENIED", "当前岗位或事项授权不允许此操作")

    def _scopes(self, identity, permission):
        scopes = [g["matter_id"] for g in identity["grants"] if allows([g], permission)]
        if not scopes:
            raise AppError(403, "STAFF_PERMISSION_DENIED", "当前岗位无权访问此功能")
        return scopes

    async def get_version(self, identity, version_id, permission="knowledge.read"):
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("""SELECT v.*,d.title,d.matter_id,d.scenario_id,d.kind,d.archived FROM knowledge_versions v
                JOIN knowledge_documents d ON d.id=v.document_id WHERE v.id=:id"""), {"id": version_id})).mappings().first()
        if not row or not allows(identity["grants"], permission, row["matter_id"]):
            raise AppError(404, "KNOWLEDGE_NOT_FOUND", "知识版本不存在或不在授权范围内")
        return dict(row)

    async def list_versions(self, identity, filters=None):
        scopes = self._scopes(identity, "knowledge.read")
        filters = filters or {}
        params = {"scopes": scopes, "all": int("*" in scopes)}
        clause = "(:all=1 OR d.matter_id IN :scopes)"
        for key in ("matter_id", "kind"):
            if filters.get(key):
                clause += f" AND d.{key}=:{key}"
                params[key] = filters[key]
        if filters.get("status"):
            clause += " AND v.processing_status=:status"
            params["status"] = filters["status"]
        query = text("""SELECT v.*,d.title,d.matter_id,d.scenario_id,d.kind,d.archived FROM knowledge_versions v
            JOIN knowledge_documents d ON d.id=v.document_id WHERE """ + clause + " ORDER BY v.created_at DESC LIMIT 100").bindparams(bindparam("scopes", expanding=True))
        async with self.engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(query, params)).mappings().all()]

    async def upload(self, identity, *, title, matter_id, scenario_id, kind, filename, source_uri, rule_version, raw, is_teaching, visibility=None):
        self.authorize(identity, "knowledge.manage", matter_id)
        if (SCENARIOS.get(matter_id) != scenario_id or kind not in KINDS or not is_teaching
                or not 1 <= len(title.strip()) <= 150 or not 1 <= len(source_uri.strip()) <= 1000
                or not 1 <= len(rule_version.strip()) <= 80):
            raise AppError(422, "INVALID_KNOWLEDGE", "请填写事项、情形、类型、版本及来源，并确认仅上传示例资料")
        filename = Path(filename.replace("\\", "/")).name
        visibility = visibility or ("staff" if kind == "workflow" else "public")
        if visibility not in {"public", "staff"} or (kind == "workflow" and visibility != "staff"):
            raise AppError(422, "INVALID_KNOWLEDGE", "知识可见范围无效")
        extension = Path(filename).suffix.lower()
        if extension not in {".md", ".pdf"} or not raw or len(raw) > 20 * 1024 * 1024:
            raise AppError(422, "INVALID_DOCUMENT", "仅支持20MB以内的Markdown或PDF")
        if extension == ".pdf" and not raw.startswith(b"%PDF-"):
            raise AppError(422, "INVALID_DOCUMENT", "文件不是有效的PDF")
        identifier, version_id, job_id = str(uuid4()), str(uuid4()), str(uuid4())
        storage = self.runtime_dir / "knowledge" / "uploads" / (version_id + extension)
        storage.parent.mkdir(parents=True, exist_ok=True)
        import asyncio
        await asyncio.to_thread(storage.write_bytes, raw)
        try:
            async with self.engine.begin() as conn:
                await conn.execute(text("INSERT INTO knowledge_documents(id,title,matter_id,scenario_id,kind,visibility,archived,created_by,created_at) VALUES(:id,:title,:matter,:scenario,:kind,:visibility,0,:actor,:now)"),
                    {"id": identifier, "title": title.strip(), "matter": matter_id, "scenario": scenario_id, "kind": kind, "visibility": visibility, "actor": identity["user_id"], "now": time.time()})
                await conn.execute(text("""INSERT INTO knowledge_versions(id,document_id,version_no,rule_version,filename,source_uri,storage_key,created_by,created_at)
                    VALUES(:id,:document,1,:version,:filename,:source,:storage,:actor,:now)"""), {"id": version_id, "document": identifier,
                    "version": rule_version.strip(), "filename": filename, "source": source_uri.strip(), "storage": storage.relative_to(self.runtime_dir).as_posix(), "actor": identity["user_id"], "now": time.time()})
                await conn.execute(text("INSERT INTO knowledge_jobs(id,version_id,created_at) VALUES(:id,:version,:now)"), {"id": job_id, "version": version_id, "now": time.time()})
                await self._log(conn, identity, "knowledge_uploaded", version_id)
        except Exception:
            storage.unlink(missing_ok=True)
            raise
        return {"id": identifier, "version_id": version_id, "job_id": job_id}

    async def new_version(self, identity, version_id, *, content, rule_version, page=None, expected_latest=None):
        source = await self.get_version(identity, version_id, "knowledge.manage")
        rule_version = rule_version.strip()
        if source["archived"] or not isinstance(content, str) or not content.strip() or len(content) > 50000 or not 1 <= len(rule_version.strip()) <= 80:
            raise AppError(422, "INVALID_KNOWLEDGE", "请填写不超过50000字符的示例正文和新版本号，归档文档不可修改")
        pages = json.loads(source["pages_json"] or "[]")
        if source["filename"].lower().endswith(".pdf"):
            if type(page) is not int or not 1 <= page <= len(pages):
                raise AppError(422, "INVALID_PAGE", "PDF人工补充须指定现有页码")
            pages[page - 1] = {"page": page, "text": content.strip(), "manual": True}
        else:
            pages = [{"page": None, "text": content.strip(), "manual": True}]
        if sum(len(p.get("text", "")) for p in pages) > 50000:
            raise AppError(422, "DOCUMENT_TOO_LONG", "文档全文不得超过50000字符")
        new_id, job_id = str(uuid4()), str(uuid4())
        async with self.engine.begin() as conn:
            await self._lock(conn)
            latest = (await conn.execute(text("SELECT MAX(version_no) FROM knowledge_versions WHERE document_id=:id"), {"id": source["document_id"]})).scalar()
            if expected_latest is not None and latest != expected_latest:
                raise AppError(409, "VERSION_CONFLICT", "文档已产生新草稿，请刷新后重试")
            exists = (await conn.execute(text("SELECT 1 FROM knowledge_versions WHERE document_id=:id AND rule_version=:version"), {"id": source["document_id"], "version": rule_version})).first()
            if exists:
                raise AppError(409, "VERSION_CONFLICT", "新版本号已存在")
            await conn.execute(text("""INSERT INTO knowledge_versions(id,document_id,version_no,rule_version,filename,source_uri,storage_key,manual_pages,created_by,created_at)
                VALUES(:id,:document,:seq,:version,:filename,:source,:storage,:pages,:actor,:now)"""),
                {"id": new_id, "document": source["document_id"], "seq": latest + 1, "version": rule_version.strip(), "filename": source["filename"],
                 "source": source["source_uri"], "storage": source["storage_key"], "pages": json.dumps(pages, ensure_ascii=False), "actor": identity["user_id"], "now": time.time()})
            await conn.execute(text("INSERT INTO knowledge_jobs(id,version_id,created_at) VALUES(:id,:version,:now)"), {"id": job_id, "version": new_id, "now": time.time()})
            await self._log(conn, identity, "knowledge_version_created", new_id)
        return {"version_id": new_id, "job_id": job_id, "document_id": source["document_id"]}

    async def retry(self, identity, version_id):
        version = await self.get_version(identity, version_id, "knowledge.manage")
        async with self.engine.begin() as conn:
            await self._lock(conn)
            row = (await conn.execute(text("SELECT state,processing_status FROM knowledge_versions WHERE id=:id"), {"id": version_id})).mappings().one()
            if version["archived"] or row["state"] != "draft" or row["processing_status"] in {"queued", "processing"}:
                raise AppError(409, "KNOWLEDGE_LOCKED", "已提交或正在处理的版本不能重建，请创建新草稿")
            job_id = str(uuid4())
            await conn.execute(text("UPDATE knowledge_versions SET processing_status='queued' WHERE id=:id"), {"id": version_id})
            await conn.execute(text("INSERT INTO knowledge_jobs(id,version_id,created_at) VALUES(:id,:version,:now)"), {"id": job_id, "version": version_id, "now": time.time()})
        return {"job_id": job_id, "version_id": version_id}

    async def claim_job(self):
        async with self.engine.begin() as conn:
            await self._lock(conn)
            await conn.execute(text("UPDATE knowledge_jobs SET status='queued',stage='queued',lease_token=NULL WHERE status='running' AND (lease_until IS NULL OR lease_until<:now)"), {"now": time.time()})
            row = (await conn.execute(text("SELECT * FROM knowledge_jobs WHERE status='queued' ORDER BY created_at LIMIT 1"))).mappings().first()
            if not row:
                return None
            token = str(uuid4())
            await conn.execute(text("UPDATE knowledge_jobs SET status='running',stage='extracting',attempts=attempts+1,lease_until=:lease,lease_token=:token WHERE id=:id"), {"lease": time.time() + 300, "id": row["id"], "token": token})
            await conn.execute(text("UPDATE knowledge_versions SET processing_status='processing' WHERE id=:id AND state='draft'"), {"id": row["version_id"]})
        return {**dict(row), "lease_token": token}

    async def job(self, identity, job_id):
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("SELECT * FROM knowledge_jobs WHERE id=:id"), {"id": job_id})).mappings().first()
        if not row:
            raise AppError(404, "JOB_NOT_FOUND", "处理任务不存在")
        await self.get_version(identity, row["version_id"])
        result = dict(row)
        result.pop("lease_token", None)
        return result

    async def release(self, identity, release_id, permission="knowledge.read"):
        async with self.engine.connect() as conn:
            row = (await conn.execute(text("SELECT * FROM knowledge_releases WHERE id=:id"), {"id": release_id})).mappings().first()
            items = (await conn.execute(text("""SELECT v.*,d.title,d.kind,d.archived FROM knowledge_release_items i JOIN knowledge_versions v ON v.id=i.version_id
                JOIN knowledge_documents d ON d.id=v.document_id WHERE i.release_id=:id"""), {"id": release_id})).mappings().all()
        if not row or not allows(identity["grants"], permission, row["matter_id"]):
            raise AppError(404, "RELEASE_NOT_FOUND", "版本包不存在或不在授权范围内")
        return {**dict(row), "items": [dict(item) for item in items]}

    async def list_releases(self, identity):
        scopes = self._scopes(identity, "knowledge.read")
        query = text("SELECT * FROM knowledge_releases WHERE (:all=1 OR matter_id IN :scopes) ORDER BY created_at DESC LIMIT 100").bindparams(bindparam("scopes", expanding=True))
        async with self.engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(query, {"all": int("*" in scopes), "scopes": scopes})).mappings().all()]

    async def create_release(self, identity, *, title, version_ids, effective_from, effective_to=None):
        if not isinstance(version_ids, list) or not 1 <= len(version_ids) <= 10 or len(set(version_ids)) != len(version_ids) or not 1 <= len(title.strip()) <= 150:
            raise AppError(422, "INVALID_RELEASE", "请选择1至10个不重复版本并填写名称")
        items = [await self.get_version(identity, value) for value in version_ids]
        matter, scenario = items[0]["matter_id"], items[0]["scenario_id"]
        self.authorize(identity, "rule.submit", matter)
        if any(item["matter_id"] != matter or item["scenario_id"] != scenario for item in items) or len({item["document_id"] for item in items}) != len(items):
            raise AppError(422, "INVALID_RELEASE", "版本包必须属于同一事项和情形，每份文档只能选择一个版本")
        try:
            start = float(effective_from)
            end = float(effective_to) if effective_to not in (None, "") else None
            import math
            if not math.isfinite(start) or (end is not None and (not math.isfinite(end) or end <= start)):
                raise ValueError()
        except (ValueError, TypeError):
            raise AppError(422, "INVALID_RELEASE", "生效时间区间无效") from None
        identifier = str(uuid4())
        async with self.engine.begin() as conn:
            await self._lock(conn)
            # 一个草稿版本只进入一个待审核版本包，防止并发状态串用。
            for item in items:
                row = (await conn.execute(text("SELECT state,processing_status FROM knowledge_versions WHERE id=:id"), {"id": item["id"]})).mappings().one()
                if item["archived"] or row["state"] != "draft" or row["processing_status"] != "ready":
                    raise AppError(409, "KNOWLEDGE_NOT_READY", "请选择未归档、索引就绪的草稿版本")
            await conn.execute(text("INSERT INTO knowledge_releases(id,title,matter_id,scenario_id,created_by,created_at,effective_from,effective_to) VALUES(:id,:title,:matter,:scenario,:actor,:now,:start,:end)"),
                {"id": identifier, "title": title.strip(), "matter": matter, "scenario": scenario, "actor": identity["user_id"], "now": time.time(), "start": start, "end": end})
            for item in items:
                await conn.execute(text("INSERT INTO knowledge_release_items VALUES(:release,:version)"), {"release": identifier, "version": item["id"]})
        return {"id": identifier}

    async def release_action(self, identity, release_id, action, comment="", expected_revision=None):
        permission = {"submit": "rule.submit", "approve": "rule.review", "reject": "rule.review", "publish": "rule.publish", "retire": "rule.publish"}.get(action)
        if not permission:
            raise AppError(422, "INVALID_ACTION", "无效操作")
        release = await self.release(identity, release_id, permission)
        if not isinstance(comment, str) or not 1 <= len(comment.strip()) <= 1000:
            raise AppError(422, "INVALID_COMMENT", "请填写1至1000字符的操作说明")
        async with self.engine.begin() as conn:
            await self._lock(conn)
            current = (await conn.execute(text("SELECT * FROM knowledge_releases WHERE id=:id"), {"id": release_id})).mappings().one()
            if expected_revision is not None and current["revision"] != expected_revision:
                raise AppError(409, "RELEASE_CONFLICT", "版本包状态已变化，请刷新")
            required = {"submit": "draft", "approve": "submitted", "reject": "submitted", "publish": "approved", "retire": "published"}[action]
            if current["state"] != required:
                raise AppError(409, "RELEASE_CONFLICT", "当前状态不允许该操作")
            items = (await conn.execute(text("""SELECT v.*,d.archived FROM knowledge_release_items i JOIN knowledge_versions v ON v.id=i.version_id
                JOIN knowledge_documents d ON d.id=v.document_id WHERE i.release_id=:id"""), {"id": release_id})).mappings().all()
            if action in {"approve", "reject"} and (identity["user_id"] in {current["created_by"], current["submitted_by"]} or any(i["created_by"] == identity["user_id"] for i in items)):
                raise AppError(403, "SELF_REVIEW_FORBIDDEN", "提交人或内容维护者不能审核自己的版本包")
            if action != "retire" and any(i["archived"] or i["processing_status"] != "ready" for i in items):
                raise AppError(409, "KNOWLEDGE_NOT_READY", "版本未就绪或已归档")
            if action == "submit" and any(i["state"] != "draft" for i in items):
                raise AppError(409, "RELEASE_CONFLICT", "成员版本已进入其他审核流程")
            if action == "publish":
                overlaps = (await conn.execute(text("""SELECT id FROM knowledge_releases WHERE state='published' AND matter_id=:matter AND scenario_id=:scenario
                    AND (effective_to IS NULL OR effective_to>:start) AND (:end IS NULL OR effective_from<:end)"""),
                    {"matter": current["matter_id"], "scenario": current["scenario_id"], "start": current["effective_from"], "end": current["effective_to"]})).first()
                if overlaps:
                    raise AppError(409, "RULE_VERSION_CONFLICT", "生效时间与已发布版本重叠，请先停用旧版本或调整新草稿生效时间")
                await conn.execute(text("INSERT INTO knowledge_scope_settings VALUES(:matter,:scenario,1) ON CONFLICT(matter_id,scenario_id) DO UPDATE SET managed=1"), {"matter": current["matter_id"], "scenario": current["scenario_id"]})
            target = {"submit": "submitted", "approve": "approved", "reject": "rejected", "publish": "published", "retire": "retired"}[action]
            await conn.execute(text("UPDATE knowledge_releases SET state=:state,revision=revision+1,submitted_by=CASE WHEN :action='submit' THEN :actor ELSE submitted_by END,reviewed_by=CASE WHEN :action='approve' THEN :actor ELSE reviewed_by END WHERE id=:id"),
                {"state": target, "action": action, "actor": identity["user_id"], "id": release_id})
            for item in items:
                await conn.execute(text("UPDATE knowledge_versions SET state=:state WHERE id=:id"), {"state": "draft" if action == "reject" else target, "id": item["id"]})
            await conn.execute(text("INSERT INTO knowledge_actions VALUES(:id,:release,:actor,:action,:comment,:now)"),
                {"id": str(uuid4()), "release": release_id, "actor": identity["user_id"], "action": action, "comment": comment.strip(), "now": time.time()})
            await self._log(conn, identity, "release_" + action, release_id)
        return {"id": release_id, "state": target}

    async def archive(self, identity, document_id):
        async with self.engine.begin() as conn:
            await self._lock(conn)
            row = (await conn.execute(text("SELECT * FROM knowledge_documents WHERE id=:id"), {"id": document_id})).mappings().first()
            if not row:
                raise AppError(404, "KNOWLEDGE_NOT_FOUND", "文档不存在")
            self.authorize(identity, "knowledge.manage", row["matter_id"])
            locked = (await conn.execute(text("SELECT 1 FROM knowledge_versions WHERE document_id=:id AND state IN ('submitted','approved','published')"), {"id": document_id})).first()
            if locked:
                raise AppError(409, "KNOWLEDGE_LOCKED", "文档仍在审核或已发布，请先撤出流程或停用版本包")
            await conn.execute(text("UPDATE knowledge_documents SET archived=1 WHERE id=:id"), {"id": document_id})
            await self._log(conn, identity, "knowledge_archived", document_id)
        return {"archived": True}

    async def capture(self, conn, matter_id, scenario_id):
        """在问答接纳事务内冻结版本ID及模板；这里只读DB，不执行网络或文件操作。"""
        now = time.time()
        prompt = (await conn.execute(text("SELECT v.* FROM prompt_versions v JOIN prompt_heads h ON v.matter_id=h.matter_id AND v.version=h.version WHERE v.matter_id=:matter"), {"matter": matter_id})).mappings().one()
        releases = (await conn.execute(text("SELECT id FROM knowledge_releases WHERE matter_id=:matter AND scenario_id=:scenario AND state='published' AND effective_from<=:now AND (effective_to IS NULL OR effective_to>:now)"), {"matter": matter_id, "scenario": scenario_id, "now": now})).scalars().all()
        managed = bool((await conn.execute(text("SELECT managed FROM knowledge_scope_settings WHERE matter_id=:matter AND scenario_id=:scenario"), {"matter": matter_id, "scenario": scenario_id})).scalar())
        versions = []
        for release_id in releases:
            versions.extend((await conn.execute(text("""SELECT i.version_id FROM knowledge_release_items i JOIN knowledge_versions v ON v.id=i.version_id
                JOIN knowledge_documents d ON d.id=v.document_id WHERE i.release_id=:id AND d.visibility='public' AND d.kind!='workflow'"""), {"id": release_id})).scalars().all())
        return {"matter_id": matter_id, "scenario_id": scenario_id, "managed": managed, "release_ids": releases, "version_ids": versions,
                "conflict": len(releases) > 1, "prompt": dict(prompt)}

    async def chunks_for_snapshot(self, snapshot):
        output = []
        async with self.engine.connect() as conn:
            for version_id in snapshot["version_ids"]:
                version = (await conn.execute(text("SELECT v.*,d.matter_id,d.scenario_id,d.kind FROM knowledge_versions v JOIN knowledge_documents d ON d.id=v.document_id WHERE v.id=:id"), {"id": version_id})).mappings().one()
                if version["matter_id"] != snapshot["matter_id"] or version["scenario_id"] != snapshot["scenario_id"]:
                    raise AppError(503, "KNOWLEDGE_SCOPE_MISMATCH", "知识版本范围异常，请转人工")
                chunks = (await conn.execute(text("SELECT * FROM knowledge_chunks WHERE version_id=:id ORDER BY chunk_no"), {"id": version_id})).mappings().all()
                output.append({**dict(version), "chunks": [dict(row) for row in chunks]})
        return output
