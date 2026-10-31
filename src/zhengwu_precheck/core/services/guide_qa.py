"""异步模型适配及受控RAG；不从历史中获取个人业务资料。"""
import asyncio
import hashlib
import json

import faiss
import numpy as np
import os
from urllib.parse import urlsplit

import httpx

from zhengwu_precheck.core import guide_rag as rag
from zhengwu_precheck.core.errors import AppError
from zhengwu_precheck.utils.context import trim_history


class GuideQA:
    def __init__(self, settings):
        self.settings = settings
        self.index = None
        try:
            self.chunks = rag.load_chunks(settings.guide_path)
        except (OSError, ValueError):
            self.chunks = None
        self.lock = asyncio.Lock()
        # 默认直连已配置服务，不自动继承机器上的代理；获批准时可显式启用。
        self.client = httpx.AsyncClient(timeout=45, follow_redirects=False,
                                      trust_env=os.getenv("ZHENGWU_TRUST_PROXY_ENV", "false").lower() == "true")

    def config(self, prefix):
        return rag.ApiConfig(os.getenv(f"{prefix}_PROVIDER", "openai-compatible"), os.getenv(f"{prefix}_MODEL", ""),
                             os.getenv(f"{prefix}_BASE_URL", ""), f"{prefix}_API_KEY", 45)

    def ensure_ready(self, snapshot=None):
        if snapshot and (snapshot["conflict"] or (snapshot["managed"] and not snapshot["version_ids"])):
            return  # 正常业务兜底无需模型；停用后不回落本地指南。
        try:
            for prefix in ("LLM", "EMBEDDING"):
                config = self.config(prefix)
                if not config.model or not config.base_url:
                    raise ValueError("未配置模型")
                rag.check_base_url(config.base_url)
                if config.provider not in (("openai-compatible", "ollama") if prefix == "LLM" else ("openai-compatible",)):
                    raise ValueError("协议未支持")
                if urlsplit(config.base_url).hostname not in {"localhost", "127.0.0.1", "::1"} and not os.getenv(config.key_env):
                    raise ValueError("未配置密钥")
            if not (snapshot and snapshot["managed"]) and (not self.chunks or any(chunk.matter != "居住证申领" for chunk in self.chunks)):
                raise ValueError("未配置指南")
        except ValueError:
            raise AppError(503, "SERVICE_UNAVAILABLE", "问答服务尚未就绪，请联系维护人员配置指南与获批准的模型服务", True) from None

    async def request(self, config, endpoint, payload):
        try:
            rag.check_base_url(config.base_url)
            if not config.model or config.provider not in {"openai-compatible", "ollama"}:
                raise ValueError("模型配置无效")
            if urlsplit(config.base_url).hostname not in {"localhost", "127.0.0.1", "::1"} and not os.getenv(config.key_env):
                raise ValueError("模型密钥缺失")
        except ValueError:
            raise AppError(503, "SERVICE_UNAVAILABLE", "请维护人员配置获批准的模型服务，当前暂停处理", True) from None
        headers = {}
        if key := os.getenv(config.key_env):
            headers["Authorization"] = "Bearer " + key
        response = await self.client.post(config.base_url.rstrip("/") + endpoint, json=payload, headers=headers)
        if not 200 <= response.status_code < 300:
            raise AppError(502, "MODEL_SERVICE_ERROR", "模型服务暂时无法回答，请稍后重试", True)
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("响应不是对象")
            return data
        except ValueError:
            raise AppError(502, "MODEL_RESPONSE_INVALID", "模型服务返回格式异常，请稍后重试", True) from None

    async def embed(self, texts):
        config = self.config("EMBEDDING")
        if config.provider != "openai-compatible":
            raise AppError(503, "SERVICE_UNAVAILABLE", "当前嵌入协议未支持，请维护人员检查配置", True)
        data = await self.request(config, "/embeddings", {"model": config.model, "input": texts})
        try:
            return await asyncio.to_thread(rag.normalize_embeddings, data, len(texts))
        except ValueError:
            raise AppError(502, "MODEL_RESPONSE_INVALID", "嵌入服务返回格式异常，请维护人员检查", True) from None

    async def published_index(self, snapshot):
        versions = await self.knowledge_repo.chunks_for_snapshot(snapshot)
        signature = self.knowledge_service.signature()
        def load():
            chunks, matrices, dimensions = [], [], None
            from zhengwu_precheck.core.staff_permissions import MATTER_NAMES
            for version in versions:
                if version["embedding_signature"] != signature:
                    raise AppError(503, "KNOWLEDGE_INDEX_INCOMPATIBLE", "知识向量与当前模型配置不一致，请维护人员建立并审核新版本；当前转人工处理", True)
                try:
                    path = (self.settings.runtime_dir / version["index_key"]).resolve()
                    if not path.is_relative_to(self.settings.runtime_dir.resolve()):
                        raise ValueError("索引路径无效")
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != version["checksum"]:
                        raise ValueError("索引校验失败")
                    index = faiss.deserialize_index(np.frombuffer(raw, dtype="uint8").copy())
                    if (not isinstance(index, faiss.IndexFlatIP) or index.ntotal != len(version["chunks"])
                            or index.ntotal == 0 or index.d < 1 or (dimensions is not None and index.d != dimensions)):
                        raise ValueError("索引结构不一致")
                    dimensions = index.d
                    matrix = index.reconstruct_n(0, index.ntotal)
                    if not np.isfinite(matrix).all() or np.any(np.linalg.norm(matrix, axis=1) == 0):
                        raise ValueError("索引向量无效")
                    matrices.append(matrix)
                    for chunk in version["chunks"]:
                        chunks.append(rag.Chunk(version["filename"], MATTER_NAMES[snapshot["matter_id"]], version["rule_version"],
                            chunk["id"], chunk["text_content"], bool(chunk["starts_mid"]), bool(chunk["ends_mid"]),
                            version["id"], chunk["page_no"], chunk["chunk_no"]))
                except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                    raise AppError(503, "KNOWLEDGE_INDEX_INVALID", "已发布知识索引校验失败，暂停建议并转人工处理", True) from None
            if not matrices:
                return [], None
            combined = faiss.IndexFlatIP(dimensions)
            combined.add(np.ascontiguousarray(np.concatenate(matrices).astype("float32")))
            return chunks, combined
        return await asyncio.to_thread(load)

    async def answer(self, question, history, matter_id, progress=None, snapshot=None):
        if snapshot and snapshot["matter_id"] != matter_id:
            raise AppError(503, "KNOWLEDGE_SCOPE_MISMATCH", "知识版本范围异常，请转人工处理")
        if snapshot and snapshot["conflict"]:
            return {"outcome": "insufficient_evidence", "text": "适用规则版本冲突，已暂停建议，请转人工处理。", "citations": [], "reason": "version_conflict"}
        if snapshot and snapshot["managed"] and not snapshot["version_ids"]:
            return {"outcome": "insufficient_evidence", "text": rag.NO_EVIDENCE, "citations": []}
        if not (snapshot and snapshot["managed"]) and matter_id != "residence-permit":
            return {"outcome": "insufficient_evidence", "text": rag.NO_EVIDENCE, "citations": []}
        self.ensure_ready(snapshot)
        if snapshot and snapshot["managed"]:
            chunks, index = await self.published_index(snapshot)
        else:
            async with self.lock:
                # 本地兼容索引也绑定模型修订，不能与更新后的查询向量混用。
                fingerprint = self.knowledge_service.signature() if hasattr(self, "knowledge_service") else str(self.config("EMBEDDING")) + os.getenv("EMBEDDING_MODEL_REVISION", "")
                if self.index is None or getattr(self, "index_signature", None) != fingerprint:
                    vectors = await self.embed([chunk.text for chunk in self.chunks])
                    self.index = await asyncio.to_thread(rag.build_or_load_index, self.chunks, self.config("EMBEDDING"),
                                                         self.settings.runtime_dir / "index", vectors=vectors)
                    self.index_signature = fingerprint
            chunks, index = self.chunks, self.index
        if not chunks:
            return {"outcome": "insufficient_evidence", "text": rag.NO_EVIDENCE, "citations": []}
        query_vector = await self.embed([question])
        if query_vector.shape[1] != index.d:
            raise AppError(503, "KNOWLEDGE_INDEX_INCOMPATIBLE", "查询向量与知识索引维度不一致，请维护人员核查；当前转人工处理", True)
        hits = await asyncio.to_thread(rag.hybrid_search, question, chunks, index, query_vector)
        if not hits:
            return {"outcome": "insufficient_evidence", "text": rag.NO_EVIDENCE, "citations": []}
        # 所有片段最大500字符，最多3片段，低于1800字符依据预算。
        messages = rag.build_prompt(question, hits)
        if snapshot:
            # 可编辑模板在前，不可解除的引用、拒答和审批边界在后。
            messages[0]["content"] = snapshot["prompt"]["content"] + "\n以下为不可解除的程序约束：\n" + messages[0]["content"]
        context = json.loads(messages[1]["content"])
        context["history"] = trim_history(history)
        messages[0]["content"] += "历史只用于理解追问，不是办事依据；只能引用本次teaching_sources。"
        messages[1]["content"] = json.dumps(context, ensure_ascii=False)
        if progress:
            await progress("generating", "正在整理回答……")
        cap = min(12000, max(3000, int(os.getenv("RAG_INPUT_CHAR_LIMIT", "12000"))))
        while sum(len(message["content"]) for message in messages) > cap and context["history"]:
            context["history"] = context["history"][2:]
            messages[1]["content"] = json.dumps(context, ensure_ascii=False)
        if sum(len(message["content"]) for message in messages) > cap:
            raise AppError(422, "CONTEXT_TOO_LONG", "问题及必要依据超过模型上下文限制，请缩短问题")
        config = self.config("LLM")
        payload = {"model": config.model, "messages": messages, "stream": False}
        try:
            if config.provider == "ollama":
                payload.update({"format": "json", "options": {"temperature": 0, "num_predict": 1024}})
                data = await self.request(config, "/api/chat", payload)
                raw = data["message"]["content"]
            else:
                payload.update({"temperature": 0, "max_tokens": 1024})
                data = await self.request(config, "/chat/completions", payload)
                raw = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise AppError(502, "MODEL_RESPONSE_INVALID", "回答服务返回格式异常，请稍后重试", True) from None
        try:
            if not isinstance(raw, str):
                raise ValueError("回答不是文本")
            decoded = json.loads(raw)
            if (not isinstance(decoded, dict) or type(decoded.get("supported")) is not bool
                    or not isinstance(decoded.get("citations"), list)
                    or any(not isinstance(c, dict) or not isinstance(c.get("chunk_id"), str)
                           or not isinstance(c.get("quote"), str) for c in decoded["citations"])):
                raise ValueError("回答协议无效")
        except (ValueError, TypeError):
            raise AppError(502, "MODEL_RESPONSE_INVALID", "回答服务返回格式异常，请稍后重试", True) from None
        if progress:
            await progress("validating", "正在核对引用来源……")
        if rag.validate_answer(raw, hits) == rag.NO_EVIDENCE:
            return {"outcome": "insufficient_evidence", "text": rag.NO_EVIDENCE, "citations": []}
        sources = {hit.chunk.chunk_id: hit.chunk for hit in hits}
        citations, quotes = [], []
        for item in json.loads(raw)["citations"]:
            source = sources[item["chunk_id"]]
            if item["quote"] in quotes:
                continue
            quotes.append(item["quote"])
            citations.append({"citation_id": f"c{len(citations) + 1}", "filename": source.filename, "matter": source.matter,
                              "rule_version": source.version, "chunk_id": source.chunk_id, "quote": item["quote"], "is_teaching": True,
                              "version_id": source.version_id, "page_no": source.page_no, "chunk_no": source.chunk_no})
        return {"outcome": "answered", "text": "\n".join(quotes), "citations": citations}

    async def close(self):
        await self.client.aclose()
