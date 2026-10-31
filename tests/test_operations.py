from contextlib import closing
"""运维控制使用隔离虚构数据，不访问真实运行目录。"""
import asyncio
import json
import sqlite3
import multiprocessing

import unittest
import tempfile
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine


def quota_worker(path, queue):
    from zhengwu_precheck.infrastructure.quota_repository import QuotaRepository, QuotaLimits, QuotaExceeded
    async def run():
        engine = create_async_engine('sqlite+aiosqlite:///' + path)
        repository = QuotaRepository(engine)
        try:
            await repository.reserve('shared-service', 10, QuotaLimits(3, 30, 10))
            queue.put('accepted')
        except QuotaExceeded:
            queue.put('limited')
        finally:
            await engine.dispose()
    asyncio.run(run())


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp_path = Path(self.temporary.name)

    def test_multiple_processes_cannot_multiply_total_budget(self):
        from zhengwu_precheck.infrastructure.quota_repository import QuotaRepository
        path = str(self.tmp_path / 'shared.db')
        async def initialize():
            engine = create_async_engine('sqlite+aiosqlite:///' + path)
            await QuotaRepository(engine).initialize()
            await engine.dispose()
        asyncio.run(initialize())
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        workers = [context.Process(target=quota_worker, args=(path, queue)) for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(20)
            if worker.is_alive():
                worker.terminate()
                worker.join()
            self.assertEqual(worker.exitcode, 0)
        results = [queue.get(timeout=5) for _ in workers]
        queue.close()
        self.assertEqual(results.count('accepted'), 3)
        self.assertEqual(results.count('limited'), 3)

    def test_backup_requires_quiesce_and_release_preflight_requires_tls(self):
        from zhengwu_precheck.infrastructure.backup import backup
        from zhengwu_precheck.core.services.operations import release_preflight
        with self.assertRaises(ValueError):
            backup(self.tmp_path / 'db', self.tmp_path, self.tmp_path / 'copy')
        with self.assertRaises(ValueError):
            release_preflight({}, repository=Path.cwd())

    def test_quota_multi_scope_rollback_and_expired_lease_keeps_budget(self):
        from zhengwu_precheck.infrastructure.quota_repository import QuotaRepository, QuotaLimits, QuotaExceeded
        async def run():
            engine = create_async_engine('sqlite+aiosqlite:///' + (self.tmp_path/'multi.db').as_posix())
            now = [1000]
            repository = QuotaRepository(engine, clock=lambda: now[0])
            await repository.initialize()
            low = QuotaLimits(10, 10, 1, lease_seconds=5)
            await repository.reserve('global', 10, low)
            with self.assertRaises(QuotaExceeded):
                await repository.reserve_many([('user',low),('global',low)], 1)
            self.assertEqual((await repository.usage('user'))['requests'], 0)
            now[0] += 6
            with self.assertRaises(QuotaExceeded):
                await repository.reserve('global', 1, low)
            self.assertEqual((await repository.usage('global'))['unresolved'], 1)
            await engine.dispose()
        asyncio.run(run())

    def test_quota_reservation_shared_restart_and_unknown_usage(self):
        tmp_path = self.tmp_path
        from zhengwu_precheck.infrastructure.quota_repository import QuotaRepository, QuotaLimits, QuotaExceeded

        async def run():
            engine = create_async_engine('sqlite+aiosqlite:///' + (tmp_path / 'quota.db').as_posix())
            first = QuotaRepository(engine, clock=lambda: 1000)
            await first.initialize()
            limits = QuotaLimits(daily_requests=3, daily_tokens=100, concurrent=1, lease_seconds=5)
            reservation = await first.reserve('operator:fictional:llm', 60, limits)
            with self.assertRaises(QuotaExceeded):
                await first.reserve('operator:fictional:llm', 1, limits)
            await first.settle(reservation.id, actual_tokens=None)
            second = QuotaRepository(engine, clock=lambda: 1010)
            with self.assertRaises(QuotaExceeded):
                await second.reserve('operator:fictional:llm', 50, limits)
            small = await second.reserve('operator:fictional:llm', 30, limits)
            await second.settle(small.id, actual_tokens=10)
            assert (await second.usage('operator:fictional:llm'))['tokens'] == 70
            await engine.dispose()
        asyncio.run(run())


    def test_diagnostic_redaction_does_not_change_original(self):
        tmp_path = self.tmp_path
        from zhengwu_precheck.utils.redaction import redact
        original = {'content': '姓名：张三 电话13812345678 身份证110101199001011234 a@example.com', 'token': 'secret-value'}
        result = redact(original)
        assert '13812345678' not in result['content']
        assert '110101199001011234' not in result['content']
        assert 'a@example.com' not in result['content']
        assert '张三' not in result['content']
        assert result['token'] == '[已隐藏]'
        assert original['token'] == 'secret-value'


    def test_backup_checksums_restore_isolated_and_refuses_overwrite(self):
        tmp_path = self.tmp_path
        from zhengwu_precheck.infrastructure.backup import backup, restore
        live = tmp_path / 'live'
        live.mkdir()
        with closing(sqlite3.connect(live / 'app.db')) as conn:
            conn.execute('create table materials(id text, path text)')
            conn.execute("insert into materials values ('fictional','uploads/a.txt')")
            conn.commit()
        (live / 'uploads').mkdir()
        (live / 'uploads/a.txt').write_text('虚构材料', encoding='utf-8')
        snapshot = tmp_path / 'snapshot'
        manifest = backup(live / 'app.db', live, snapshot, quiesced=True)
        assert manifest['files']['uploads/a.txt']
        target = tmp_path / 'restored'
        restore(snapshot, target)
        with closing(sqlite3.connect(target / 'app.db')) as conn:
            assert conn.execute('select count(*) from materials').fetchone()[0] == 1
        assert (target / 'uploads/a.txt').read_text(encoding='utf-8') == '虚构材料'
        with self.assertRaises(FileExistsError):
            restore(snapshot, target)
        (snapshot / 'uploads/a.txt').write_text('tampered', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '校验'):
            restore(snapshot, tmp_path / 'bad')
        assert not (tmp_path / 'bad').exists()


    def test_retention_unknown_classes_never_automatically_delete(self):
        tmp_path = self.tmp_path
        from zhengwu_precheck.core.services.retention import classify, archive_reviewed
        records = [dict(id='e', category='evaluation', created_at=0), dict(id='m', category='material', created_at=0)]
        plan = classify(records, now=100 * 86400)
        assert plan[0]['action'] == 'supervisor_review'
        assert plan[1]['action'] == 'hold_policy_unconfirmed'
        with self.assertRaises(PermissionError):
            archive_reviewed(records, tmp_path / 'archive.json', supervisor_id=None, approved_ids={'e'})
        result = archive_reviewed(records, tmp_path / 'archive.json', supervisor_id='fictional-supervisor', approved_ids={'e'})
        assert result['archived_ids'] == ['e']
        assert len(records) == 2  # 原始业务记录永不在此处删除。

    def test_backup_requires_referenced_knowledge_and_active_material_files(self):
        from zhengwu_precheck.infrastructure.backup import backup
        live = self.tmp_path / 'live'
        live.mkdir()
        with closing(sqlite3.connect(live / 'app.db')) as conn:
            conn.executescript("""CREATE TABLE knowledge_versions(storage_key TEXT,index_key TEXT);
                CREATE TABLE private_materials(id TEXT,consent INTEGER);
                INSERT INTO knowledge_versions VALUES('knowledge/source.md','knowledge/index.faiss');
                INSERT INTO private_materials VALUES('fictional',1);""")
            conn.commit()
        for relative in ['knowledge/source.md','knowledge/index.faiss','private-materials/fictional.bin']:
            target = live / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'fictional teaching data')
        for index, relative in enumerate(['knowledge/source.md','knowledge/index.faiss','private-materials/fictional.bin']):
            with self.subTest(relative=relative):
                target = live / relative
                target.unlink()
                with self.assertRaisesRegex(ValueError, '引用|缺失'):
                    backup(live / 'app.db', live, self.tmp_path / f'missing-{index}', quiesced=True)
                target.write_bytes(b'fictional teaching data')
        manifest = backup(live / 'app.db', live, self.tmp_path / 'complete', quiesced=True)
        self.assertIn('private-materials/fictional.bin', manifest['files'])
