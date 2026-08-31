# app/graphs/base_subgraph.py —— 子图构建模板
# 业务：所有业务子图共用同一构建方式；业务差异只体现在"节点列表"上（docs/01 §4.2 子图模板）
from typing import Callable

from langgraph.graph import END, START, StateGraph

from app.core.state import ReimbursementState


def build_subgraph(name: str, nodes: list[tuple[str, Callable]]):
    """构建并编译业务子图（线性串联；节点内部对 returned 状态短路）

    参数：
        name:  业务名，用于节点命名（如 travel）
        nodes: [(节点名, 节点函数)]，按顺序执行
    """
    # 作用：新建状态图（复用全局 State Schema）
    builder = StateGraph(ReimbursementState)
    names: list[str] = []
    for node_name, fn in nodes:
        full = f"{name}_{node_name}"
        builder.add_node(full, fn)
        names.append(full)
    # 作用：线性串联 START → n0 → n1 → ... → END
    builder.add_edge(START, names[0])
    for src, dst in zip(names, names[1:]):
        builder.add_edge(src, dst)
    builder.add_edge(names[-1], END)
    # 作用：子图无需 checkpointer（无 HITL），由父图 invoke 调用
    return builder.compile()
