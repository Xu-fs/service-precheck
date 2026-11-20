from contextlib import closing
"""SQLite在线备份API与私有文件快照。附件一致性要求调用方先停止写入接纳。"""
import hashlib
from importlib.metadata import distributions
import json
from pathlib import Path
import shutil
import sqlite3
import time

FORMAT_VERSION = 1


def checksum(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _validate_root(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('不允许符号链接目录')
    return path.resolve()


def _validate_references(db, root, inventory=None):
    """核对项目数据库引用的原文、索引和仍授权的私有材料。"""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    references = []
    if 'knowledge_versions' in tables:
        columns = {row[1] for row in db.execute('PRAGMA table_info(knowledge_versions)')}
        for column in ('storage_key', 'index_key'):
            if column in columns:
                digest = 'checksum' if column == 'index_key' and 'checksum' in columns else 'NULL'
                references.extend(db.execute(f'SELECT {column},{digest} FROM knowledge_versions WHERE {column} IS NOT NULL'))
    if 'private_materials' in tables:
        columns = {row[1] for row in db.execute('PRAGMA table_info(private_materials)')}
        digest = 'sha256' if 'sha256' in columns else 'NULL'
        references.extend((f'private-materials/{mid}.bin', sha) for mid, sha in
                          db.execute(f'SELECT id,{digest} FROM private_materials WHERE consent=1'))
    for relative, expected in references:
        path = root / relative
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise ValueError('数据库文件引用不在快照目录内')
        if not path.is_file() or (inventory is not None and path.relative_to(root).as_posix() not in inventory):
            raise ValueError('数据库引用的文件缺失，不能生成或恢复完整快照')
        if expected and checksum(path) != expected:
            raise ValueError('数据库引用文件校验失败')


def backup(database, runtime, destination, *, quiesced=False, config_version='unrecorded'):
    if not quiesced:
        raise ValueError('先停止新写入、等待在途任务完成或核对，然后显式确认 quiesced')
    runtime, destination = _validate_root(runtime), _validate_root(destination)
    database = Path(database).resolve()
    if not database.is_file() or not database.is_relative_to(runtime):
        raise ValueError('数据库须位于指定私有运行目录')
    if destination.is_relative_to(runtime) or runtime.is_relative_to(destination):
        raise ValueError('备份与运行目录必须相互隔离')
    if destination.exists():
        raise FileExistsError('备份目标已存在')
    files = []
    for path in runtime.rglob('*'):
        if path.is_symlink():
            raise ValueError('运行目录含符号链接，须人工核对')
        if path.is_file() and (path.suffix.lower() in {'.pem','.key','.p12','.pfx'} or path.name.lower().startswith('.env')):
            raise ValueError('运行目录混入凭证文件，请由管理员移至独立凭证目录')
        if path.is_file() and path != database and str(path) not in {str(database)+'-wal', str(database)+'-shm', str(database)+'-journal'}:
            files.append(path)
    destination.mkdir(parents=True)
    db_relative = database.relative_to(runtime).as_posix()
    output_db = destination / db_relative
    output_db.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    # SQLite写锁贯穿数据库快照和附件复制；进程外附件写入仍须通过维护模式停止。
    with closing(sqlite3.connect(database)) as writer, closing(sqlite3.connect(database)) as source:
        writer.execute('PRAGMA busy_timeout=5000')
        writer.execute('BEGIN IMMEDIATE')
        try:
            with closing(sqlite3.connect(output_db)) as output:
                source.backup(output)
                if output.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('数据库完整性检查失败')
                if output.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('数据库关联不一致')
                _validate_references(output, runtime)
            for path in files:
                relative = path.relative_to(runtime)
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                before = checksum(path)
                shutil.copy2(path, target)
                if checksum(path) != before or checksum(target) != before:
                    raise ValueError('文件在快照期间变化，须重新维护备份')
            tables = [row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            versions = list(source.execute('SELECT version FROM schema_versions')) if 'schema_versions' in tables else []
        finally:
            writer.rollback()
    manifest = dict(format_version=FORMAT_VERSION, database=db_relative, created_at=started,
                    snapshot_seconds=time.time()-started, quiesced=True, config_version=config_version,
                    schema_versions=[row[0] for row in versions],
                    dependencies=sorted({dist.metadata['Name']: dist.version for dist in distributions()}.items()),
                    files={p.relative_to(destination).as_posix(): checksum(p) for p in destination.rglob('*') if p.is_file()})
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def restore(snapshot, destination, *, expected_config_version=None):
    snapshot, destination = _validate_root(snapshot), _validate_root(destination)
    if destination.exists():
        raise FileExistsError('拒绝覆盖已有目录；恢复须使用全新隔离目录')
    if destination.is_relative_to(snapshot) or snapshot.is_relative_to(destination):
        raise ValueError('恢复目录必须与备份隔离')
    manifest = json.loads((snapshot / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format_version') != FORMAT_VERSION:
        raise ValueError('备份版本不兼容')
    if expected_config_version is not None and manifest['config_version'] != expected_config_version:
        raise ValueError('配置版本不匹配，须先审核迁移')
    installed = {dist.metadata['Name'].lower(): dist.version for dist in distributions()}
    for name, version in manifest.get('dependencies', []):
        if name.lower() in {'sqlalchemy', 'aiosqlite'} and installed.get(name.lower()) != version:
            raise ValueError('数据库依赖版本不匹配，须先在隔离环境审核迁移')
    for relative, expected in manifest['files'].items():
        path = snapshot / relative
        if not path.resolve().is_relative_to(snapshot) or any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('清单含非法路径')
        if not path.is_file() or checksum(path) != expected:
            raise ValueError('备份校验失败')
    if manifest['database'] not in manifest['files']:
        raise ValueError('数据库未在校验清单内')
    with closing(sqlite3.connect((snapshot / manifest['database']).as_uri()+'?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('数据库校验失败')
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        versions = [row[0] for row in db.execute('SELECT version FROM schema_versions')] if 'schema_versions' in tables else []
        if versions != manifest['schema_versions']:
            raise ValueError('迁移版本不匹配')
        _validate_references(db, snapshot, manifest['files'])
    destination.mkdir(parents=True)
    for relative in manifest['files']:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot / relative, target)
    return manifest
