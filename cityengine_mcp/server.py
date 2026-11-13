"""CityEngine Cloud MCP server (stdio).

把 CityEngine 云端插件网关能力暴露为 MCP 工具，供 kimi code / codex /
Claude Desktop 等支持 MCP 的大模型客户端直接调用：

  - parse_cga       CGA 源码 + 属性 → 引擎解析 → 三维模型（glb）
  - generate_cga    自然语言 → NL2CGA → CGA 源码（不渲染）
  - generate_model  自然语言 → CGA → 引擎解析 → 三维模型（一键）
  - list_demos      列出官网首页 demo 案例
  - get_demo        查看某个 demo 的属性面板 / 引擎 / 规模信息
  - get_quota       查询当前 API key 的额度用量

鉴权：环境变量 CITYENGINE_API_KEY（插件 API key，cvk_ 前缀，
在 https://www.cityengine.cn 用户中心「插件 Key」页签发，或调用
网关 POST /v1/register 自助注册签发）。

只调用计算型只读端点（渲染/生成/查询），无任何写操作。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

GATEWAY_BASE = os.environ.get("CITYENGINE_GATEWAY_BASE", "https://api.cityengine.cn").rstrip("/")
DEMOS_BASE = os.environ.get("CITYENGINE_DEMOS_BASE", "https://www.cityengine.cn").rstrip("/")
API_KEY = os.environ.get("CITYENGINE_API_KEY", "").strip()

DEFAULT_OUTPUT_DIR = os.environ.get(
    "CITYENGINE_OUTPUT_DIR",
    str(Path.cwd() / "cityengine_mcp_output"),
)
POLL_INTERVAL_S = float(os.environ.get("CITYENGINE_POLL_INTERVAL_S", "2"))
MAX_POLL_S = float(os.environ.get("CITYENGINE_MAX_POLL_S", "300"))

# 与官方 web-sdk 一致的默认地块（30m x 20m），仅在调用方未传 initial_shape 时使用
DEFAULT_LOT = {
    "type": "Polygon",
    "pts": [[0, 0], [30, 0], [30, 20], [0, 20]],
    "centerX": 15,
    "centerZ": 10,
}

MAX_CGA_SOURCE_LEN = 200_000  # 网关契约上限
MAX_TEXT_LEN = 2_000

mcp = FastMCP(
    "cityengine",
    instructions=(
        "CityEngine 云端能力：CGA 规则解析生成三维模型(glTF/glb)、"
        "自然语言一句话生成 CGA 规则(NL2CGA)、一句话直接生成三维模型、"
        "官网 demo 案例查询。所有工具均为只读计算型接口。"
    ),
)


class GatewayError(RuntimeError):
    """网关错误，带 HTTP 状态码与 error_code。"""

    def __init__(self, status: int, payload: str, stage: str):
        code, message, retry_after = "", "", None
        try:
            d = json.loads(payload)
            if isinstance(d, dict) and "detail" in d:
                d = d["detail"]
            if isinstance(d, dict):
                code = d.get("error_code", "") or ""
                message = d.get("message", "") or ""
                retry_after = d.get("retry_after")
        except Exception:
            message = payload[:300]
        super().__init__(f"[{stage}] HTTP {status} {code}: {message}")
        self.status = status
        self.error_code = code
        self.retry_after = retry_after


def _require_key() -> str:
    if not API_KEY:
        raise GatewayError(
            0,
            json.dumps({
                "error_code": "NO_API_KEY",
                "message": "未配置 CITYENGINE_API_KEY 环境变量。请到 https://www.cityengine.cn "
                           "用户中心签发插件 Key（cvk_ 前缀），或调用 POST /v1/register 自助注册。",
            }),
            "config",
        )
    return API_KEY


def _request(method: str, path: str, body: Optional[dict] = None,
             expect: int = 200, timeout: float = 30.0, retries: int = 3) -> dict:
    """带 429 退避的网关请求。path 可为绝对 URL（下载签名地址）。"""
    key = _require_key()
    url = path if path.startswith("http") else GATEWAY_BASE + path
    headers = {"X-API-Key": key}
    for attempt in range(retries + 1):
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, json=body, headers=headers)
        if resp.status_code == 429 and attempt < retries:
            err = GatewayError(429, resp.text, f"{method} {path}")
            time.sleep(min(err.retry_after or 2, 60) + 0.2)
            continue
        if resp.status_code != expect:
            raise GatewayError(resp.status_code, resp.text, f"{method} {path}")
        return resp.json()
    raise GatewayError(429, '{"error_code":"RATE_LIMITED","message":"retry budget exhausted"}',
                       f"{method} {path}")


def _wait_job(job_id: str, timeout_s: float) -> dict:
    deadline = time.time() + min(timeout_s, MAX_POLL_S)
    while True:
        job = _request("GET", f"/v1/jobs/{job_id}")
        status = job.get("status")
        if status == "succeeded":
            return job
        if status == "failed":
            err = job.get("error") or {}
            raise GatewayError(
                0,
                json.dumps({"error_code": err.get("error_code", "JOB_FAILED"),
                            "message": err.get("message", "job failed")}),
                f"job {job_id}",
            )
        if time.time() > deadline:
            raise GatewayError(
                0,
                json.dumps({"error_code": "POLL_TIMEOUT",
                            "message": f"job {job_id} 轮询超时（{timeout_s}s），"
                                       f"可稍后调用 get_quota 确认额度后用 job_id 到 "
                                       f"GET /v1/jobs/{job_id} 查询"}),
                f"job {job_id}",
            )
        time.sleep(POLL_INTERVAL_S)


def _download_glb(gltf: dict, job_id: str, output_dir: Optional[str]) -> dict:
    """下载 glb 并做 sha256 校验，返回落盘信息。"""
    out_dir = Path(output_dir or DEFAULT_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    url = gltf["url"]
    if not url.startswith("http"):
        url = GATEWAY_BASE + "/" + url.lstrip("/")
    with httpx.Client(timeout=120.0) as client:
        resp = client.get(url)
    if resp.status_code != 200:
        raise GatewayError(resp.status_code, resp.text, f"download {gltf['url']}")
    data = resp.content
    sha = hashlib.sha256(data).hexdigest()
    expected = gltf.get("sha256")
    if expected and sha != expected:
        raise GatewayError(
            0,
            json.dumps({"error_code": "SHA256_MISMATCH",
                        "message": f"glb 校验失败：期望 {expected}，实际 {sha}"}),
            "download",
        )
    path = out_dir / f"{job_id}.glb"
    path.write_bytes(data)
    return {"glb_path": str(path), "size_bytes": len(data), "sha256": sha}


def _usage_of(job_or_resp: dict) -> dict:
    usage = job_or_resp.get("usage") or {}
    return {
        "quota_kind": usage.get("quota_kind"),
        "quota_cost": usage.get("quota_cost"),
        "quota_remaining": usage.get("quota_remaining"),
        "engine_ms": usage.get("engine_ms"),
    }


@mcp.tool()
def parse_cga(
    cga_source: str,
    attributes: Optional[dict] = None,
    initial_shape: Optional[dict] = None,
    seed: Optional[int] = None,
    download_glb: bool = True,
    output_dir: Optional[str] = None,
    timeout_s: float = 300,
) -> dict:
    """用 CityEngine 引擎解析 CGA 规则源码，生成三维模型（glTF/glb）。

    Args:
        cga_source: CGA 规则源码全文（手写或 generate_cga 的产物）。
        attributes: CGA attr 覆盖，如 {"height": 30}；键必须在规则的 attr 声明内。
        initial_shape: 初始地块多边形 {"type":"Polygon","pts":[[x,z],...]}，米制；
                       缺省用 30m x 20m 调试地块。
        seed: 随机种子，固定后可复现结果。
        download_glb: 是否把 glb 下载到本地（默认 True）。
        output_dir: glb 落盘目录（默认环境变量 CITYENGINE_OUTPUT_DIR 或当前目录下
                    cityengine_mcp_output/）。
        timeout_s: 轮询 job 的最长秒数（上限 300）。

    Returns:
        job_id、模型统计(nodes/vertices/triangles/textures_embedded)、glb 本地路径、
        签名下载地址(24h 有效)、sha256、计费用量、告警列表。
    """
    if not cga_source or not cga_source.strip():
        raise ValueError("cga_source 不能为空")
    if len(cga_source) > MAX_CGA_SOURCE_LEN:
        raise ValueError(f"cga_source 超过契约上限 {MAX_CGA_SOURCE_LEN} 字符")
    body: dict[str, Any] = {
        "cga_source": cga_source,
        "initial_shape": initial_shape or DEFAULT_LOT,
    }
    if attributes:
        body["attributes"] = attributes
    if seed is not None:
        body["seed"] = seed
    job = _request("POST", "/v1/generate", body, expect=202, timeout=30)
    job_id = job["job_id"]
    job = _wait_job(job_id, timeout_s)
    result = job.get("result") or {}
    gltf = result.get("gltf") or {}
    out: dict[str, Any] = {
        "job_id": job_id,
        "status": "succeeded",
        "stats": gltf.get("stats"),
        "gltf_url": gltf.get("url"),
        "gltf_expires_at": gltf.get("expires_at"),
        "sha256": gltf.get("sha256"),
        "size_bytes": gltf.get("size_bytes"),
        "warnings": result.get("warnings") or [],
        "usage": _usage_of(job),
    }
    if download_glb and gltf.get("url"):
        out["glb"] = _download_glb(gltf, job_id, output_dir)
    return out


@mcp.tool()
def generate_cga(
    text: Optional[str] = None,
    base_cga: Optional[str] = None,
    instruction: Optional[str] = None,
) -> dict:
    """自然语言一句话 → NL2CGA → CGA 规则源码（不渲染，同步返回）。

    两种模式二选一：
      1. text：自然语言描述，如「三层小楼，坡屋顶，底层商铺」。
      2. base_cga + instruction：在既有 CGA 上按指令修改（如「把立面材质改成红砖」）。

    LLM 兜底模式最长约 90s，属正常现象。

    Returns:
        cga_source（可喂给 parse_cga / generate_model 的 cga_edit 回传）、
        engine_path（self/kimi_fallback/hybrid）、warnings、usage。
    """
    if bool(text) == bool(base_cga and instruction):
        raise ValueError("text 与 (base_cga + instruction) 必须二选一")
    if text and len(text) > MAX_TEXT_LEN:
        raise ValueError(f"text 超过契约上限 {MAX_TEXT_LEN} 字符")
    body: dict[str, Any] = {}
    if text:
        body["text"] = text
    else:
        body["cga_edit"] = {"base_cga": base_cga, "instruction": instruction}
    resp = _request("POST", "/v1/nl2cga", body, expect=200, timeout=150)
    return {
        "request_id": resp.get("request_id"),
        "cga_source": resp.get("cga_source"),
        "engine_path": resp.get("engine_path"),
        "warnings": resp.get("warnings") or [],
        "usage": _usage_of(resp),
    }


@mcp.tool()
def generate_model(
    text: Optional[str] = None,
    base_cga: Optional[str] = None,
    instruction: Optional[str] = None,
    attributes: Optional[dict] = None,
    initial_shape: Optional[dict] = None,
    seed: Optional[int] = None,
    download_glb: bool = True,
    output_dir: Optional[str] = None,
    timeout_s: float = 300,
) -> dict:
    """自然语言一句话 → 直接生成三维模型（NL2CGA + 引擎解析一键串联）。

    两种模式二选一：
      1. text：自然语言描述，如「生成一栋 12 层现代玻璃幕墙办公楼」。
      2. base_cga + instruction：回传既有 CGA + 修改指令，二次生成再出模型。

    Args:（其余参数同 parse_cga）

    Returns:
        cga_source（供后续 cga_edit 回传）、模型统计、glb 本地路径、
        签名下载地址、sha256、usage、warnings。
    """
    if bool(text) == bool(base_cga and instruction):
        raise ValueError("text 与 (base_cga + instruction) 必须二选一")
    if text and len(text) > MAX_TEXT_LEN:
        raise ValueError(f"text 超过契约上限 {MAX_TEXT_LEN} 字符")
    body: dict[str, Any] = {"initial_shape": initial_shape or DEFAULT_LOT}
    if text:
        body["text"] = text
    else:
        body["cga_edit"] = {"base_cga": base_cga, "instruction": instruction}
    if attributes:
        body["attributes"] = attributes
    if seed is not None:
        body["seed"] = seed
    job = _request("POST", "/v1/nl2model", body, expect=202, timeout=30)
    job_id = job["job_id"]
    job = _wait_job(job_id, timeout_s)
    result = job.get("result") or {}
    gltf = result.get("gltf") or {}
    out: dict[str, Any] = {
        "job_id": job_id,
        "status": "succeeded",
        "cga_source": result.get("cga_source"),
        "stats": gltf.get("stats"),
        "gltf_url": gltf.get("url"),
        "gltf_expires_at": gltf.get("expires_at"),
        "sha256": gltf.get("sha256"),
        "size_bytes": gltf.get("size_bytes"),
        "warnings": result.get("warnings") or [],
        "usage": _usage_of(job),
    }
    if download_glb and gltf.get("url"):
        out["glb"] = _download_glb(gltf, job_id, output_dir)
    return out


@mcp.tool()
def list_demos() -> list[dict]:
    """列出 CityEngine 官网首页的 demo 案例（id、名称、引擎、来源日期、属性个数）。

    返回条目可传给 get_demo 查看属性面板细节。
    """
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{DEMOS_BASE}/home-demos.json")
    if resp.status_code != 200:
        raise GatewayError(resp.status_code, resp.text, "GET home-demos.json")
    demos = resp.json()
    return [
        {
            "id": d.get("id"),
            "name": d.get("name"),
            "engine": d.get("engine"),
            "source_date": d.get("sourceDate"),
            "attr_count": len(d.get("attrs") or []),
            "thumbnail": f"{DEMOS_BASE}/home-scenes/{d.get('id')}.jpg",
        }
        for d in demos
    ]


@mcp.tool()
def get_demo(demo_id: str) -> dict:
    """查看某个官网 demo 案例的详情：属性面板定义、材质数、节点数、引擎耗时。

    注意：demo 的 CGA 源码属付费内容（官网商城），本工具返回公开快照信息
    （属性 schema / 规模 / 引擎指标），适合做参数设计参考。

    Args:
        demo_id: list_demos 返回的 id，如 building-glass-20260928。
    """
    with httpx.Client(timeout=60.0) as client:
        r1 = client.get(f"{DEMOS_BASE}/home-demos.json")
        r2 = client.get(f"{DEMOS_BASE}/home-scenes/{demo_id}.json")
    if r1.status_code != 200:
        raise GatewayError(r1.status_code, r1.text, "GET home-demos.json")
    entry = next((d for d in r1.json() if d.get("id") == demo_id), None)
    if entry is None:
        raise GatewayError(
            404,
            json.dumps({"error_code": "DEMO_NOT_FOUND",
                        "message": f"demo '{demo_id}' 不在首页在册清单"}),
            "get_demo",
        )
    out: dict[str, Any] = {
        "id": demo_id,
        "name": entry.get("name"),
        "engine": entry.get("engine"),
        "source_date": entry.get("sourceDate"),
        "attrs": entry.get("attrs") or [],
    }
    if r2.status_code == 200:
        scene = (r2.json() or {}).get("scene") or {}
        meta = scene.get("meta") or {}
        out["snapshot"] = {
            "nodes": len(scene.get("nodes") or []),
            "materials": len(scene.get("materials") or []),
            "warnings": scene.get("warnings") or [],
            "timing": meta.get("timing"),
        }
    else:
        out["snapshot"] = None
        out["snapshot_notice"] = f"快照不可用（HTTP {r2.status_code}）"
    return out


@mcp.tool()
def get_quota() -> dict:
    """查询当前 API key 的套餐、额度用量（generate/NL2CGA）与限流参数。"""
    return _request("GET", "/v1/account/quota")


def main() -> None:
    if not API_KEY:
        print("[cityengine-mcp] 警告：未设置 CITYENGINE_API_KEY，"
              "generate/parse/quota 工具将报 NO_API_KEY", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
