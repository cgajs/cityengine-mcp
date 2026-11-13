"""cityengine-tools：CityEngine Cloud 的 OpenAI 兼容 function-calling 适配层。

用法：
    from cityengine_tools import TOOLS, call_tool

    # TOOLS 直接喂给任何 OpenAI 兼容 client 的 tools= 参数
    resp = client.chat.completions.create(model=..., messages=msgs, tools=TOOLS)
    for tc in resp.choices[0].message.tool_calls or []:
        result = call_tool(tc.function.name, json.loads(tc.function.arguments))
"""

import json
from importlib.resources import files

from .client import GatewayError, TOOL_NAMES, call_tool

TOOLS = json.loads(files("cityengine_tools").joinpath("tools.json").read_text(encoding="utf-8"))

__all__ = ["TOOLS", "TOOL_NAMES", "call_tool", "GatewayError"]
__version__ = "0.1.0"
