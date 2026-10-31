"""阶段1独立命令行入口；核心逻辑已提取供Web复用，运行方式保持兼容。"""
import argparse
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from zhengwu_precheck.core.guide_rag import (
    ApiConfig, Chunk, Hit, NO_EVIDENCE, build_or_load_index, build_prompt,
    call_llm, check_base_url, embed_texts, load_chunks, retrieve, tokenize, validate_answer,
)

# 保持原阶段1测试使用的HTTP边界可注入。
import httpx

# 【保留在 examples】命令行装配仅用于验证流程，不引入Web或其他业务能力。
def main() -> int:
    parser = argparse.ArgumentParser(description="示例指南问答：BM25＋FAISS＋模型原文引用")
    parser.add_argument("--guide", type=Path, default=ROOT / "data" / "居住证申领_示例指南.md")
    parser.add_argument("--index-dir", type=Path, default=Path(tempfile.gettempdir()) / "zhengwu-guide-qa")
    parser.add_argument("--question", help="单次提问；不填则进入交互模式")
    parser.add_argument("--retrieve-only", action="store_true", help="只验证检索，仍需要嵌入API")
    parser.add_argument("--rebuild", action="store_true", help="重新建立FAISS索引")
    parser.add_argument("--min-similarity", type=float, default=0.45)
    parser.add_argument("--llm-provider", default=os.getenv("LLM_PROVIDER", "openai-compatible"), choices=["openai-compatible", "ollama"])
    parser.add_argument("--llm-model", default=os.getenv("LLM_MODEL", ""))
    parser.add_argument("--llm-base-url", default=os.getenv("LLM_BASE_URL", ""))
    parser.add_argument("--llm-key-env", default="LLM_API_KEY")
    parser.add_argument("--embedding-model", default=os.getenv("EMBEDDING_MODEL", ""))
    parser.add_argument("--embedding-base-url", default=os.getenv("EMBEDDING_BASE_URL", ""))
    parser.add_argument("--embedding-key-env", default="EMBEDDING_API_KEY")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("需要Python 3.11或更高版本")
    if args.index_dir.resolve().is_relative_to(ROOT):
        parser.error("本原型要求 --index-dir 指向仓库外目录")
    if not -1 <= args.min_similarity <= 1:
        parser.error("相似度阈值必须在-1与1之间")
    if not args.embedding_model or not args.embedding_base_url:
        parser.error("请配置 EMBEDDING_MODEL 和 EMBEDDING_BASE_URL")
    if not args.retrieve_only and (not args.llm_model or not args.llm_base_url):
        parser.error("请配置 LLM_MODEL 和 LLM_BASE_URL")
    embedding = ApiConfig("openai-compatible", args.embedding_model, args.embedding_base_url, args.embedding_key_env)
    llm = ApiConfig(args.llm_provider, args.llm_model, args.llm_base_url, args.llm_key_env)
    try:
        chunks = load_chunks(args.guide)
        index = build_or_load_index(chunks, embedding, args.index_dir, rebuild=args.rebuild)
        print(f"示例指南问答原型｜{len(chunks)}个片段｜索引：{args.index_dir.resolve()}")
        print("仅验证指南问答；不提供材料识别、预检、人工工单或正式审批。")
        while True:
            if args.question is not None:
                question = args.question.strip()
            else:
                try:
                    question = input("\n问题（输入exit退出）：").strip()
                except EOFError:
                    break
            if question.lower() in {"exit", "quit", "退出"}:
                break
            if not question:
                if args.question is not None:
                    parser.error("问题不能为空")
                continue
            hits = retrieve(question, chunks, index, embedding, min_similarity=args.min_similarity)
            if args.retrieve_only:
                print("仅检索结果，不是模型回答：")
                for hit in hits:
                    print(f"{hit.chunk.chunk_id}｜{hit.chunk.filename}｜{hit.chunk.matter}｜{hit.chunk.version}"
                          f"｜BM25={hit.bm25:.3f}｜余弦={hit.similarity:.3f}｜RRF={hit.fused_score:.4f}\n{hit.chunk.text}\n")
                if not hits:
                    print(NO_EVIDENCE)
            elif not hits:
                print(NO_EVIDENCE)  # 无候选片段时不调用LLM，防止无依据生成。
            else:
                answer = validate_answer(call_llm(build_prompt(question, hits), llm), hits)
                print(answer)
            if args.question is not None:
                break
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        # 服务故障与“依据不足”分开报告，不能把网络异常描述为知识库没有答案。
        print(f"运行失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已退出。")
