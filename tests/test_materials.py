import unittest
import tempfile
from pathlib import Path
from sqlalchemy import text
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.core.errors import AppError


class MaterialFixture:
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = ChatRepository(Path(self.tmp.name) / 'db.sqlite')
        await self.chat.initialize()
        async with self.chat.engine.begin() as c:
            await c.execute(text("INSERT INTO users VALUES ('u','u','unused','active'),('v','v','unused','active')"))
            await c.execute(text("INSERT INTO conversations(id,user_id,created_at) VALUES ('case','u',0)"))
        from zhengwu_precheck.infrastructure.material_repository import MaterialRepository
        self.repo = MaterialRepository(self.chat.engine, Path(self.tmp.name) / 'private')
        await self.repo.initialize()

    async def asyncTearDown(self):
        await self.chat.close()
        self.tmp.cleanup()

    async def upload(self):
        return await self.repo.store('u', 'case', 'fictional.pdf', b'%PDF-1.4\nfictional test', True)


class MaterialTests(MaterialFixture, unittest.IsolatedAsyncioTestCase):
    async def test_private_storage_requires_owner_and_explicit_consent(self):
        for user, consent, code in [(None, True, 'LOGIN_REQUIRED'), ('v', True, 'CASE_NOT_FOUND'), ('u', False, 'MATERIAL_CONSENT_REQUIRED')]:
            with self.assertRaises(AppError) as e:
                await self.repo.store(user, 'case', 'x.pdf', b'%PDF-1.4', consent)
            self.assertEqual(e.exception.code, code)
        item = await self.upload()
        self.assertEqual(await self.repo.read_bytes('u', item['id']), b'%PDF-1.4\nfictional test')
        self.assertNotIn('path', item)
        with self.assertRaises(AppError):
            await self.repo.read_bytes('v', item['id'])

    async def test_signature_limits_and_filename_are_enforced(self):
        for name, body in [('../x.pdf', b'%PDF-1.4'), ('x.exe', b'MZ'), ('x.png', b'bad'), ('x.pdf', b'%PDF-' + b'x' * (10*1024*1024))]:
            with self.assertRaises(AppError):
                await self.repo.store('u', 'case', name, body, True)

    async def test_manual_fields_have_explicit_source_and_immutable_set(self):
        item = await self.upload()
        corrected = await self.repo.correct_fields('u', item['id'], 1, 'identity', {'name':'教学甲','address':'虚构路1号','date':'2026-10-01'})
        self.assertEqual(corrected['extraction']['source'], 'user_transcription')
        material_set = await self.repo.create_set('u', 'case', [item['id']])
        await self.repo.correct_fields('u', item['id'], 2, 'identity', {'name':'教学乙'})
        with self.assertRaises(AppError) as e:
            await self.repo.get_set('u', material_set['id'])
        self.assertEqual(e.exception.code, 'MATERIAL_SET_STALE')

    async def test_revocation_invalidates_sets_and_removes_bytes(self):
        item = await self.upload()
        material_set = await self.repo.create_set('u', 'case', [item['id']])
        await self.repo.revoke('u', item['id'])
        with self.assertRaises(AppError):
            await self.repo.get_set('u', material_set['id'])
        with self.assertRaises(AppError):
            await self.repo.read_bytes('u', item['id'])
        self.assertEqual(list((Path(self.tmp.name)/'private').glob('*.bin')), [])

    async def test_untrusted_processing_fails_closed_and_is_not_missing_material(self):
        item = await self.upload()
        result = await self.repo.process('u', item['id'])
        self.assertEqual(result['processing_status'], 'unavailable')
        self.assertEqual(result['extraction']['error_code'], 'MATERIAL_ISOLATION_UNAVAILABLE')
        self.assertEqual(result['extraction']['fields'], {})

class PrecheckTests(unittest.TestCase):
    def check(self,items,matter='residence-permit',scenario='rental-demo',version='teaching-2026-10-v1'):
        from zhengwu_precheck.core.services.materials import evaluate
        return evaluate(matter,scenario,version,items)

    def item(self,kind,name='教学甲',address='虚构路1号',**fields):
        return {'id':kind,'version':2,'sha256':'fictional','extraction':{'source':'user_transcription','confirmed':True,'kind':kind,'fields':{'name':name,'address':address,'date':'2026-10-01',**fields}}}

    def test_two_matters_have_independent_complete_and_missing_results(self):
        residence=[self.item('identity'),self.item('residence'),self.item('application')]
        self.assertEqual(self.check(residence)['result'],'未发现清单问题')
        self.assertEqual(self.check(residence[:-1])['result'],'疑似缺项')
        business=[self.item('identity'),self.item('application'),self.item('business_address')]
        self.assertEqual(self.check(business,'sole-trader-registration','standard-demo')['result'],'未发现清单问题')
        self.assertEqual(self.check(residence,'sole-trader-registration','standard-demo')['result'],'疑似缺项')

    def test_conflicts_and_unconfirmed_ocr_require_verification(self):
        items=[self.item('identity'),self.item('residence',name='教学乙'),self.item('application')]
        result=self.check(items)
        self.assertEqual(result['result'],'信息待核实')
        self.assertTrue(any(e['code']=='FIELD_MISMATCH' for e in result['evidence']))
        items[1]['extraction']['confirmed']=False
        self.assertEqual(self.check(items)['result'],'信息待核实')

    def test_unreadable_present_file_is_not_claimed_missing(self):
        result=self.check([{'id':'one','version':1,'extraction':{'error_code':'OCR_UNAVAILABLE'}}])
        self.assertEqual(result['result'],'无法判断')

    def test_rule_change_and_historical_date_exception_are_explicit(self):
        items=[self.item('identity',expiry_date='2026-01-01',historical_valid_on='2025-01-01'),self.item('residence'),self.item('application')]
        result=self.check(items)
        self.assertEqual(result['result'],'需人工核验')
        self.assertIn('expired_historical_valid',result['exceptions'])
        with self.assertRaises(AppError) as e:
            self.check(items,version='unknown')
        self.assertEqual(e.exception.code,'RULE_SNAPSHOT_CHANGED')

class ExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_returns_honest_failure_for_malformed_image(self):
        from zhengwu_precheck.utils.material_extract import extract_material
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bad.png'; p.write_bytes(b'\x89PNG\r\n\x1a\nwrong')
            result=await extract_material(p,'bad.png',trusted_teaching=True)
            self.assertEqual(result['error_code'],'MATERIAL_EXTRACTION_FAILED')
            self.assertNotIn(str(p),str(result))

    async def test_worker_does_not_accept_untrusted_processing_by_default(self):
        from zhengwu_precheck.utils.material_extract import extract_material
        result=await extract_material(Path('not-read'),'x.png')
        self.assertEqual(result['error_code'],'MATERIAL_ISOLATION_UNAVAILABLE')

    async def test_worker_timeout_reaps_process(self):
        from zhengwu_precheck.utils.material_extract import extract_material
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.png'; p.write_bytes(b'\x89PNG\r\n\x1a\nwrong')
            result=await extract_material(p,'x.png',trusted_teaching=True,timeout=0.001)
            self.assertEqual(result['error_code'],'MATERIAL_PROCESSING_TIMEOUT')

class RealOCRTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_local_chinese_image_recognition(self):
        from zhengwu_precheck.utils.material_extract import extract_material
        result=await extract_material(Path('data/materials/fictional-identity.png'),'fictional-identity.png',trusted_teaching=True)
        self.assertEqual(result.get('source'),'local_rapidocr',result)
        self.assertIn('教学甲',result['text'])
        self.assertIn('2026-10-01',result['text'])
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['fields'],{})

class MaterialIntegrationTests(MaterialFixture, unittest.IsolatedAsyncioTestCase):
    async def test_precheck_record_keeps_evidence_and_rechecks_set_authorization(self):
        item=await self.upload()
        await self.repo.correct_fields('u',item['id'],1,'identity',{'name':'教学甲','address':'虚构路1号','date':'2026-10-01'})
        mset=await self.repo.create_set('u','case',[item['id']])
        record=await self.repo.precheck('u','case','residence-permit','rental-demo','teaching-2026-10-v1',mset['id'],1)
        self.assertEqual(record['result'],'疑似缺项')
        self.assertTrue(record['evidence'])
        fetched=await self.repo.get_precheck('u',record['id'])
        self.assertEqual(fetched['material_set_id'],mset['id'])
        with self.assertRaises(AppError):
            await self.repo.get_precheck('v',record['id'])
        await self.repo.revoke('u',item['id'])
        with self.assertRaises(AppError):
            await self.repo.get_precheck('u',record['id'])

    async def test_concurrent_correction_version_cannot_overwrite_newer_fields(self):
        item=await self.upload()
        await self.repo.correct_fields('u',item['id'],1,'identity',{'name':'教学甲'})
        with self.assertRaises(AppError) as e:
            await self.repo.correct_fields('u',item['id'],1,'identity',{'name':'旧值'})
        self.assertEqual(e.exception.code,'MATERIAL_CHANGED')
        self.assertEqual((await self.repo.get('u',item['id']))['extraction']['fields']['name'],'教学甲')

    async def test_upload_count_limit_applies_to_live_authorized_materials(self):
        first=None
        for _ in range(10):
            first=await self.upload()
        with self.assertRaises(AppError) as e:
            await self.upload()
        self.assertEqual(e.exception.code,'MATERIAL_LIMIT')
        await self.repo.revoke('u',first['id'])
        await self.upload()

class SelectionSnapshotTests(MaterialFixture, unittest.IsolatedAsyncioTestCase):
    async def test_opaque_selection_snapshot_is_recorded_separately_from_teaching_rule_version(self):
        material_set=await self.repo.create_set('u','case',[])
        result=await self.repo.precheck('u','case','residence-permit','rental-demo','opaque-server-selection-hash',material_set['id'],4)
        self.assertEqual(result['selection_snapshot_id'],'opaque-server-selection-hash')
        self.assertEqual(result['teaching_rule_version'],'teaching-2026-10-v1')
        self.assertEqual(result['case_version'],4)
        self.assertEqual(len(result['teaching_rule_digest']),64)

class ScannedPDFTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_local_scanned_pdf_ocr(self):
        from zhengwu_precheck.utils.material_extract import extract_material
        result=await extract_material(Path('data/materials/fictional-scanned-identity.pdf'),'fictional-scanned-identity.pdf',trusted_teaching=True)
        self.assertEqual(result.get('source'),'local_rapidocr_pdf',result)
        self.assertIn('教学甲',result['text'])
        self.assertIn('2026-10-01',result['text'])
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['pages'][0]['page'],1)

class RuleFreshnessTests(MaterialFixture, unittest.IsolatedAsyncioTestCase):
    async def test_saved_precheck_rejects_changed_teaching_rules(self):
        from unittest.mock import patch
        mset=await self.repo.create_set('u','case',[])
        record=await self.repo.precheck('u','case','residence-permit','rental-demo','selection',mset['id'],1)
        replacement=Path(self.tmp.name)/'changed-rules.json'; replacement.write_text('{}')
        with patch('zhengwu_precheck.core.services.materials.RULES_PATH',replacement):
            with self.assertRaises(AppError) as e:
                await self.repo.get_precheck('u',record['id'])
        self.assertEqual(e.exception.code,'RULE_SNAPSHOT_CHANGED')


class WorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_lifecycle(self,cancel):
        import asyncio,os,sys
        from unittest.mock import patch
        from zhengwu_precheck.utils import material_extract
        spawn=asyncio.create_subprocess_exec
        state={}; ready=asyncio.Event()
        async def tracked_spawn(executable,*args,**kwargs):
            code="import os,sys,threading; print(os.getpid(),flush=True); threading.Event().wait()"
            proc=await spawn(executable,'-I','-c',code,**kwargs)
            state['proc']=proc
            state['actual_pid']=int(await asyncio.wait_for(proc.stdout.readline(),5))
            ready.set()
            return proc
        with patch.object(material_extract.asyncio,'create_subprocess_exec',tracked_spawn):
            task=asyncio.create_task(material_extract.extract_material(Path('unused'),'x.png',trusted_teaching=True,timeout=0.05 if not cancel else 40))
            try:
                await asyncio.wait_for(ready.wait(),5)
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    result=await task
                    self.assertEqual(result['error_code'],'MATERIAL_PROCESSING_TIMEOUT')
                self.assertIsNotNone(state['proc'].returncode)
                self.assertEqual(state['actual_pid'],state['proc'].pid,'Timeout must target actual parser, not forwarding launcher')
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                if state.get('actual_pid')!=getattr(state.get('proc'),'pid',None):
                    from contextlib import suppress
                    with suppress(OSError):
                        os.kill(state['actual_pid'],9)

    async def test_timeout_reaps_actual_interpreter(self):
        await self.exercise_lifecycle(False)

    async def test_cancellation_reaps_actual_interpreter(self):
        await self.exercise_lifecycle(True)

    async def test_invalid_worker_output_returns_safe_failure(self):
        import asyncio
        from unittest.mock import patch
        from zhengwu_precheck.utils import material_extract
        spawn=asyncio.create_subprocess_exec
        for raw in ['not-json','[]','null','['*2000+']'*2000,'{"source":"local_rapidocr","fields":[],"confirmed":false}','{"source":"local_rapidocr","fields":{},"confirmed":true,"text":"private"}']:
            async def output_spawn(executable,*args,**kwargs):
                return await spawn(executable,'-I','-c','import sys; sys.stdout.write('+repr(raw)+')',**kwargs)
            with patch.object(material_extract.asyncio,'create_subprocess_exec',output_spawn):
                result=await material_extract.extract_material(Path('unused'),'x.png',trusted_teaching=True)
            self.assertEqual(result['error_code'],'MATERIAL_EXTRACTION_FAILED')


    async def test_oversize_worker_output_is_bounded_and_reaped(self):
        import asyncio
        from unittest.mock import patch
        from zhengwu_precheck.utils import material_extract
        spawn=asyncio.create_subprocess_exec; processes=[]
        async def output_spawn(executable,*args,**kwargs):
            proc=await spawn(executable,'-I','-c',"import sys; sys.stdout.write('x'*500000); sys.stdout.flush()",**kwargs)
            processes.append(proc)
            return proc
        with patch.object(material_extract.asyncio,'create_subprocess_exec',output_spawn):
            result=await material_extract.extract_material(Path('unused'),'x.png',trusted_teaching=True)
        self.assertEqual(result['error_code'],'MATERIAL_EXTRACTION_FAILED')
        self.assertIsNotNone(processes[0].returncode)

