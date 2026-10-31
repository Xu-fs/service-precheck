"""受限示例知识提取子进程入口；不联网、不做OCR，不是操作系统安全沙箱。"""
import json
import logging
from pathlib import Path
import re
import sys

from pypdf import PdfReader, overwrite_configuration


def extract_pages(path, filename):
    if filename.lower().endswith(".md"):
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("文件超过20MB")
        content = path.read_text(encoding="utf-8-sig")
        content = re.sub(r"\A---\r?\n.*?\r?\n---\r?\n", "", content, count=1, flags=re.S).strip()
        pages = [{"page": None, "text": content}]
    else:
        reader = PdfReader(str(path), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 20:
            raise ValueError("PDF加密或超过20页")
        pages, total = [], 0
        for number, page in enumerate(reader.pages, 1):
            resources = page.get("/Resources")
            if resources is not None:
                resources = resources.get_object()
                xobjects = resources.get("/XObject")
                if xobjects is not None:
                    # 首期只接受简单示例PDF；Form可能吞掉解析异常后返回部分正文。
                    # 不解析Form内容，也不缓存/递归展开嵌套Form，要求人工转换为简单PDF或补文本。
                    for value in xobjects.get_object().values():
                        if value.get_object().get("/Subtype") == "/Form":
                            raise ValueError("示例PDF包含不支持的Form对象，请转换或人工补充")
            stream = page.get_contents()
            # ContentStream可能尚未解析operations而为假值，不能使用if stream。
            if stream is not None and len(stream.get_data()) > 2 * 1024 * 1024:
                raise ValueError("PDF页面内容超过2MB")
            content = (page.extract_text() or "").strip()
            total += len(content)
            if total > 50000:
                raise ValueError("提取正文超过50000字符")
            pages.append({"page": number, "text": content})
    if sum(len(page["text"]) for page in pages) > 50000:
        raise ValueError("提取正文超过50000字符")
    return pages


if __name__ == "__main__":
    try:
        logging.disable(logging.CRITICAL)  # 畸形示例文件诊断不输出原文或内部路径。
        # 路径由父进程校验为私有运行目录中的上传文件，文件名只用来判断类型。
        path, filename = Path(sys.argv[1]), sys.argv[2]
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("文件过大")
        # 在解压/解析前收紧pypdf预算，不仅在分配后检查页面长度。
        overwrite_configuration(maximum_declared_stream_length=2 * 1024 * 1024,
            array_based_stream_maximum_output_length=2 * 1024 * 1024,
            zlib_maximum_output_length=2 * 1024 * 1024, lzw_maximum_output_length=2 * 1024 * 1024,
            run_length_maximum_output_length=2 * 1024 * 1024, jbig2_maximum_output_length=2 * 1024 * 1024,
            brotli_maximum_output_length=2 * 1024 * 1024, image_maximum_buffer_size=2 * 1024 * 1024,
            stream_decoding_work_maximum_length=8 * 1024 * 1024, stream_parsing_work_maximum_count=100000,
            zlib_maximum_recovery_input_length=1024 * 1024)
        # 支持平台的OS资源上限；Windows仍需客户部署增加Job Object/容器内存限制。
        if sys.platform != "win32":
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        sys.stdout.write(json.dumps(extract_pages(path, filename), ensure_ascii=True))
    except Exception:
        # 不将PDF内容、文件路径或异常堆栈打印到父进程日志。
        sys.exit(2)
