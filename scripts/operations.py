"""构建、发布预检、备份恢复入口；不会创建凭证或远程部署。"""
import argparse
import asyncio
import json
from importlib.metadata import distributions
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from zhengwu_precheck.infrastructure.backup import backup, restore
from zhengwu_precheck.core.services.operations import release_preflight


def dependency_snapshot():
    """无pip环境也可校验已安装包的声明依赖并生成精确版本记录。"""
    from packaging.requirements import Requirement
    installed = list(distributions())
    normalize = lambda name: name.lower().replace('_','-').replace('.','-')
    versions = {normalize(dist.metadata['Name']):dist.version for dist in installed}
    failures = []
    for dist in installed:
        for specification in dist.requires or []:
            dependency = Requirement(specification)
            if dependency.marker is not None and not dependency.marker.evaluate({'extra':''}):
                continue
            version = versions.get(normalize(dependency.name))
            if version is None or (dependency.specifier and not dependency.specifier.contains(version)):
                failures.append(dist.metadata['Name'] + ' requires ' + dependency.name)
    if failures:
        raise ValueError('依赖不一致：' + ', '.join(failures))
    return sorted(dist.metadata['Name']+'=='+dist.version for dist in installed)


async def recovery_demo(output):
    """用实际应用仓储演练隔离恢复；不调用模型或使用真实业务资料。"""
    import httpx
    from sqlalchemy import text
    from zhengwu_precheck.main import create_app
    from zhengwu_precheck.settings import Settings

    class OfflineFixture:
        def config(self, prefix):
            from zhengwu_precheck.core.guide_rag import ApiConfig
            return ApiConfig('openai-compatible', 'fictional-offline-embedding', 'http://127.0.0.1:1/v1', 'NOT_A_REAL_KEY')

        async def embed(self, values):
            import numpy as np
            return np.array([[1.0, float(i+1)] for i in range(len(values))], dtype='float32')

        async def close(self):
            pass

    def require(condition, message):
        if not condition:
            raise ValueError('恢复演练核对失败：' + message)

    output = Path(output).resolve()
    if output.is_relative_to(ROOT) and not output.is_relative_to(ROOT/'.venv'):
        raise ValueError('演练产物须放仓库外或.venv忽略目录')
    work = output/('fictional-recovery-'+str(uuid4()))
    live = work/'live'
    app = create_app(Settings(runtime_dir=live, trusted_teaching_materials=False), qa=OfflineFixture())
    image = (ROOT/'data/materials/fictional-identity.png').read_bytes()
    password = 'Fictional-demo-'+str(uuid4())
    async with app.router.lifespan_context(app):
        state = app.state
        await state.repo.create_user('fictional-recovery-user', password)
        token, _ = await state.repo.login('fictional-recovery-user', password)
        user = await state.repo.authenticate(token)
        uid = user['id']
        await state.repo.create_user('fictional-recovery-window', password)
        await state.staff_repo.grant_role('fictional-recovery-window', 'window', 'residence-permit', actor='fictional-demo')
        staff = await state.staff_repo.user('fictional-recovery-window')
        identities = {}
        for role in ('owner', 'supervisor'):
            username = 'fictional-recovery-'+role
            await state.repo.create_user(username, password)
            await state.staff_repo.grant_role(username, role, 'residence-permit', actor='fictional-demo')
            account = await state.staff_repo.user(username)
            identities[role] = {'user_id':account['id'], 'grants':await state.staff_repo.grants(account['id'])}
        owner, supervisor = identities['owner'], identities['supervisor']

        async def wait_ready(version_id):
            state.knowledge_service.wakeup.set()
            deadline = time.monotonic()+30
            while True:
                version = await state.knowledge_repo.get_version(owner, version_id)
                if version['processing_status'] == 'ready':
                    return version
                require(version['processing_status'] != 'failed' and time.monotonic() < deadline, '教学索引处理')
                await asyncio.sleep(.05)

        async def publish(version_id, title):
            release = await state.knowledge_repo.create_release(owner, title=title, version_ids=[version_id], effective_from=time.time()-1)
            await state.knowledge_repo.release_action(owner, release['id'], 'submit', '虚构演练提交')
            await state.knowledge_repo.release_action(supervisor, release['id'], 'approve', '独立教学复核')
            await state.knowledge_repo.release_action(supervisor, release['id'], 'publish', '仅本地虚构演练')
            return release

        guide_text = '虚构恢复演练教学规则：申请表需要填写姓名。不代表实际政策。'
        source = await state.knowledge_repo.upload(owner, title='虚构恢复教学指南', matter_id='residence-permit',
            scenario_id='rental-demo', kind='guide', filename='fictional-recovery.md', source_uri='本地虚构演练',
            rule_version='fictional-recovery-v1', raw=guide_text.encode('utf-8'), is_teaching=True)
        await wait_ready(source['version_id'])
        first_release = await publish(source['version_id'], '虚构原规则')
        async with state.repo.engine.connect() as connection:
            original_snapshot = await state.knowledge_repo.capture(connection, 'residence-permit', 'rental-demo')
        await state.knowledge_repo.release_action(supervisor, first_release['id'], 'retire', '演练停用')
        async with state.repo.engine.connect() as connection:
            disabled = await state.knowledge_repo.capture(connection, 'residence-permit', 'rental-demo')
        require(not disabled['version_ids'], '停用规则不可用于新请求')
        restored_rule = await state.knowledge_repo.new_version(owner, source['version_id'], content=guide_text,
            rule_version='fictional-recovery-restored', expected_latest=1)
        await wait_ready(restored_rule['version_id'])
        await publish(restored_rule['version_id'], '虚构规则受控恢复')
        prompt_before = await state.prompt_repo.active('residence-permit')
        await state.prompt_repo.save(supervisor, 'residence-permit', '虚构恢复演练提示词', prompt_before['version'], '演练变更')
        prompt_restored = await state.prompt_repo.restore(supervisor, 'residence-permit', prompt_before['version'],
            prompt_before['version']+1, '恢复追加新版本，不覆盖历史')
        require(prompt_restored['content'] == prompt_before['content'], '提示词恢复内容')
        case = await state.agent_service.create_case({'user_id':uid})
        material = await state.material_repo.store(uid, case['case_id'], '虚构教学材料.png', image, True)
        draft = await state.agent_repo.create_draft(uid, dict(title='虚构恢复咨询', content='仅用于本地恢复演练',
            matter_id='residence-permit', scenario_id='rental-demo', case_id=case['case_id'],
            shared_material_ids=[material['id']], shared_material_versions=[
                {key:material[key] for key in ('id','version','sha256','filename')}]))
        confirmation = await state.agent_repo.confirm_draft(uid, draft['id'], draft['version'])
        operation_key = 'fictional-recovery-operation'
        ticket = await state.ticket_repo.create_confirmed(uid, draft['id'], draft['version'], confirmation['id'], operation_key)
        async with state.repo.engine.connect() as connection:
            pending = (await connection.execute(text("SELECT count(*) FROM ticket_jobs WHERE status='pending'"))).scalar_one()
        require(pending == 2, '原工单持久任务')
    # 生命周期退出关闭连接和后台写入，再执行跨数据库/文件一致快照。
    started = time.perf_counter()
    manifest = backup(live/'chat.sqlite3', live, work/'snapshot', quiesced=True, config_version='fictional-app-v1')
    backup_seconds = time.perf_counter()-started
    started = time.perf_counter()
    restore(work/'snapshot', work/'restored', expected_config_version='fictional-app-v1')
    restored = create_app(Settings(runtime_dir=work/'restored', trusted_teaching_materials=False), qa=OfflineFixture())
    async with restored.router.lifespan_context(restored):
        state = restored.state
        require((await state.repo.authenticate(token))['id'] == uid, '用户会话')
        require(await state.material_repo.read_bytes(uid, material['id']) == image, '材料原件')
        detail = await state.ticket_repo.staff_detail({'user_id':staff['id']}, ticket['id'])
        require(detail['ticket']['id'] == ticket['id'], '工作人员授权与工单')
        require(len(detail['shared_context']['materials']) == 1, '共享材料关联')
        async with state.repo.engine.connect() as connection:
            snapshot = await state.knowledge_repo.capture(connection, 'residence-permit', 'rental-demo')
        require(snapshot['version_ids'] == [restored_rule['version_id']], '受控恢复规则')
        historical = await state.knowledge_repo.chunks_for_snapshot(original_snapshot)
        current = await state.knowledge_repo.chunks_for_snapshot(snapshot)
        require(historical[0]['chunks'][0]['text_content'] == guide_text, '旧快照保留')
        import faiss
        for version in [*historical, *current]:
            index = faiss.read_index(str(work/'restored'/version['index_key']))
            require(index.ntotal == len(version['chunks']), '索引与片段一致')
        active_prompt = await state.prompt_repo.active('residence-permit')
        history = await state.prompt_repo.history(supervisor, 'residence-permit')
        require(active_prompt['content'] == prompt_before['content'] and active_prompt['restored_from'] == prompt_before['version']
                and len(history) == 3 and history[1]['content'] == '虚构恢复演练提示词', '提示词版本与历史')
        repeated = await state.ticket_repo.create_confirmed(uid, draft['id'], draft['version'], confirmation['id'], operation_key)
        require(repeated['id'] == ticket['id'], '原操作键重试')
        require((await state.ticket_repo.citizen_list(uid))['total'] == 1, '单一工单')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restored), base_url='http://127.0.0.1') as client:
            response = await client.get('/health')
            require(response.status_code == 200 and response.json()['status'] == 'ok', '恢复后健康检查')
        # 仅推进这个全新虚构运行库的业务时钟；不修改真实实例时间。
        async with state.repo.engine.connect() as connection:
            due = (await connection.execute(text('SELECT max(due_at) FROM ticket_jobs'))).scalar_one()
        state.ticket_repo.clock = lambda: due+1
        await state.ticket_repo.run_due()
        async with state.repo.engine.connect() as connection:
            transitions = (await connection.execute(text('SELECT count(*) FROM ticket_transitions'))).scalar_one()
        require(await state.ticket_repo.run_due() == 0, '任务重复执行')
        async with state.repo.engine.connect() as connection:
            after = (await connection.execute(text('SELECT count(*) FROM ticket_transitions'))).scalar_one()
            done = (await connection.execute(text("SELECT count(*) FROM ticket_jobs WHERE status='done'"))).scalar_one()
        require(after == transitions and done == pending, '任务留痕去重')
    result = dict(dataset='fictional_real_application_schema', artifact_directory=str(work),
        backup_seconds=backup_seconds, restore_verify_seconds=time.perf_counter()-started,
        snapshot_at=manifest['created_at'], records=1, tasks=pending, tickets=1, referenced_files=len(manifest['files'])-1,
        application_restart_verified=True, original_submission_replayed=True,
        tasks_not_duplicated=True, knowledge_index_restored=True, rule_restore_verified=True,
        prompt_restore_verified=True, embedding_provider='fictional_offline_fixture', production_rpo_rto_verified=False)
    (work/'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('build')
    commands.add_parser('preflight')
    commands.add_parser('serve')
    demo = commands.add_parser('recovery-demo')
    demo.add_argument('--output', type=Path, required=True)
    save = commands.add_parser('backup')
    save.add_argument('--runtime', type=Path, required=True)
    save.add_argument('--database', type=Path, required=True)
    save.add_argument('--output', type=Path, required=True)
    save.add_argument('--quiesced', action='store_true')
    load = commands.add_parser('restore')
    load.add_argument('--snapshot', type=Path, required=True)
    load.add_argument('--output', type=Path, required=True)
    load.add_argument('--config-version', required=True)
    args = parser.parse_args()
    if args.command == 'build':
        subprocess.run([sys.executable, str(ROOT/'scripts/check_structure.py')], check=True)
        subprocess.run([sys.executable, '-m', 'compileall', '-q', str(ROOT/'src')], check=True)
        locked = dependency_snapshot()
        target = ROOT/'.venv/build'
        target.mkdir(parents=True, exist_ok=True)
        (target/'requirements.lock.txt').write_text('\n'.join(locked)+'\n', encoding='utf-8')
        print('本地构建检查通过；已记录当前环境固定版本，发布前仍须执行全量测试。')
    elif args.command in ('preflight','serve'):
        result = release_preflight(os.environ, repository=ROOT)
        if args.command == 'serve':
            import uvicorn
            uvicorn.run('zhengwu_precheck.main:create_app', factory=True, host='127.0.0.1', port=int(os.environ.get('ZHENGWU_PORT','8000')),
                        ssl_certfile=os.environ['ZHENGWU_TLS_CERT'], ssl_keyfile=os.environ['ZHENGWU_TLS_KEY'])
        else:
            print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'backup':
        result = backup(args.database,args.runtime,args.output,quiesced=args.quiesced,
                        config_version=os.environ.get('ZHENGWU_CONFIG_VERSION','unrecorded'))
        print(json.dumps({'status':'snapshot_created','files':len(result['files'])}))
    elif args.command == 'restore':
        result = restore(args.snapshot,args.output,expected_config_version=args.config_version)
        print(json.dumps({'status':'isolated_restore_verified','files':len(result['files'])}))
    elif args.command == 'recovery-demo':
        print(json.dumps(asyncio.run(recovery_demo(args.output)), ensure_ascii=False))


if __name__=='__main__':
    try:
        main()
    except (ValueError, FileExistsError, PermissionError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
