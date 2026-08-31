# app/tools/verify_tool.py —— 验真 / 查重工具
# 业务：真伪查验 + 查重（同一票号不得重复报销）
from app.adapters.base import VerifyProvider


class VerifyTool:
    """验真/查重：封装 VerifyProvider，供验真节点调用"""

    def __init__(self, provider: VerifyProvider):
        # 作用：注入验真实现（Mock/真实可替换）
        self._provider = provider

    def check(self, invoice_data: dict) -> dict:
        """返回 {verified, duplicate, note}"""
        return self._provider.verify(invoice_data)
