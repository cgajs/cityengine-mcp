# cityengine-tools

CityEngine Cloud 的 OpenAI 兼容 function-calling 适配层。把 6 个云端能力
（generate_cga / parse_cga / generate_model / list_demos / get_demo / get_quota）
导出为标准 OpenAI tools JSON schema，任何支持 function calling 的模型
（deepseek / qwen / glm / 混元 / gpt / 文心……）都能直接调用。

- 网关契约：`openapi-v1.yaml`（参数名/类型/必填与其一致）
- 鉴权：`X-API-Key` 头，`cvk_` 前缀插件 key（https://www.cityengine.cn 用户中心签发）
- 与 `cityengine-mcp`（MCP server）共用同一套网关端点，本包面向无 MCP 客户端的模型

## 安装

```bash
pip install cityengine-tools   # 或本地：pip install ./cityengine-tools
```

## 最小用法（任意 OpenAI 兼容模型）

```python
import json, os
from openai import OpenAI
from cityengine_tools import TOOLS, call_tool

client = OpenAI(api_key=os.environ["MODEL_API_KEY"], base_url="<模型服务商的 OpenAI 兼容地址>")
msgs = [{"role": "user", "content": "用工具查一下我当前的 CityEngine 额度"}]
resp = client.chat.completions.create(model="<model>", messages=msgs, tools=TOOLS)
msg = resp.choices[0].message
msgs.append(msg)
for tc in msg.tool_calls or []:
    result = call_tool(tc.function.name, json.loads(tc.function.arguments))
    msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})
print(client.chat.completions.create(model="<model>", messages=msgs).choices[0].message.content)
```

`CITYENGINE_API_KEY` 环境变量提供 CityEngine 插件 key（也可 `call_tool(..., api_key="cvk_...")` 显式传入）。

各模型现成示例见 `examples/`：deepseek、qwen（dashscope 兼容模式）、glm（智谱）、hunyuan（腾讯，未实测）。

## 工具一览

| 工具 | 网关端点 | 说明 |
|---|---|---|
| generate_cga | POST /v1/nl2cga | 一句话 → CGA 源码（同步，不渲染） |
| parse_cga | POST /v1/generate | CGA + 属性 → glTF/glb（异步 job，自动轮询） |
| generate_model | POST /v1/nl2model | 一句话 → 三维模型（一键，异步 job） |
| list_demos | GET www.cityengine.cn/home-demos.json | 官网首页 demo 清单 |
| get_demo | GET home-scenes/{id}.json | demo 属性面板/规模快照 |
| get_quota | GET /v1/account/quota | 额度与限流查询 |

全部只读计算型端点，无任何写操作。`call_tool` 额外关键字参数（不进 schema）：
`download_glb`（默认 False，只回签名 URL）、`output_dir`、`timeout_s`。

## REST 直连

不想装包的用户可按 `openapi-v1.yaml` 直接 HTTP 调用网关，能力等价。
