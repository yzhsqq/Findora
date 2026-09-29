"""Explicit, point-limited CJ detail and logistics trial tools.

AgentScope generates schemas from runtime annotations, so this module does not
use postponed annotations.
"""
import json

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from app.infrastructure.cj_live_quote import CJQuoteError, CJLiveQuoteService
from app.infrastructure.context import ShoppingContext
from app.infrastructure.eventbus import TradeEventBus


def build_cj_live_tools(service: CJLiveQuoteService, bus: TradeEventBus):
    async def cj_product_detail_tool(product_id: str) -> ToolChunk:
        """按 CJ 商品 ID 获取真实规格详情；仅用户需要具体详情时调用，缺失品牌或产地保持未知。"""
        session_id = ShoppingContext.current_session_id()
        bus.publish(session_id, "tool.invoke", {"tool": "cj_product_detail_tool", "args": {"product_id": product_id}})
        try:
            product = await service.detail(product_id)
        except CJQuoteError as error:
            bus.publish(session_id, "tool.result", {"tool": "cj_product_detail_tool", "error": str(error)})
            return ToolChunk(content=[TextBlock(type="text", text=f"[error] {error}")], state=ToolResultState.ERROR)
        bus.publish(session_id, "tool.result", {"tool": "cj_product_detail_tool", "product_id": product_id})
        return ToolChunk(content=[TextBlock(type="text", text=json.dumps({"product": product}, ensure_ascii=False))],
                         state=ToolResultState.SUCCESS)

    async def cj_freight_quote_tool(product_id: str, ship_to: str, sku_id: str = "", quantity: int = 1) -> ToolChunk:
        """对指定 CJ 商品、规格和目的国查询 CJ 物流试算；不代表最终支付价。

        sku_id 留空时试算详情中的第一个规格，结果会标记 first_variant_assumed。
        同一条件短期缓存；只在用户明确询问配送或到手费用时调用。
        """
        session_id = ShoppingContext.current_session_id()
        args = {"product_id": product_id, "sku_id": sku_id, "ship_to": ship_to, "quantity": quantity}
        bus.publish(session_id, "tool.invoke", {"tool": "cj_freight_quote_tool", "args": args})
        try:
            quote = await service.quote(product_id, sku_id, ship_to, quantity)
        except CJQuoteError as error:
            bus.publish(session_id, "tool.result", {"tool": "cj_freight_quote_tool", "error": str(error)})
            return ToolChunk(content=[TextBlock(type="text", text=f"[error] {error}")], state=ToolResultState.ERROR)
        bus.publish(session_id, "tool.result", {"tool": "cj_freight_quote_tool", "quote": quote})
        return ToolChunk(content=[TextBlock(type="text", text=json.dumps({"quote": quote}, ensure_ascii=False))],
                         state=ToolResultState.SUCCESS)

    return cj_product_detail_tool, cj_freight_quote_tool
