"""第二批真实SQLite/FAISS行为测试；仅外部嵌入使用确定性替身。"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
AVAILABLE = importlib.util.find_spec("zhengwu_precheck.core.services.knowledge") is not None


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(AVAILABLE, "知识处理服务尚未实现")
        from zhengwu_precheck.infrastructure.database import ChatRepository
        from zhengwu_precheck.infrastructure.knowledge_repository import KnowledgeRepository
        from zhengwu_precheck.infrastructure.prompt_repository import PromptRepository
        from zhengwu_precheck.core.services.knowledge import KnowledgeService
        from zhengwu_precheck.settings import Settings
        from zhengwu_precheck.core.guide_rag import ApiConfig
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(runtime_dir=Path(self.temp.name))
        self.base = ChatRepository(Path(self.temp.name) / "qa.sqlite3")
        await self.base.initialize()
        self.repo = KnowledgeRepository(self.base.engine, Path(self.temp.name))
        await PromptRepository(self.base.engine).initialize()
        await self.repo.initialize()
        class Embedder:
            def config(self, prefix):
                return ApiConfig("openai-compatible", "fixture", "http://127.0.0.1:9999/v1", "TEST_KEY")
            async def embed(self, values):
                import numpy as np
                vectors = np.array([[1.0, float(i + 1)] for i in range(len(values))], dtype="float32")
                vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
                return vectors
        self.worker = KnowledgeService(self.repo, Embedder(), self.settings)
        self.owner = {"user_id": "owner", "grants": [{"role_code": "owner", "matter_id": "residence-permit"}]}
        self.supervisor = {"user_id": "supervisor", "grants": [{"role_code": "supervisor", "matter_id": "residence-permit"}]}

    async def asyncTearDown(self):
        if hasattr(self, "base"):
            await self.base.close()
            self.temp.cleanup()

    async def upload(self, raw=None, filename="示例.md"):
        return await self.repo.upload(self.owner, title="示例指南", matter_id="residence-permit", scenario_id="rental-demo", kind="guide",
                                     filename=filename, source_uri="自编示例资料", rule_version="DEMO-v1", raw=raw or "示例申请表包含姓名和居住地址。".encode(), is_teaching=True)

    async def processed(self):
        result = await self.upload()
        self.assertTrue(await self.worker.run_once())
        self.assertEqual((await self.repo.get_version(self.owner, result["version_id"]))["processing_status"], "ready")
        return result

    async def test_index_build_requires_review_then_published_snapshot(self):
        uploaded = await self.processed()
        async with self.base.engine.begin() as conn:
            before = await self.repo.capture(conn, "residence-permit", "rental-demo")
        self.assertEqual(before["version_ids"], [])
        release = await self.repo.create_release(self.owner, title="示例发布包", version_ids=[uploaded["version_id"]], effective_from=time.time()-1)
        await self.repo.release_action(self.owner, release["id"], "submit", "提交示例版本")
        from zhengwu_precheck.core.errors import AppError
        with self.assertRaises(AppError):
            await self.repo.release_action(self.owner, release["id"], "approve", "越权审核")
        await self.repo.release_action(self.supervisor, release["id"], "approve", "依据核对通过")
        await self.repo.release_action(self.supervisor, release["id"], "publish", "发布生效")
        async with self.base.engine.begin() as conn:
            snapshot = await self.repo.capture(conn, "residence-permit", "rental-demo")
        self.assertTrue(snapshot["managed"])
        self.assertEqual(snapshot["version_ids"], [uploaded["version_id"]])
        versions = await self.repo.chunks_for_snapshot(snapshot)
        self.assertIn("居住地址", versions[0]["chunks"][0]["text_content"])
        await self.repo.release_action(self.supervisor, release["id"], "retire", "示例规则停用")
        self.assertEqual(len(await self.repo.chunks_for_snapshot(snapshot)), 1, "旧请求仍可引用旧版本")
        async with self.base.engine.begin() as conn:
            after = await self.repo.capture(conn, "residence-permit", "rental-demo")
        self.assertTrue(after["managed"])
        self.assertEqual(after["version_ids"], [])

    async def test_background_embeddings_keep_upload_operator_and_restore_context(self):
        from zhengwu_precheck.core.services.model_budget import model_principal
        seen=[]
        original=self.worker.qa.embed
        async def embed(values):
            seen.append(model_principal.get())
            return await original(values)
        self.worker.qa.embed=embed
        before=model_principal.get()
        await self.processed()
        self.assertEqual(seen,['staff:owner'])
        self.assertEqual(model_principal.get(),before)

    async def test_scanned_pdf_requires_manual_text_and_new_version(self):
        from io import BytesIO
        from pypdf import PdfWriter
        writer = PdfWriter(); writer.add_blank_page(width=200, height=200)
        buffer = BytesIO(); writer.write(buffer)
        uploaded = await self.upload(buffer.getvalue(), "扫描示例.pdf")
        await self.worker.run_once()
        old = await self.repo.get_version(self.owner, uploaded["version_id"])
        self.assertEqual(old["processing_status"], "needs_text")
        new = await self.repo.new_version(self.owner, old["id"], content="人工补充的示例材料是申请表。", rule_version="DEMO-v2", page=1, expected_latest=1)
        await self.worker.run_once()
        self.assertEqual((await self.repo.get_version(self.owner, new["version_id"]))["processing_status"], "ready")
        self.assertEqual((await self.repo.get_version(self.owner, old["id"]))["processing_status"], "needs_text")

    async def test_self_review_and_overlapping_release_are_rejected(self):
        from zhengwu_precheck.core.errors import AppError
        uploaded = await self.processed()
        mixed = {"user_id": "owner", "grants": self.owner["grants"] + self.supervisor["grants"]}
        release = await self.repo.create_release(self.owner, title="示例包", version_ids=[uploaded["version_id"]], effective_from=time.time()-1)
        await self.repo.release_action(self.owner, release["id"], "submit", "提交")
        with self.assertRaises(AppError) as failure:
            await self.repo.release_action(mixed, release["id"], "approve", "本人审核")
        self.assertEqual(failure.exception.code, "SELF_REVIEW_FORBIDDEN")
        await self.repo.release_action(self.supervisor, release["id"], "approve", "复核通过")
        await self.repo.release_action(self.supervisor, release["id"], "publish", "发布")
        with self.assertRaises(AppError):
            await self.repo.archive(self.owner, uploaded["id"])
        new = await self.repo.new_version(self.owner, uploaded["version_id"], content="新的示例申请表包含姓名。", rule_version="DEMO-v2", expected_latest=1)
        await self.worker.run_once()
        next_release = await self.repo.create_release(self.owner, title="新示例包", version_ids=[new["version_id"]], effective_from=time.time()-1)
        await self.repo.release_action(self.owner, next_release["id"], "submit", "提交")
        await self.repo.release_action(self.supervisor, next_release["id"], "approve", "通过")
        with self.assertRaises(AppError) as failure:
            await self.repo.release_action(self.supervisor, next_release["id"], "publish", "冲突发布")
        self.assertEqual(failure.exception.code, "RULE_VERSION_CONFLICT")

    async def test_pdf_false_content_stream_limit_and_extraction_process_timeout(self):
        from io import BytesIO
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject
        from zhengwu_precheck.core.services.knowledge import extract_in_process
        from zhengwu_precheck.core.errors import AppError
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        stream = DecodedStreamObject()
        stream.set_data(b"% oversized teaching comment\n" + b"x" * (2 * 1024 * 1024))
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = BytesIO(); writer.write(output)
        item = await self.upload(output.getvalue(), "大页面示例.pdf")
        await self.worker.run_once()
        self.assertEqual((await self.repo.get_version(self.owner, item["version_id"]))["processing_status"], "failed")
        compressed = PdfWriter(); page = compressed.add_blank_page(width=200, height=200)
        repeated = DecodedStreamObject()
        repeated.set_data(b"%" + b"a" * (600 * 1024) + b"\nBT (Teaching.) Tj ET\n")
        from pypdf.generic import ArrayObject
        ref = compressed._add_object(repeated.flate_encode())
        page[NameObject("/Contents")] = ArrayObject([ref] * 16)
        bomb = BytesIO(); compressed.write(bomb)
        compressed_item = await self.upload(bomb.getvalue(), "重复压缩示例.pdf")
        await self.worker.run_once()
        self.assertEqual((await self.repo.get_version(self.owner, compressed_item["version_id"]))["processing_status"], "failed")
        path = Path(self.temp.name) / "timeout.md"
        path.write_text("仅用于进程超时验证的示例文本。", encoding="utf-8")
        with self.assertRaises(AppError) as failure:
            await extract_in_process(path, "timeout.md", timeout=0.000001)
        self.assertEqual(failure.exception.code, "DOCUMENT_EXTRACTION_TIMEOUT")
        self.assertEqual((await extract_in_process(path, "timeout.md"))[0]["text"], "仅用于进程超时验证的示例文本。")

    async def test_extraction_cancellation_reaps_actual_child(self):
        from unittest.mock import patch
        from zhengwu_precheck.core.services.knowledge import extract_in_process
        create_process = asyncio.create_subprocess_exec
        started = asyncio.Event()
        children = []

        async def start_slow_child(*args, **kwargs):
            child = await create_process(sys._base_executable, "-I", "-c",
                "import time; time.sleep(30)", **kwargs)
            children.append(child)
            started.set()
            return child

        with patch("zhengwu_precheck.core.services.knowledge.asyncio.create_subprocess_exec", side_effect=start_slow_child):
            task = asyncio.create_task(extract_in_process(Path(self.temp.name) / "cancel.md", "cancel.md"))
            try:
                await asyncio.wait_for(started.wait(), 10)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsNotNone(children[0].returncode, "取消完成时实际提取子进程必须已经退出")
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                for child in children:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()

    async def test_pdf_form_xobject_is_rejected_instead_of_publishing_partial_text(self):
        from io import BytesIO
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject, ArrayObject, DictionaryObject, NumberObject
        from zhengwu_precheck.core.errors import AppError
        writer = PdfWriter(); page = writer.add_blank_page(width=200, height=200)
        font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")}))
        fonts = DictionaryObject({NameObject("/F1"): font})
        form = DecodedStreamObject()
        form.set_data(b"%" + b"a" * (2100 * 1024) + b"\nBT /F1 12 Tf (Hidden necessary requirement.) Tj ET\n")
        form[NameObject("/Type")] = NameObject("/XObject")
        form[NameObject("/Subtype")] = NameObject("/Form")
        form[NameObject("/BBox")] = ArrayObject([NumberObject(i) for i in (0, 0, 200, 200)])
        form[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
        reference = writer._add_object(form.flate_encode())
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts,
            NameObject("/XObject"): DictionaryObject({NameObject("/Big"): reference})})
        base = DecodedStreamObject()
        base.set_data(b"BT /F1 12 Tf (Teaching requirement one.) Tj ET\n/Big Do\n")
        page[NameObject("/Contents")] = writer._add_object(base)
        output = BytesIO(); writer.write(output)
        item = await self.upload(output.getvalue(), "含复杂Form的示例.pdf")
        await self.worker.run_once()
        state = await self.repo.get_version(self.owner, item["version_id"])
        self.assertEqual(state["processing_status"], "failed")
        with self.assertRaises(AppError) as caught:
            await self.repo.create_release(self.owner, title="部分正文不能发布", version_ids=[item["version_id"]], effective_from=time.time()-1)
        self.assertEqual(caught.exception.code, "KNOWLEDGE_NOT_READY")

    async def test_version_number_whitespace_conflicts_instead_of_storage_failure(self):
        from zhengwu_precheck.core.errors import AppError
        item = await self.processed()
        for version in (" DEMO-v1", "DEMO-v1 "):
            with self.assertRaises(AppError) as caught:
                await self.repo.new_version(self.owner, item["version_id"], content="示例资料新正文。",
                    rule_version=version, expected_latest=1)
            self.assertEqual(caught.exception.code, "VERSION_CONFLICT")

    async def test_window_and_other_matter_cannot_modify(self):
        from zhengwu_precheck.core.errors import AppError
        uploaded = await self.processed()
        for role, matter in [("window", "residence-permit"), ("owner", "sole-trader-registration")]:
            identity = {"user_id": "other", "grants": [{"role_code": role, "matter_id": matter}]}
            with self.assertRaises(AppError):
                await self.repo.new_version(identity, uploaded["version_id"], content="越权示例内容。", rule_version="DEMO-v2")

    async def test_expired_claim_is_fenced_and_failed_task_can_retry(self):
        from sqlalchemy import text
        from zhengwu_precheck.core.errors import AppError
        uploaded = await self.upload()
        old = await self.repo.claim_job()
        async with self.base.engine.begin() as conn:
            await conn.execute(text("UPDATE knowledge_jobs SET lease_until=0 WHERE id=:id"), {"id": old["id"]})
        resumed = await self.repo.claim_job()
        self.assertEqual(resumed["id"], old["id"])
        self.assertNotEqual(resumed["lease_token"], old["lease_token"])
        with self.assertRaises(AppError) as failure:
            await self.worker._process(old)
        self.assertEqual(failure.exception.code, "JOB_LEASE_LOST")
        await self.worker._process(resumed)
        self.assertEqual((await self.repo.get_version(self.owner, uploaded["version_id"]))["processing_status"], "ready")
        failed = await self.upload(b"not utf8\xff", "错误示例.md")
        await self.worker.run_once()
        self.assertEqual((await self.repo.get_version(self.owner, failed["version_id"]))["processing_status"], "failed")
        await self.repo.retry(self.owner, failed["version_id"])
        self.assertEqual((await self.repo.get_version(self.owner, failed["version_id"]))["processing_status"], "queued")

    async def test_actual_submitter_and_content_maintainer_cannot_self_review(self):
        from zhengwu_precheck.core.errors import AppError
        uploaded = await self.processed()
        release = await self.repo.create_release(self.owner, title="示例包", version_ids=[uploaded["version_id"]], effective_from=time.time()-1)
        submitter = {"user_id": "another", "grants": self.owner["grants"] + self.supervisor["grants"]}
        await self.repo.release_action(submitter, release["id"], "submit", "代为提交")
        with self.assertRaises(AppError) as failure:
            await self.repo.release_action(submitter, release["id"], "approve", "自审")
        self.assertEqual(failure.exception.code, "SELF_REVIEW_FORBIDDEN")
        await self.repo.release_action(self.supervisor, release["id"], "reject", "需调整")
        content_owner = {"user_id": "author", "grants": self.owner["grants"] + self.supervisor["grants"]}
        new = await self.repo.new_version(content_owner, uploaded["version_id"], content="另一作者的示例申请表包含地址。", rule_version="DEMO-v2", expected_latest=1)
        await self.worker.run_once()
        next_release = await self.repo.create_release(self.owner, title="新示例包", version_ids=[new["version_id"]], effective_from=time.time()-1)
        await self.repo.release_action(self.owner, next_release["id"], "submit", "提交")
        with self.assertRaises(AppError) as failure:
            await self.repo.release_action(content_owner, next_release["id"], "approve", "内容作者自审")
        self.assertEqual(failure.exception.code, "SELF_REVIEW_FORBIDDEN")

    async def test_concurrent_publication_has_one_winner_and_stale_revision_rejected(self):
        from zhengwu_precheck.core.errors import AppError
        one, two = await self.processed(), await self.processed()
        releases = []
        for item in [one, two]:
            release = await self.repo.create_release(self.owner, title="并发示例包", version_ids=[item["version_id"]], effective_from=time.time()-1)
            await self.repo.release_action(self.owner, release["id"], "submit", "提交", expected_revision=1)
            await self.repo.release_action(self.supervisor, release["id"], "approve", "审核", expected_revision=2)
            releases.append(release)
        result = await asyncio.gather(*[self.repo.release_action(self.supervisor, r["id"], "publish", "发布", expected_revision=3) for r in releases], return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in result), 1)
        self.assertEqual([r.code for r in result if isinstance(r, AppError)], ["RULE_VERSION_CONFLICT"])
        winner = next(r for r in result if isinstance(r, dict))
        with self.assertRaises(AppError) as failure:
            await self.repo.release_action(self.supervisor, winner["id"], "retire", "旧状态", expected_revision=3)
        self.assertEqual(failure.exception.code, "RELEASE_CONFLICT")

    async def test_schema_one_additive_migration_preserves_versions_and_internal_visibility(self):
        from sqlalchemy import text
        item = await self.processed()
        async with self.base.engine.begin() as conn:
            await conn.execute(text("ALTER TABLE knowledge_releases DROP COLUMN submitted_by"))
            await conn.execute(text("ALTER TABLE knowledge_jobs DROP COLUMN lease_token"))
            await conn.execute(text("ALTER TABLE knowledge_documents DROP COLUMN visibility"))
            await conn.execute(text("ALTER TABLE knowledge_versions DROP COLUMN embedding_signature"))
            await conn.execute(text("UPDATE knowledge_schema_versions SET version=1"))
        await self.repo.initialize()
        row = await self.repo.get_version(self.owner, item["version_id"])
        self.assertEqual(row["rule_version"], "DEMO-v1")
        async with self.base.engine.connect() as conn:
            self.assertEqual((await conn.execute(text("SELECT version FROM knowledge_schema_versions"))).scalar(), 2)
        from zhengwu_precheck.core.errors import AppError
        with self.assertRaises(AppError):
            await self.repo.upload(self.owner, title="内部示例", matter_id="residence-permit", scenario_id="rental-demo", kind="workflow", filename="流程.md", source_uri="示例", rule_version="v1", raw=b"fixture", is_teaching=True, visibility="public")


if __name__ == "__main__":
    unittest.main()
