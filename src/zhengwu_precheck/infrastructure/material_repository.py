"""Private, per-user teaching materials. Never used by the public knowledge index."""
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4
from sqlalchemy import text
from zhengwu_precheck.core.errors import AppError


class MaterialRepository:
    def __init__(self, engine, runtime_dir, clock=time.time, processor=None):
        self.engine, self.clock = engine, clock
        self.runtime_dir = Path(runtime_dir).resolve()
        self.processor = processor

    async def initialize(self):
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        async with self.engine.begin() as c:
            for sql in [
                '''CREATE TABLE IF NOT EXISTS private_materials (id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),case_id TEXT NOT NULL REFERENCES conversations(id),filename TEXT NOT NULL,sha256 TEXT NOT NULL,size INTEGER NOT NULL,version INTEGER NOT NULL,consent INTEGER NOT NULL,processing_status TEXT NOT NULL,extraction TEXT NOT NULL,created_at REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS material_sets (id TEXT PRIMARY KEY,user_id TEXT NOT NULL,case_id TEXT NOT NULL,snapshot TEXT NOT NULL,created_at REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS precheck_records (id TEXT PRIMARY KEY,user_id TEXT NOT NULL,case_id TEXT NOT NULL,material_set_id TEXT NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL)''']:
                await c.execute(text(sql))

    async def _case(self, c, user_id, case_id):
        if not user_id:
            raise AppError(401, 'LOGIN_REQUIRED', '请先登录')
        row = (await c.execute(text('SELECT id FROM conversations WHERE id=:id AND user_id=:user'), {'id':case_id,'user':user_id})).first()
        if not row:
            raise AppError(404, 'CASE_NOT_FOUND', '案例不存在或无权访问')

    def _path(self, mid):
        # All identifiers come from persisted UUIDs, never filenames or caller paths.
        if str(uuid4().__class__(mid)) != mid:
            raise AppError(404, 'MATERIAL_NOT_FOUND', '材料不存在')
        return self.runtime_dir / (mid + '.bin')

    async def store(self, user_id, case_id, filename, content: bytes, consent: bool):
        async with self.engine.begin() as c:
            await self._case(c, user_id, case_id)
            await c.execute(text('UPDATE conversations SET created_at=created_at WHERE id=:id'), {'id':case_id})
            if consent is not True:
                raise AppError(422, 'MATERIAL_CONSENT_REQUIRED', '请逐份确认材料处理授权')
            if not isinstance(filename, str) or len(filename)>120 or any(x in filename for x in ('/','\\',':')):
                raise AppError(422, 'INVALID_MATERIAL', '材料名称不正确')
            suffix = Path(filename).suffix.lower()
            magic = {'.pdf': b'%PDF-', '.png': b'\x89PNG\r\n\x1a\n', '.jpg': b'\xff\xd8\xff', '.jpeg': b'\xff\xd8\xff'}
            if not isinstance(content, bytes) or not 1<=len(content)<=10*1024*1024 or suffix not in magic or not content.startswith(magic[suffix]):
                raise AppError(422, 'INVALID_MATERIAL', '仅支持10MB以内PNG、JPEG或PDF，文件头必须匹配')
            count = (await c.execute(text('SELECT COUNT(*) FROM private_materials WHERE case_id=:id AND consent=1'), {'id':case_id})).scalar()
            if count>=10:
                raise AppError(422, 'MATERIAL_LIMIT', '每个案例最多10份有效材料')
            item = dict(id=str(uuid4()),user_id=user_id,case_id=case_id,filename=filename,sha256=hashlib.sha256(content).hexdigest(),size=len(content),version=1,consent=1,processing_status='pending',extraction='{}',created_at=self.clock())
            path = self._path(item['id'])
            try:
                with path.open('xb') as f:
                    f.write(content)
                await c.execute(text('INSERT INTO private_materials VALUES (:id,:user_id,:case_id,:filename,:sha256,:size,:version,:consent,:processing_status,:extraction,:created_at)'), item)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        return await self.get(user_id, item['id'])

    async def get(self, user_id, material_id):
        if not user_id:
            raise AppError(401,'LOGIN_REQUIRED','请先登录')
        async with self.engine.connect() as c:
            row = (await c.execute(text('SELECT * FROM private_materials WHERE id=:id AND user_id=:user AND consent=1'), {'id':material_id,'user':user_id})).mappings().first()
        if not row:
            raise AppError(404,'MATERIAL_NOT_FOUND','材料不存在或授权已撤回')
        item = dict(row)
        item['extraction'] = json.loads(item['extraction'])
        return item

    async def list(self, user_id, case_id):
        async with self.engine.connect() as c:
            await self._case(c,user_id,case_id)
            ids = (await c.execute(text('SELECT id FROM private_materials WHERE case_id=:id AND user_id=:user AND consent=1 ORDER BY created_at'), {'id':case_id,'user':user_id})).scalars().all()
        return [await self.get(user_id, mid) for mid in ids]

    async def read_bytes(self, user_id, material_id):
        item = await self.get(user_id, material_id)
        content = self._path(item['id']).read_bytes()
        if hashlib.sha256(content).hexdigest()!=item['sha256']:
            raise AppError(409,'MATERIAL_CORRUPT','材料校验失败，请重新上传')
        return content

    async def _update(self,user_id,mid,version,status,extraction):
        async with self.engine.begin() as c:
            changed = await c.execute(text('UPDATE private_materials SET version=version+1,processing_status=:status,extraction=:data WHERE id=:id AND user_id=:user AND consent=1 AND version=:version'), {'id':mid,'user':user_id,'version':version,'status':status,'data':json.dumps(extraction,ensure_ascii=False)})
            if changed.rowcount!=1:
                raise AppError(409,'MATERIAL_CHANGED','材料已变化或授权已撤回，请刷新')
        return await self.get(user_id,mid)

    async def correct_fields(self,user_id,material_id,expected_version,kind,fields):
        await self.get(user_id,material_id)
        allowed={'name','address','date','expiry_date','historical_valid_on'}
        if kind not in {'identity','residence','application','business_address'} or not isinstance(fields,dict) or not set(fields)<=allowed or any(not isinstance(v,str) or len(v)>300 for v in fields.values()):
            raise AppError(422,'INVALID_FIELDS','请填写支持的材料类别和字段')
        return await self._update(user_id,material_id,expected_version,'confirmed',{'source':'user_transcription','kind':kind,'fields':fields,'confirmed':True,'note':'办事人主动转录或更正，未经真实性核验'})

    async def revoke(self,user_id,material_id):
        item = await self.get(user_id,material_id)
        async with self.engine.begin() as c:
            await c.execute(text("UPDATE private_materials SET consent=0,version=version+1,extraction='{}',processing_status='revoked' WHERE id=:id AND user_id=:user"),{'id':material_id,'user':user_id})
        self._path(item['id']).unlink(missing_ok=True)
        return {'id':material_id,'status':'revoked'}

    async def create_set(self,user_id,case_id,material_ids):
        async with self.engine.connect() as c:
            await self._case(c,user_id,case_id)
        if not isinstance(material_ids,list) or len(material_ids)>10 or len(set(material_ids))!=len(material_ids):
            raise AppError(422,'INVALID_MATERIAL_SET','材料集合不正确')
        snapshot=[]
        for mid in material_ids:
            item=await self.get(user_id,mid)
            if item['case_id']!=case_id:
                raise AppError(404,'MATERIAL_NOT_FOUND','材料不属于此案例')
            snapshot.append(item)
        record={'id':str(uuid4()),'user_id':user_id,'case_id':case_id,'snapshot':json.dumps(snapshot,ensure_ascii=False),'created_at':self.clock()}
        async with self.engine.begin() as c:
            await c.execute(text('INSERT INTO material_sets VALUES (:id,:user_id,:case_id,:snapshot,:created_at)'),record)
        record['snapshot']=snapshot
        return record

    async def get_set(self,user_id,material_set_id):
        async with self.engine.connect() as c:
            row=(await c.execute(text('SELECT * FROM material_sets WHERE id=:id AND user_id=:user'),{'id':material_set_id,'user':user_id})).mappings().first()
        if not row:
            raise AppError(404,'MATERIAL_SET_NOT_FOUND','材料集合不存在')
        result=dict(row); result['snapshot']=json.loads(result['snapshot'])
        for old in result['snapshot']:
            try:
                current=await self.get(user_id,old['id'])
            except AppError:
                raise AppError(409,'MATERIAL_SET_STALE','材料授权已变化，请重新预检') from None
            if current['version']!=old['version']:
                raise AppError(409,'MATERIAL_SET_STALE','材料内容已变化，请重新预检')
        return result

    async def process(self,user_id,material_id):
        item=await self.get(user_id,material_id)
        if self.processor is None:
            result={'source':'none','fields':{},'confirmed':False,'error_code':'MATERIAL_ISOLATION_UNAVAILABLE'}
            return await self._update(user_id,material_id,item['version'],'unavailable',result)
        from zhengwu_precheck.utils.material_extract import extract_material
        result=await extract_material(self._path(item['id']),item['filename'],**self.processor)
        return await self._update(user_id,material_id,item['version'],'extracted' if not result.get('error_code') else 'unavailable',result)

    async def precheck(self,user_id,case_id,matter_id,scenario_id,rule_snapshot_id,material_set_id,expected_case_version):
        from zhengwu_precheck.core.services.materials import evaluate, load_rules, RULES_PATH
        material_set=await self.get_set(user_id,material_set_id)
        if material_set['case_id']!=case_id:
            raise AppError(404,'MATERIAL_SET_NOT_FOUND','材料集合不属于此案例')
        rules=load_rules()
        result=evaluate(matter_id,scenario_id,rules['version'],material_set['snapshot'])
        result.update(selection_snapshot_id=rule_snapshot_id,teaching_rule_version=rules['version'],teaching_rule_digest=hashlib.sha256(RULES_PATH.read_bytes()).hexdigest())
        result.update(id=str(uuid4()),case_id=case_id,case_version=expected_case_version,material_set_id=material_set_id)
        async with self.engine.begin() as c:
            await c.execute(text('INSERT INTO precheck_records VALUES (:id,:user,:case,:set,:payload,:now)'),{'id':result['id'],'user':user_id,'case':case_id,'set':material_set_id,'payload':json.dumps(result,ensure_ascii=False),'now':self.clock()})
        return result

    async def get_precheck(self,user_id,precheck_id):
        async with self.engine.connect() as c:
            row=(await c.execute(text('SELECT payload FROM precheck_records WHERE id=:id AND user_id=:user'),{'id':precheck_id,'user':user_id})).first()
        if not row:
            raise AppError(404,'PRECHECK_NOT_FOUND','预检记录不存在或无权访问')
        result=json.loads(row[0])
        from zhengwu_precheck.core.services.materials import RULES_PATH
        if result.get('teaching_rule_digest')!=hashlib.sha256(RULES_PATH.read_bytes()).hexdigest():
            raise AppError(409,'RULE_SNAPSHOT_CHANGED','示例规则已变化，请重新确认和预检')
        await self.get_set(user_id,result['material_set_id'])
        return result


