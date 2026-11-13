"""CityEngine Cloud function-calling 适配层客户端。

把模型返回的 tool_calls 执行到 https://api.cityengine.cn 网关，
并回填 tool 结果。仅调用只读计算型端点，无任何写操作。

鉴权：X-API-Key 头（cvk_ 前缀，用户中心签发或 POST /v1/register 自助注册）。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx

GATEWAY_BASE = os.environ.get("CITYENGINE_GATEWAY_BASE", "https://api.cityengine.cn").rstrip("/")
DEMOS_BASE = os.environ.get("CITYENGINE_DEMOS_BASE", "https://www.cityengine.cn").rstrip("/")

DEFAULT_LOT = {
    "type": "Polygon",
    "pts": [[0, 0], [30, 0], [30, 20], [0, 20]],
    "centerX": 15,
    "centerZ": 10,
}

TOOL_NAMES = ("parse_cga", "generate_cga", "generate_model", "list_demos", "get_demo", "get_quota")


class GatewayError(RuntimeError):
    """网关错误，带 HTTP 状态码与 error_code。"""

    def __init__(self, status: int, payload: str, stage: str):
        code, message = "", ""
        try:
            d = json.loads(payload)
            if isinstance(d, dict) and "detail" in d:
                d = d["detail"]
            if isinstance(d, dict):
                code = d.get("error_code", "") or ""
                message = d.get("message", "") or ""
        except Exception:
            message = payload[:300]
        super().__init__(f"[{stage}] HTTP {status} {code}: {message}")
        self.status = status
        self.error_code = code


def _api_key(api_key: Optional[str]) -> str:
    key = (api_key or os.environ.get("CITYENGINE_API_KEY", "")).strip()
    if not key:
        raise GatewayError(0, '{"error_code":"NO_API_KEY","message":"未配置 CITYENGINE_API_KEY"}', "config")
    return key


def _request(method: str, path: str, api_key: str, body: Optional[dict] = None,
             expect: int = 200, timeout: float = 30.0, retries: int = 3) -> dict:
    url = path if path.startswith("http") else GATEWAY_BASE + path
    headers = {"X-API-Key": api_key}
    for attempt in range(retries + 1):
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, json=body, headers=headers)
        if resp.status_code == 429 and attempt < retries:
            time.sleep(min(resp.headers.get("Retry-After", 2) and int(resp.headers["Retry-After"]) or 2, 60) + 0.2)
            continue
        if resp.status_code != expect:
            raise GatewayError(resp.status_code, resp.text, f"{method} {path}")
        return resp.json()
    raise GatewayError(429, '{"error_code":"RATE_LIMITED"}', f"{method} {path}")


def _wait_job(job_id: str, api_key: str, timeout_s: float) -> dict:
    deadline = time.time() + min(timeout_s, 300)
    while True:
        job = _request("GET", f"/v1/jobs/{job_id}", api_key)
        status = job.get("status")
        if status == "succeeded":
            return job
        if status == "failed":
            err = job.get("error") or {}
            raise GatewayError(0, json.dumps({"error_code": err.get("error_code", "JOB_FAILED"),
                                              "message": err.get("message", "job failed")}),
                               f"job {job_id}")
        if time.time() > deadline:
            raise GatewayError(0, '{"error_code":"POLL_TIMEOUT"}', f"job {job_id}")
        time.sleep(2)


def _download_glb(gltf: dict, job_id: str, output_dir: Optional[str]) -> dict:
    out_dir = Path(output_dir or os.environ.get("CITYENGINE_OUTPUT_DIR", "cityengine_tools_output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    url = gltf["url"]
    if not url.startswith("http"):
        url = GATEWAY_BASE + "/" + url.lstrip("/")
    with httpx.Client(timeout=120.0) as client:
        resp = client.get(url)
    if resp.status_code != 200:
        raise GatewayError(resp.status_code, resp.text, "download glb")
    data = resp.content
    sha = hashlib.sha256(data).hexdigest()
    if gltf.get("sha256") and sha != gltf["sha256"]:
        raise GatewayError(0, '{"error_code":"SHA256_MISMATCH"}', "download glb")
    path = out_dir / f"{job_id}.glb"
    path.write_bytes(data)
    return {"glb_path": str(path), "size_bytes": len(data), "sha256": sha}


def _usage_of(d: dict) -> dict:
    u = d.get("usage") or {}
    return {"quota_kind": u.get("quota_kind"), "quota_cost": u.get("quota_cost"),
            "quota_remaining": u.get("quota_remaining"), "engine_ms": u.get("engine_ms")}


def _job_out(job: dict) -> dict:
    gltf = (job.get("result") or {}).get("gltf") or {}
    return {
        "job_id": job.get("job_id"),
        "status": "succeeded",
        "stats": gltf.get("stats"),
        "gltf_url": gltf.get("url"),
        "gltf_expires_at": gltf.get("expires_at"),
        "sha256": gltf.get("sha256"),
        "size_bytes": gltf.get("size_bytes"),
        "warnings": (job.get("result") or {}).get("warnings") or [],
        "usage": _usage_of(job),
    }


def call_tool(name: str, arguments: Optional[dict] = None, *,
              api_key: Optional[str] = None,
              download_glb: bool = False,
              output_dir: Optional[str] = None,
              timeout_s: float = 300) -> Any:
    """执行一个 CityEngine 工具调用并返回可回填给模型的 JSON 结果。

    Args:
        name: 工具名（TOOL_NAMES 之一）。
        arguments: 模型 tool_calls 里的 arguments（已 json.loads）。
        api_key: cvk_ 插件 key；缺省读环境变量 CITYENGINE_API_KEY。
        download_glb: parse_cga/generate_model 是否把 glb 下载到本地（默认 False，
                      只回签名 URL；LLM 场景通常无需落盘）。
        output_dir: glb 落盘目录。
        timeout_s: 异步 job 轮询上限秒数。
    """
    args = dict(arguments or {})

    if name == "get_quota":
        return _request("GET", "/v1/account/quota", _api_key(api_key))

    if name == "list_demos":
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(f"{DEMOS_BASE}/home-demos.json")
        if resp.status_code != 200:
            raise GatewayError(resp.status_code, resp.text, "GET home-demos.json")
        return [
            {"id": d.get("id"), "name": d.get("name"), "engine": d.get("engine"),
             "source_date": d.get("sourceDate"), "attr_count": len(d.get("attrs") or []),
             "thumbnail": f"{DEMOS_BASE}/home-scenes/{d.get('id')}.jpg"}
            for d in resp.json()
        ]

    if name == "get_demo":
        demo_id = args["demo_id"]
        with httpx.Client(timeout=60.0) as client:
            r1 = client.get(f"{DEMOS_BASE}/home-demos.json")
            r2 = client.get(f"{DEMOS_BASE}/home-scenes/{demo_id}.json")
        if r1.status_code != 200:
            raise GatewayError(r1.status_code, r1.text, "GET home-demos.json")
        entry = next((d for d in r1.json() if d.get("id") == demo_id), None)
        if entry is None:
            raise GatewayError(404, '{"error_code":"DEMO_NOT_FOUND"}', "get_demo")
        out: dict[str, Any] = {"id": demo_id, "name": entry.get("name"),
                               "engine": entry.get("engine"), "source_date": entry.get("sourceDate"),
                               "attrs": entry.get("attrs") or []}
        if r2.status_code == 200:
            scene = (r2.json() or {}).get("scene") or {}
            meta = scene.get("meta") or {}
            out["snapshot"] = {"nodes": len(scene.get("nodes") or []),
                               "materials": len(scene.get("materials") or []),
                               "warnings": scene.get("warnings") or [],
                               "timing": meta.get("timing")}
        else:
            out["snapshot"] = None
        return out

    key = _api_key(api_key)

    if name == "generate_cga":
        text, base_cga, instruction = args.get("text"), args.get("base_cga"), args.get("instruction")
        if bool(text) == bool(base_cga and instruction):
            raise ValueError("text 与 (base_cga + instruction) 必须二选一")
        body = {"text": text} if text else {"cga_edit": {"base_cga": base_cga, "instruction": instruction}}
        resp = _request("POST", "/v1/nl2cga", key, body, expect=200, timeout=150)
        return {"request_id": resp.get("request_id"), "cga_source": resp.get("cga_source"),
                "engine_path": resp.get("engine_path"), "warnings": resp.get("warnings") or [],
                "usage": _usage_of(resp)}

    if name == "parse_cga":
        body = {"cga_source": args["cga_source"], "initial_shape": args.get("initial_shape") or DEFAULT_LOT}
        for k in ("attributes", "seed"):
            if args.get(k) is not None:
                body[k] = args[k]
        job = _request("POST", "/v1/generate", key, body, expect=202, timeout=30)
        job = _wait_job(job["job_id"], key, timeout_s)
        out = _job_out(job)
        if download_glb and out.get("gltf_url"):
            out["glb"] = _download_glb({"url": out["gltf_url"], "sha256": out.get("sha256")},
                                       out["job_id"], output_dir)
        return out

    if name == "generate_model":
        text, base_cga, instruction = args.get("text"), args.get("base_cga"), args.get("instruction")
        if bool(text) == bool(base_cga and instruction):
            raise ValueError("text 与 (base_cga + instruction) 必须二选一")
        body = {"initial_shape": args.get("initial_shape") or DEFAULT_LOT}
        if text:
            body["text"] = text
        else:
            body["cga_edit"] = {"base_cga": base_cga, "instruction": instruction}
        for k in ("attributes", "seed"):
            if args.get(k) is not None:
                body[k] = args[k]
        job = _request("POST", "/v1/nl2model", key, body, expect=202, timeout=30)
        job = _wait_job(job["job_id"], key, timeout_s)
        out = _job_out(job)
        out["cga_source"] = (job.get("result") or {}).get("cga_source")
        if download_glb and out.get("gltf_url"):
            out["glb"] = _download_glb({"url": out["gltf_url"], "sha256": out.get("sha256")},
                                       out["job_id"], output_dir)
        return out

    raise ValueError(f"未知工具：{name}（支持 {TOOL_NAMES}）")
