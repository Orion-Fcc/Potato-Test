"""本地向量化：bge-small-zh-v1.5（ONNX，int8 量化，23 MB）。

## 为什么用本地模型而不是调网关
这是个桌面单机产品。检索增强如果要求"再配一个 embedding 的 API key + 一次网络往返"，
就会出现三种退化：断网不能用、换网关要重配、每次检索多几百毫秒。
onnxruntime 本来就在安装包里（OCR 用），模型量化后 23 MB —— 代价是包大一点，换来完全离线。

## 四个约定（都直接影响检索质量，改之前想清楚）

1. **查询要加指令前缀，文档不加**。bge-zh 的训练配方是这样：query 前面加
   「为这个句子生成表示以用于检索相关文章：」。两边都用原文或两边都加，余弦分会明显偏低。
2. **CLS 池化 + L2 归一化**，不是 mean pooling —— bge v1.5 的官方用法。
3. **失败一律返回 None**。没装模型 / 没装 onnxruntime / 推理炸了，都算"这次没有向量"，
   调用方（`app/knowledge.py`）自动退回关键词检索。向量是增强，**不能让检索整体坏掉**。
4. **批量、限长**。单条最长 512 token（模型上限），批 16 条 —— 大批量一次性喂给 ONNX
   会把内存顶上去，而这条路径经常在后台跑。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("potato.embeddings")

MODEL_DIR = Path(__file__).resolve().parent / "embedding_models" / "bge-small-zh-v1.5"
MODEL_FILE = MODEL_DIR / "onnx" / "model_quantized.onnx"
TOKENIZER_FILE = MODEL_DIR / "tokenizer.json"

# 模型标识：写进 knowledge_chunk.embed_model。换模型/换精度时要能识别出旧向量已过期。
MODEL_TAG = "bge-small-zh-v1.5-int8"

# bge-zh 的查询指令（见文件头约定 1）。文档侧不加。
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

MAX_TOKENS = 512
BATCH = 16

_lock = threading.Lock()
_state: dict = {"tried": False, "tok": None, "sess": None}


def available() -> bool:
    """模型和 onnxruntime 都在才谈得上向量化。结果缓存，避免每次检索都探盘。"""
    return _load() is not None


def _load():
    """懒加载 tokenizer + ONNX 会话。失败返回 None（并记住失败，不反复重试）。"""
    with _lock:
        if _state["tried"]:
            return _state["sess"] and _state["tok"]
        _state["tried"] = True
        try:
            import onnxruntime  # noqa: F401  （缺了就走 except）
            from tokenizers import Tokenizer

            if not MODEL_FILE.is_file() or not TOKENIZER_FILE.is_file():
                log.info("向量模型缺失：%s", MODEL_DIR)
                return None
            tok = Tokenizer.from_file(str(TOKENIZER_FILE))
            tok.enable_truncation(max_length=MAX_TOKENS)
            tok.enable_padding(length=None)  # 批内动态补齐，见 _encode
            sess = onnxruntime.InferenceSession(
                str(MODEL_FILE), providers=["CPUExecutionProvider"]
            )
            _state["tok"], _state["sess"] = tok, sess
            log.info("向量模型已加载：%s", MODEL_TAG)
            return tok and sess
        except Exception as exc:  # noqa: BLE001
            log.info("向量模型不可用（%s: %s），检索退回关键词", type(exc).__name__, exc)
            return None


def _encode(texts: list[str]) -> list[list[float]] | None:
    """跑一遍 ONNX，CLS 池化 + L2 归一化。"""
    if _load() is None:
        return None
    tok, sess = _state["tok"], _state["sess"]
    try:
        encs = tok.encode_batch(texts)
        # 批内按最长补齐（模型要矩形输入）
        width = max(len(e.ids) for e in encs)
        ids, mask, types = [], [], []
        for e in encs:
            pad = width - len(e.ids)
            ids.append(e.ids + [0] * pad)
            mask.append(e.attention_mask + [0] * pad)
            types.append(e.type_ids + [0] * pad)
        import numpy as np

        feeds = {
            "input_ids": np.array(ids, dtype=np.int64),
            "attention_mask": np.array(mask, dtype=np.int64),
        }
        # 有些导出带 token_type_ids，有些不带 —— 按会话实际输入喂
        if any(i.name == "token_type_ids" for i in sess.get_inputs()):
            feeds["token_type_ids"] = np.array(types, dtype=np.int64)
        out = sess.run(None, feeds)[0]  # (batch, seq, hidden)
        cls = out[:, 0, :]  # ★ CLS 池化
        norm = np.linalg.norm(cls, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return (cls / norm).tolist()  # ★ L2 归一化（之后余弦 = 点积）
    except Exception as exc:  # noqa: BLE001
        log.info("向量推理失败（%s: %s），本次退回关键词", type(exc).__name__, exc)
        return None


def embed_documents(texts: list[str]) -> list[list[float]] | None:
    """文档侧：不加指令前缀。分批评分，任一批失败就整次视为不可用（宁可不写脏向量）。"""
    if not texts:
        return []
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        part = _encode([t[:4000] for t in texts[i : i + BATCH]])
        if part is None:
            return None
        out.extend(part)
    return out


def embed_query(text: str) -> list[float] | None:
    """查询侧：**加**指令前缀（见文件头约定 1）。"""
    if not (text or "").strip():
        return None
    r = _encode([QUERY_PREFIX + text.strip()])
    return r[0] if r else None


def dumps(vec: list[float]) -> str:
    """存库用。圆到 6 位小数 —— 512 维 × 6 位，比全精度省一半体积，检索分差 < 1e-4。"""
    return json.dumps([round(x, 6) for x in vec], separators=(",", ":"))


def loads(raw: str) -> list[float] | None:
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) and v else None
    except Exception:  # noqa: BLE001
        return None
