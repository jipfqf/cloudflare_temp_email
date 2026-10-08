#!/usr/bin/env python3
"""mail-worker 激活版本自愈器 (heal_worker.py)

背景 (2026-10-08 404 事故根因):
  Cloudflare 上偶发出现"0 绑定 + 无脚本的纯 assets 版本"抢占 mail-worker 100% 流量。
  实测该版本: script 无 etag、bindings 为空、assets.serve_directly=true。
  结果: Pages 中间件转发过来的 /open_api /admin /user_api 全部 404 (站点全挂)。
  正常的健康版本必须同时具备: 有 script etag + 绑定里含 DB(D1)。

两种用法:
  1) CI 部署后 (backend_deploy.yaml):
       python3 heal_worker.py --watch 120 --expect <wrangler版本ID>
     含 --expect 时: 轮询最多 120s, 一旦被流氓版本抢走流量就立刻重新 pin 回正确版本,
     结束时激活版本正确 -> 退出码 0 (部署"直接成功"); pin 不回去才退出码 1。
  2) 定时巡检 (selfheal.yaml, 每 5 分钟):
       python3 heal_worker.py
     单次检查: 激活版本不健康则自动 pin 到最新健康版本并输出告警。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

SCRIPT_NAME = os.environ.get("WORKER_SCRIPT", "mail-worker")
REQUIRED_BINDING = "DB"


def api(method: str, path: str, payload=None):
    token = os.environ["CLOUDFLARE_API_TOKEN"]
    acct = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{acct}{path}",
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        return {"success": False, "status": exc.code, "error": exc.read().decode()[:300]}


def get_version(version_id: str) -> dict:
    res = api("GET", f"/workers/scripts/{SCRIPT_NAME}/versions/{version_id}")
    return res.get("result") or {}


def version_state(version_id: str) -> dict:
    v = get_version(version_id)
    resources = v.get("resources", {})
    bindings = [b.get("name") for b in resources.get("bindings", [])]
    script_ok = bool(resources.get("script", {}).get("etag"))
    return {
        "number": v.get("number"),
        "script_ok": script_ok,
        "has_db": REQUIRED_BINDING in bindings,
        "healthy": script_ok and REQUIRED_BINDING in bindings,
        "bindings": bindings,
    }


def active_version_id() -> str:
    res = api("GET", f"/workers/scripts/{SCRIPT_NAME}/deployments")
    deps = (res.get("result") or {}).get("deployments") or []
    if not deps:
        raise RuntimeError("no deployments found")
    return deps[0]["versions"][0]["version_id"]


def pin(version_id: str, message: str) -> bool:
    res = api(
        "POST",
        f"/workers/scripts/{SCRIPT_NAME}/deployments",
        {
            "strategy": "percentage",
            "versions": [{"version_id": version_id, "percentage": 100}],
            "annotations": {"workers/message": message},
        },
    )
    return bool(res.get("success"))


def newest_healthy_version() -> str:
    res = api("GET", f"/workers/scripts/{SCRIPT_NAME}/versions?limit=20")
    for v in (res.get("result") or {}).get("items", []):
        st = version_state(v["id"])
        if st["healthy"]:
            return v["id"]
    return ""


def one_shot_heal() -> int:
    try:
        active = active_version_id()
    except Exception as exc:  # noqa: BLE001
        print(f"::error::cannot read active version: {exc}")
        return 1
    st = version_state(active)
    if st["healthy"]:
        print(f"OK: active version {active[:8]} (v{st['number']}) healthy, bindings={st['bindings']}")
        return 0

    good = newest_healthy_version()
    if not good:
        print(f"::error::active version {active[:8]} unhealthy ({st}) and NO healthy version exists")
        return 1
    print(
        f"::warning::active version {active[:8]} (v{st['number']}) UNHEALTHY: "
        f"script_ok={st['script_ok']} bindings={st['bindings']}; healing -> {good[:8]}"
    )
    if pin(good, "auto-heal: active version lost D1 binding / script"):
        time.sleep(3)
        now = version_state(active_version_id())
        if now["healthy"]:
            print(f"HEALED: traffic pinned to {good[:8]}, active={now['bindings']}")
            return 0
        print(f"::error::heal pin did not stick, active still {now}")
        return 1
    print("::error::heal pin request failed")
    return 1


def watch(expect: str, seconds: int) -> int:
    """Pin traffic back to `expect` whenever a rogue version steals it."""
    est = version_state(expect)
    if not est["healthy"]:
        print(f"::error::expected version {expect[:8]} is not healthy: {est}")
        return 1
    print(f"watching {seconds}s; expected version {expect[:8]} (v{est['number']}) bindings={est['bindings']}")

    deadline = time.time() + seconds
    healed = False
    while time.time() < deadline:
        try:
            active = active_version_id()
        except Exception as exc:  # noqa: BLE001
            print(f"::warning::read active failed: {exc}; retrying")
            time.sleep(5)
            continue

        if active == expect:
            if healed:
                print(f"OK: traffic restored to expected version {expect[:8]} (v{est['number']})")
                return 0
            time.sleep(5)
            continue

        st = version_state(active)
        print(f"rogue version {active[:8]} (v{st['number']}) has traffic: script_ok={st['script_ok']} bindings={st['bindings']}")
        if st["healthy"]:
            # another legitimate deploy took over; treat as fine, stop watching
            print("::warning::active version is healthy but different from expected; leaving it")
            return 0
        if pin(expect, "auto-recover: rogue 0-binding version stole traffic (404 incident)"):
            healed = True
            time.sleep(5)
        else:
            print("::warning::re-pin failed; retrying")
            time.sleep(5)

    active = active_version_id()
    st = version_state(active)
    if active == expect or st["healthy"]:
        print(f"OK: final active version {active[:8]} healthy ({st['bindings']})")
        return 0
    print(f"::error::still unhealthy after {seconds}s: {active[:8]} {st}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", type=int, default=0, help="seconds to watch traffic")
    parser.add_argument("--expect", default="", help="version id traffic must stay on")
    args = parser.parse_args()

    if args.expect:
        return watch(args.expect, args.watch or 120)
    return one_shot_heal()


if __name__ == "__main__":
    sys.exit(main())
