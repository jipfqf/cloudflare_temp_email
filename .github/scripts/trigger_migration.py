#!/usr/bin/env python3
"""触发 Cloudflare D1 增量数据库热迁移。
在后端 Worker 部署后执行，确保新表和新列自动创建。
"""
import os
import re
import sys
import time
import urllib.error
import urllib.request

WRANGLER_TOML = os.environ.get("WRANGLER_TOML", "wrangler.toml")

def main():
    if not os.path.exists(WRANGLER_TOML):
        print(f"info: {WRANGLER_TOML} not found, skip migration")
        return 0

    content = open(WRANGLER_TOML).read()
    m = re.search(r'ADMIN_PASSWORDS\s*=\s*\[\s*"([^"]+)"', content)
    if not m:
        print("info: no ADMIN_PASSWORDS found in config, skip migration")
        return 0

    admin_pwd = m.group(1)
    headers = {
        "x-admin-auth": admin_pwd,
        "Content-Type": "application/json",
        "User-Agent": "GitHub-Actions-Migration",
    }

    endpoints = [
        "https://codex-jts.920517.xyz/admin/db_migration",
        "https://mail-worker.jipfqf.workers.dev/admin/db_migration",
    ]

    time.sleep(3)
    success = False
    for url in endpoints:
        req = urllib.request.Request(url, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                print(f"migration OK ({url}): status {resp.status}")
                success = True
                break
        except urllib.error.HTTPError as e:
            # 200/400/401/404 等响应说明网络连通
            print(f"migration endpoint responded ({url}): HTTP {e.code}")
            if e.code in (200, 400):  # 400 往往是 "already migrated"
                success = True
                break
        except Exception as e:
            print(f"migration endpoint skipped ({url}): {e}")

    return 0 if success else 0  # 即使热迁移失败也不阻断部署流程

if __name__ == "__main__":
    sys.exit(main())
