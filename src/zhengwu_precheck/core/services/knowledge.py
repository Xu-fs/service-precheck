"""可恢复知识处理任务：逐页提取、切片、API嵌入、FAISS文件，绝不执行OCR假结果。"""
import asyncio
import hashlib
import sys
import sysconfig
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import faiss
import numpy as np
from zhengwu_precheck.utils.knowledge_extract import extract_pages
from sqlalchemy import text
from zhengwu_precheck.core.errors import AppError


async def extract_in_process(path, filename, timeout=30):
    """只管理本次创建的提取进程；超时/取消直接终止，避免后台解析线程继续占资源。"""
    helper = Path(__file__).resolve().parents[2] / "utils" / "knowledge_extract.py"
    # Windows虚拟环境的python.exe是转发启动器；结束启动器会留下真正Python子进程。
    # 直接使用基础解释器并仅加入可信虚拟环境依赖目录，取消时可终止实际解析进程。
    bootstrap = "import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')"
    process = await asyncio.create_subprocess_exec(sys._base_executable, "-I", "-c", bootstrap,
        sysconfig.get_paths()["purelib"], str(helper), str(path), filename,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        raw, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode or len(raw) > 400000:
            raise AppError(422, "DOCUMENT_PROCESSING_FAILED", "教学文件无法安全提取，请拆分或人工补充")
        pages = json.loads(raw)
        if not isinstance(pages, list) or not pages or len(pages) > 20 or sum(len(p["text"]) for p in pages) > 50000:
            raise ValueError("提取响应无效")
        return pages
    except TimeoutError:
        raise AppError(422, "DOCUMENT_EXTRACTION_TIMEOUT", "提取超过时限，请拆分教学文件后重试") from None
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        # 取消本协程不能中断回收子进程，避免保留仍在解析的进程。
        wait_task = asyncio.create_task(process.wait())
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError:
            await asyncio.shield(wait_task)
            raise


def make_chunks(pages):
    chunks = []
    for page in pages:
        body = page["text"].strip()
        for offset in range(0, len(body), 440):
            end = min(offset + 500, len(body))
            chunks.append({"id": str(uuid4()), "chunk_no": len(chunks) + 1, "page_no": page["page"], "text_content": body[offset:end],
                           "starts_mid": int(offset > 0 and body[offset - 1] not in "。！？\n"),
                           "ends_mid": int(end < len(body) and body[end - 1] not in "。！？\n")})
            if end == len(body):
                break
    return chunks


class KnowledgeService:
    def __init__(self, repository, qa, settings):
        self.repo, self.qa, self.settings = repository, qa, settings
        self.wakeup = asyncio.Event()

    def signature(self):
        config = self.qa.config("EMBEDDING")
        values = {"provider": config.provider, "model": config.model, "url": config.base_url.rstrip("/"),
                  "revision": os.getenv("EMBEDDING_MODEL_REVISION", "")}
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    async def run(self):
        # 数据库队列而非仅内存BackgroundTasks；重启后可恢复过期租约的任务。
        while True:
            try:
                while await self.run_once():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                # 记录/网络暂不可用时等待，不将内部响应正文写入页面或日志。
                pass
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), 3)
            except TimeoutError:
                pass

    async def run_once(self):
        job = await self.repo.claim_job()
        if not job:
            return False
        try:
            if job["attempts"] >= 3:
                raise AppError(409, "JOB_RETRY_LIMIT", "任务多次中断，请人工检查")
            await asyncio.wait_for(self._process(job), 240)
        except asyncio.CancelledError:
            # 主动退出恢复为queued；突然退出的任务由租约恢复。
            async with self.repo.engine.begin() as conn:
                await conn.execute(text("UPDATE knowledge_jobs SET status='queued',stage='queued',lease_until=NULL,lease_token=NULL WHERE id=:id AND status='running' AND lease_token=:token"), {"id": job["id"], "token": job["lease_token"]})
            raise
        except Exception as error:
            code = error.code if isinstance(error, AppError) else "DOCUMENT_PROCESSING_FAILED"
            async with self.repo.engine.begin() as conn:
                changed = await conn.execute(text("UPDATE knowledge_jobs SET status='failed',stage='failed',error_code=:code,finished_at=:now,lease_token=NULL WHERE id=:id AND status='running' AND lease_token=:token"),
                    {"id": job["id"], "code": code, "now": time.time(), "token": job["lease_token"]})
                if changed.rowcount == 1:
                    await conn.execute(text("UPDATE knowledge_versions SET processing_status='failed',notes=:note WHERE id=:id AND state='draft'"),
                        {"id": job["version_id"], "note": "处理失败，请检查文档、模型配置后重试；内部响应未公开。"})
        return True

    async def _assert_job(self, conn, job):
        await self.repo._lock(conn)
        active = (await conn.execute(text("SELECT 1 FROM knowledge_jobs WHERE id=:id AND status='running' AND lease_token=:token AND lease_until>:now"),
            {"id": job["id"], "token": job["lease_token"], "now": time.time()})).first()
        if not active:
            raise AppError(409, "JOB_LEASE_LOST", "任务已被恢复或重新接手，旧处理结果不再写入")

    async def _process(self, job):
        signature = self.signature()
        async with self.repo.engine.connect() as conn:
            row = (await conn.execute(text("SELECT v.*,d.archived FROM knowledge_versions v JOIN knowledge_documents d ON d.id=v.document_id WHERE v.id=:id"), {"id": job["version_id"]})).mappings().one()
        if row["state"] != "draft" or row["archived"]:
            raise AppError(409, "KNOWLEDGE_LOCKED", "版本已锁定或归档")
        if row["manual_pages"]:
            pages = json.loads(row["manual_pages"])
        else:
            path = (self.settings.runtime_dir / row["storage_key"]).resolve()
            if not path.is_relative_to(self.settings.runtime_dir.resolve()):
                raise ValueError("存储路径无效")
            pages = await extract_in_process(path, row["filename"])
        serialized = json.dumps(pages, ensure_ascii=False)
        missing = [p["page"] for p in pages if not p["text"].strip()]
        async with self.repo.engine.begin() as conn:
            await self._assert_job(conn, job)
            await conn.execute(text("UPDATE knowledge_versions SET pages_json=:pages WHERE id=:id"), {"pages": serialized, "id": row["id"]})
            if missing:
                await conn.execute(text("UPDATE knowledge_versions SET processing_status='needs_text',notes=:note WHERE id=:id"),
                    {"id": row["id"], "note": "存在无法提取文字的页面，请进行客户批准的OCR或逐页人工补充：" + str(missing)})
                await conn.execute(text("UPDATE knowledge_jobs SET status='complete',stage='needs_text',finished_at=:now,lease_token=NULL WHERE id=:id"), {"id": job["id"], "now": time.time()})
                return
            await conn.execute(text("UPDATE knowledge_jobs SET stage='chunking' WHERE id=:id"), {"id": job["id"]})
        chunks = make_chunks(pages)
        if not chunks:
            raise ValueError("正文为空")
        async with self.repo.engine.begin() as conn:
            await self._assert_job(conn, job)
            await conn.execute(text("UPDATE knowledge_jobs SET stage='embedding' WHERE id=:id"), {"id": job["id"]})
        vectors = []
        from zhengwu_precheck.core.services.model_budget import model_principal
        identity=model_principal.set('staff:'+row['created_by'])
        try:
            for start in range(0, len(chunks), 32):
                vectors.append(await self.qa.embed([c["text_content"] for c in chunks[start:start + 32]]))
        finally:
            model_principal.reset(identity)
        if signature != self.signature():
            raise AppError(503, "KNOWLEDGE_INDEX_INCOMPATIBLE", "处理期间模型配置变化，请重新建立新草稿")
        matrix = np.ascontiguousarray(np.concatenate(vectors).astype("float32"))
        if matrix.ndim != 2 or matrix.shape[0] != len(chunks) or not np.isfinite(matrix).all() or np.any(np.linalg.norm(matrix, axis=1) == 0):
            raise ValueError("嵌入响应无效")
        faiss.normalize_L2(matrix)
        index = faiss.IndexFlatIP(matrix.shape[1]); index.add(matrix)
        index_path = self.settings.runtime_dir / "knowledge" / "indexes" / (row["id"] + "-" + job["lease_token"] + ".faiss")
        index_path.parent.mkdir(parents=True, exist_ok=True)
        raw_index = faiss.serialize_index(index).tobytes()
        temporary = index_path.with_suffix(".tmp")
        await asyncio.to_thread(temporary.write_bytes, raw_index)
        temporary.replace(index_path)
        async with self.repo.engine.begin() as conn:
            await self._assert_job(conn, job)
            current = (await conn.execute(text("SELECT v.state,d.archived FROM knowledge_versions v JOIN knowledge_documents d ON d.id=v.document_id WHERE v.id=:id"), {"id": row["id"]})).mappings().one()
            if current["state"] != "draft" or current["archived"]:
                raise AppError(409, "KNOWLEDGE_LOCKED", "版本已锁定或归档")
            await conn.execute(text("DELETE FROM knowledge_chunks WHERE version_id=:id"), {"id": row["id"]})
            for chunk in chunks:
                await conn.execute(text("INSERT INTO knowledge_chunks VALUES(:id,:version,:chunk_no,:page_no,:text_content,:starts_mid,:ends_mid)"), {**chunk, "version": row["id"]})
            await conn.execute(text("UPDATE knowledge_versions SET processing_status='ready',checksum=:hash,index_key=:key,embedding_signature=:signature,notes=:notes WHERE id=:id"),
                {"hash": hashlib.sha256(raw_index).hexdigest(), "key": index_path.relative_to(self.settings.runtime_dir).as_posix(), "signature": signature,
                 "notes": "文本、片段与向量已就绪；尚需审核发布才能用于新问答。", "id": row["id"]})
            await conn.execute(text("UPDATE knowledge_jobs SET status='complete',stage='ready',finished_at=:now,lease_token=NULL WHERE id=:id"), {"id": job["id"], "now": time.time()})
