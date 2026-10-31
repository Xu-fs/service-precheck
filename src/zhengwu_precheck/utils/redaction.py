"""仅对诊断副本脱敏；不能替代结构化最小化，绝不回写业务原件。"""
import re

SECRET_KEYS = {'token', 'password', 'authorization', 'api_key', 'secret', 'cookie', 'password_hash'}


def redact(value):
    if isinstance(value, dict):
        return {key: '[已隐藏]' if any(word in str(key).lower() for word in SECRET_KEYS) else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    for pattern in (r'(?<!\d)\d{17}[\dXx](?!\d)', r'(?<!\d)1[3-9]\d{9}(?!\d)',
                    r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', r'(?<!\d)\d{16,19}(?!\d)',
                    r'(?i)(?:Bearer\s+)[A-Za-z0-9._~-]+'):
        value = re.sub(pattern, '[已隐藏]', value)
    value = re.sub(r'(姓名\s*[：:]\s*)[\u4e00-\u9fff]{2,8}', r'\1[已隐藏]', value)
    value = re.sub(r'((?:住址|地址)\s*[：:]\s*)[^\n，,；;]+', r'\1[已隐藏]', value)
    return value
