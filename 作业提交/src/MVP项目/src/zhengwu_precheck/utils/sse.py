"""SSE帧：单行JSON可安全携带中文、换行和用户文本。"""
import json


def encode_event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
