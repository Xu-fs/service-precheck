"""只保留完整历史轮次，不截断关键问题或规则依据。"""
def trim_history(history: list[dict], limit: int = 6000) -> list[dict]:
    pairs = [history[i:i + 2] for i in range(0, len(history) - 1, 2)]
    kept, length = [], 0
    for pair in reversed(pairs[-6:]):
        size = sum(len(item["content"]) for item in pair)
        if length + size > limit:
            break
        kept.insert(0, pair)
        length += size
    return [item for pair in kept for item in pair]
