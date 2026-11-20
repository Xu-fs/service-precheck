"""生成Ubuntu课程部署包，白名单打包，绝不递归收集本地环境或运行库。"""
from pathlib import Path
import hashlib
import io
import json
import tarfile

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'作业提交/src/Ubuntu部署'
PREFIX='zhengwu-deploy-20261108'


def main():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    files=[]
    for name in ('src','scripts','data','config','docs','tests','examples','.github'):
        for path in (ROOT/name).rglob('*'):
            if not path.is_file() or path.is_symlink():
                continue
            if any(x in {'.venv','.git','__pycache__','.workbuddy-ai','.pytest_cache'} for x in path.parts):
                continue
            if path.suffix in {'.key','.pem','.sqlite','.sqlite3','.db','.pyc','.log'} or path.name=='.env' or path.name.startswith('local.'):
                continue
            files.append(path)
    for name in ('README.md','requirements.txt','LICENSE','AGENTS.md','.gitignore','CHANGELOG.md','CONTRIBUTING.md','启动网页.bat'):
        files.append(ROOT/name)
    archive=OUTPUT/'zhengwu-mvp-ubuntu.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for path in sorted(files):
            relative=path.relative_to(ROOT)
            info=tar.gettarinfo(str(path),PREFIX+'/'+relative.as_posix())
            info.uid=info.gid=0
            info.uname=info.gname='root'
            info.mode=0o755 if path.suffix=='.sh' else 0o644
            content=path.read_bytes()
            if path.suffix=='.sh':
                content=content.replace(b'\r\n',b'\n')
            info.size=len(content)
            tar.addfile(info,io.BytesIO(content))
    with tarfile.open(archive) as tar:
        for member in tar:
            assert member.name.startswith(PREFIX+'/')
            assert '..' not in Path(member.name).parts and member.isfile()
            assert '/.venv/' not in member.name and not member.name.endswith(('.key','.sqlite3'))
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    (OUTPUT/'SHA256SUMS.txt').write_text(digest+'  '+archive.name+'\n',encoding='utf-8')
    print(json.dumps({'file':str(archive),'bytes':archive.stat().st_size,'files':len(files),'sha256':digest},ensure_ascii=False))


if __name__=='__main__':
    main()
