"""Bounded local extraction adapter. Trusted teaching only until OS access isolation is approved."""
import asyncio
import json
import os
from pathlib import Path
import sys
import sysconfig
from contextlib import suppress


def unavailable(code):
    return {'source':'none','fields':{},'confirmed':False,'error_code':code}


def validate_output(output):
    result=json.loads(output)
    if not isinstance(result,dict) or result.get('source') not in {'pdf_text','local_rapidocr','local_rapidocr_pdf'} or result.get('confirmed') is not False or result.get('fields')!={}:
        raise ValueError('invalid worker response')
    if not isinstance(result.get('text'),str) or len(result['text'])>20000:
        raise ValueError('invalid text budget')
    confidence=result.get('confidence')
    if confidence is not None and (type(confidence) not in (int,float) or not 0<=confidence<=1):
        raise ValueError('invalid confidence')
    pages=result.get('pages',[])
    if not isinstance(pages,list) or len(pages)>10 or any(not isinstance(p,dict) or type(p.get('page')) is not int or not 1<=p['page']<=10 or not isinstance(p.get('text'),str) or len(p['text'])>20000 for p in pages):
        raise ValueError('invalid pages')
    for block in [result,*pages]:
        lines=block.get('lines',[])
        if not isinstance(lines,list) or len(lines)>200 or any(not isinstance(line,dict) or not isinstance(line.get('text'),str) or len(line['text'])>20000 or type(line.get('confidence')) not in (int,float) or not 0<=line['confidence']<=1 for line in lines):
            raise ValueError('invalid OCR lines')
    return result


async def extract_material(path, filename, *, trusted_teaching=False, timeout=40):
    if not trusted_teaching:
        return unavailable('MATERIAL_ISOLATION_UNAVAILABLE')
    if type(timeout) not in (int,float) or not 0<timeout<=60:
        return unavailable('INVALID_PROCESSING_LIMIT')
    path=Path(path).resolve()
    env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP'}}
    env.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',PYTHONIOENCODING='utf-8')
    # The Windows venv executable forwards to a child. Launch the actual interpreter
    # directly so killing/reaping this PID also works before Job Object setup.
    bootstrap="import runpy,sys; sys.path[:0]=sys.argv[1:3]; sys.argv=sys.argv[3:]; runpy.run_path(sys.argv[0],run_name='__main__')"
    proc=None
    try:
        proc=await asyncio.create_subprocess_exec(sys._base_executable,'-I','-c',bootstrap,
            sysconfig.get_paths()['purelib'],str(Path(__file__).resolve().parents[2]),str(Path(__file__).resolve()),str(path),filename,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env)
        async def read_output():
            chunks=[]; total=0
            while True:
                chunk=await proc.stdout.read(16384)
                if not chunk:
                    break
                total+=len(chunk)
                if total>200000:
                    raise ValueError('worker response budget')
                chunks.append(chunk)
            await proc.wait()
            return b''.join(chunks)
        output=await asyncio.wait_for(read_output(),timeout)
        if proc.returncode!=0:
            return unavailable('MATERIAL_EXTRACTION_FAILED')
        return validate_output(output)
    except asyncio.TimeoutError:
        return unavailable('MATERIAL_PROCESSING_TIMEOUT')
    except (ValueError,TypeError,OSError,UnicodeError):
        return unavailable('MATERIAL_EXTRACTION_FAILED')
    finally:
        if proc is not None:
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
            wait_task=asyncio.create_task(proc.wait())
            try:
                await asyncio.shield(wait_task)
            except asyncio.CancelledError:
                await asyncio.shield(wait_task)
                raise


_JOB=None

def apply_limits():
    global _JOB
    if sys.platform=='win32':
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',w.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(n,ctypes.c_uint64) for n in ('read','write','other','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]; k.CreateJobObjectW.restype=w.HANDLE
        k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        k.GetCurrentProcess.restype=w.HANDLE
        _JOB=k.CreateJobObjectW(None,None)
        limits=Extended(); limits.basic.flags=0x2000|0x100|0x8|0x2
        limits.basic.active=1; limits.basic.process_time=30*10000000; limits.process_memory=1536*1024*1024
        if not _JOB or not k.SetInformationJobObject(_JOB,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not k.AssignProcessToJobObject(_JOB,k.GetCurrentProcess()):
            raise RuntimeError('job object unavailable')
    else:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,(30,30))
        resource.setrlimit(resource.RLIMIT_AS,(1536*1024*1024,1536*1024*1024))


def worker(path,filename):
    apply_limits()
    if path.stat().st_size>10*1024*1024:
        raise ValueError('size')
    suffix=Path(filename).suffix.lower()
    if suffix=='.pdf':
        from pypdf import overwrite_configuration
        overwrite_configuration(maximum_declared_stream_length=2*1024*1024,zlib_maximum_output_length=2*1024*1024,array_based_stream_maximum_output_length=2*1024*1024,stream_decoding_work_maximum_length=8*1024*1024,stream_parsing_work_maximum_count=100000)
        from zhengwu_precheck.utils.knowledge_extract import extract_pages
        pages=extract_pages(path,filename)
        if len(pages)>10:
            raise ValueError('pages')
        text='\n'.join(p['text'] for p in pages)
        if any(not page['text'].strip() for page in pages):
            import pypdfium2 as pdfium
            from rapidocr_onnxruntime import RapidOCR
            import numpy as np
            engine=RapidOCR(intra_op_num_threads=1,inter_op_num_threads=1)
            confidences=[]
            document=pdfium.PdfDocument(str(path))
            try:
                for record in pages:
                    if record['text'].strip():
                        continue
                    page=document[record['page']-1]
                    try:
                        width,height=page.get_size()
                        if width<=0 or height<=0 or width*height*4>12000000 or max(width,height)*2>8000:
                            raise ValueError('render pixels')
                        bitmap=page.render(scale=2)
                        try:
                            image=bitmap.to_pil().convert('RGB')
                            results,_=engine(np.array(image))
                            lines=[{'text':r[1],'confidence':float(r[2]),'box':r[0]} for r in (results or [])][:200]
                            record.update(text='\n'.join(r['text'] for r in lines),lines=lines,source='local_rapidocr')
                            confidences.extend(r['confidence'] for r in lines)
                            image.close()
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
            finally:
                document.close()
            text='\n'.join(p['text'] for p in pages)
            if len(text)>20000:
                raise ValueError('text budget')
            return {'source':'local_rapidocr_pdf','text':text,'pages':pages,'fields':{},'confirmed':False,'confidence':min(confidences,default=0.0),'engine':'rapidocr-onnxruntime-1.4.4'}
        return {'source':'pdf_text','text':text[:20000],'pages':pages,'fields':{},'confirmed':False,'confidence':None}
    if suffix not in {'.png','.jpg','.jpeg'}:
        raise ValueError('type')
    from PIL import Image
    Image.MAX_IMAGE_PIXELS=12000000
    import warnings
    warnings.simplefilter('error',Image.DecompressionBombWarning)
    with Image.open(path) as im:
        if im.width*im.height>12000000 or im.width>8000 or im.height>8000 or getattr(im,'n_frames',1)!=1:
            raise ValueError('pixels')
        im.verify()
    from rapidocr_onnxruntime import RapidOCR
    engine=RapidOCR(intra_op_num_threads=1,inter_op_num_threads=1)
    results,_=engine(str(path))
    lines=[{'text':r[1],'confidence':float(r[2]),'box':r[0]} for r in (results or [])][:200]
    return {'source':'local_rapidocr','text':'\n'.join(r['text'] for r in lines)[:20000],'lines':lines,'fields':{},'confirmed':False,'confidence':min((r['confidence'] for r in lines),default=0.0),'engine':'rapidocr-onnxruntime-1.4.4'}


if __name__=='__main__':
    try:
        result=worker(Path(sys.argv[1]),sys.argv[2])
        sys.stdout.write(json.dumps(result,ensure_ascii=True))
    except BaseException:
        sys.exit(2)


