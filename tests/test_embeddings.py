"""向量检索的单元测试（2026-10-19）。

守的是四件事，每一条都对应一个真实会坏的地方：

1. **维度与归一化** —— bge 必须出 512 维且 L2 归一化；不归一化的话"点积=余弦"这个前提就没了，
   阈值 0.5 会变成毫无意义的数字。
2. **查询加指令前缀、文档不加** —— bge-zh 的训练配方要求不对称。两边都加或都不加，
   余弦分会整体偏低，阈值一卡就全被过滤掉（表现为"检索突然什么都搜不到"）。
3. **语义能压过无关内容** —— 这是引入向量的**唯一理由**。如果这条不过，说明模型或池化用错了。
4. **模型缺失时必须优雅退化** —— 返回 None 让上层退回关键词，绝不能抛异常把检索整条链路打挂。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import embeddings as E  # noqa: E402


def test_dumps_loads_roundtrip():
    """存库格式：圆到 6 位小数后还能还原成同样的向量（阈值判断不受影响）。"""
    v = [0.1234567, -0.000001234, 0.9]
    back = E.loads(E.dumps(v))
    assert back is not None and len(back) == 3
    assert abs(back[0] - 0.123457) < 1e-9


def test_model_missing_degrades_to_none(monkeypatch):
    """★ 模型不在时：available() 假、embed 返回 None，**不抛异常**。"""
    monkeypatch.setattr(E, "MODEL_FILE", pathlib.Path("definitely-missing.onnx"))
    monkeypatch.setattr(E, "_state", {"tried": False, "tok": None, "sess": None})
    assert E.available() is False
    assert E.embed_documents(["测试"]) is None
    assert E.embed_query("测试") is None


requires_model = pytest.mark.skipif(not E.available(), reason="向量模型不存在（app/embedding_models）")


@requires_model
def test_dimension_and_l2_normalized():
    vecs = E.embed_documents(["计费收费标准"])
    assert vecs and len(vecs[0]) == 512
    norm = sum(x * x for x in vecs[0]) ** 0.5
    assert 0.99 <= norm <= 1.01, f"没归一化，范数 {norm}"


@requires_model
def test_query_prefix_makes_a_difference():
    """查询侧加了指令前缀，向量必须和"原文当文档"不同。"""
    same = "收费标准怎么填"
    q = E.embed_query(same)
    d = E.embed_documents([same])[0]
    assert q is not None and d is not None
    cos = sum(a * b for a, b in zip(q, d))
    assert cos < 0.999, "查询和文档向量几乎一样 —— 指令前缀没生效"


@requires_model
def test_semantic_beats_unrelated():
    """换个说法的命中必须明显高于无关内容（这条不过就说明向量白算）。"""
    docs = [
        "【教学场地收费标准】记录教学场地的使用价格，按小时计费。",
        "浏览器启动失败通常是 profile 目录被占用，关掉残留进程再试。",
    ]
    vecs = E.embed_documents(docs)
    q = E.embed_query("老师上课用的场地怎么收费")
    assert q is not None and vecs is not None
    cos = [sum(a * b for a, b in zip(q, v)) for v in vecs]
    assert cos[0] > cos[1] + 0.1, f"语义区分度不足：{cos}"
