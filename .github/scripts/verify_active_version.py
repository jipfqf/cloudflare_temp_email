#!/usr/bin/env python3
"""Cloudflare Worker 部署后校验: 当前激活版本必须携带 D1 绑定。

背景: wrangler 在 worker 带 [assets] 静态资源模式下, 一次 deploy 会先后创建
两个版本 - 先是带绑定的脚本版本, 约 5 秒后再创建一个"0 绑定的纯资源版本",
且后者可能抢走 100% 流量, 导致线上 env.DB 缺失、页面全部 404(2026-10-08 事故)。
本脚本等待切流完成后, 通过 Cloudflare API 校验"真实生效的版本", 不通过则失败。
"""
import json
import os
import time
import urllib.request

SCRIPT_NAME = os.environ.get("SCRIPT_NAME", "mail-worker")
REQUIRED_BINDING = "DB"


def main() -> None:
    token = os.environ["CLOUDFLARE_API_TOKEN"]
    acct = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    base = f"https://api.cloudflare.com/client/v4/accounts/{acct}"

    def get(path: str) -> dict:
        req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    time.sleep(5)  # 等待版本切流完成
    deployments = get(f"/workers/scripts/{SCRIPT_NAME}/deployments")["result"]["deployments"]
    if not deployments:
        raise SystemExit("FAIL: no deployments found for " + SCRIPT_NAME)
    active_vid = deployments[0]["versions"][0]["version_id"]
    version = get(f"/workers/scripts/{SCRIPT_NAME}/versions/{active_vid}")["result"]
    bindings = [b["name"] for b in version.get("resources", {}).get("bindings", [])]
    number = version.get("number")
    print(f"active version={active_vid} number={number} bindings={bindings}")
    if REQUIRED_BINDING not in bindings:
        raise SystemExit(
            f"FAIL: active version {number} has no {REQUIRED_BINDING} binding (bindings={bindings})!"
        )
    print(f"OK: active version carries {REQUIRED_BINDING} binding")


if __name__ == "__main__":
    main()
