#!/usr/bin/env python3
"""每天首次运行的自更新检查 — Smart Invest Skill。

机制（只 diff 一个版本文件，不做整库 diff）：
- 仓库根 `VERSION` 维护一个版本号，每次 push 代码就 +1（手动 bump）。
- 远端 `VERSION`（GitHub raw）与本地比对：不同即有更新。
- 有更新且本地是干净的 git clone → `git pull --ff-only` 拉取最新；
  本地有未提交改动（如开发仓）→ 跳过自动拉取，只提示。
- 每天只检查一次：`data/.update_check` 记录上次检查日期，幂等去重。

纯标准库（urllib + subprocess git），无第三方依赖。失败绝不抛异常、不阻塞主流程。
"""
import argparse
import json
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "VERSION"
MARKER = REPO_ROOT / "data" / ".update_check"
RAW_VERSION_URL = (
    "https://raw.githubusercontent.com/Tght1211/smart-invest/main/VERSION"
)
GIT_REMOTE = "origin"
GIT_BRANCH = "main"


def local_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def remote_version(timeout=8):
    """拉远端 VERSION（GitHub raw）。失败返回 ""，绝不抛异常。"""
    try:
        req = urllib.request.Request(
            RAW_VERSION_URL, headers={"User-Agent": "smart-invest-update/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception:
        return ""


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def due_today():
    """今天是否还没检查过更新（每天只查一次）。"""
    try:
        return MARKER.read_text(encoding="utf-8").strip() != _today()
    except Exception:
        return True  # 没有记录 → 该查


def mark_checked():
    try:
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(_today(), encoding="utf-8")
    except Exception:
        pass


def _git(*args):
    """跑一条 git 命令，返回 (ok, output)。"""
    try:
        r = subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def _is_clean_git():
    """是干净的 git 仓库（无未提交改动）才允许自动 pull。"""
    ok, _ = _git("rev-parse", "--is-inside-work-tree")
    if not ok:
        return False
    ok, out = _git("status", "--porcelain")
    return ok and out == ""


def check(apply=False, force=False):
    """检查（并可选拉取）更新。返回结果 dict，永不抛异常。

    apply=True 且有更新且本地干净 → git pull --ff-only。
    force=True 忽略「今天已查」的幂等。
    """
    result = {
        "checked": False, "local": local_version(), "remote": "",
        "update_available": False, "applied": False, "message": "",
    }
    if not force and not due_today():
        result["message"] = "今天已检查过更新，跳过。"
        return result
    result["checked"] = True
    rv = remote_version()
    result["remote"] = rv
    mark_checked()
    if not rv:
        result["message"] = "无法获取远端版本（可能离线），跳过。"
        return result
    if rv == result["local"]:
        result["message"] = f"已是最新版本 v{result['local']}。"
        return result

    result["update_available"] = True
    if not apply:
        result["message"] = (
            f"发现新版本 v{rv}（本地 v{result['local']}）。"
            f"运行 `python3 scripts/update_check.py --apply` 更新。")
        return result

    if not _is_clean_git():
        result["message"] = (
            f"发现新版本 v{rv}，但本地有未提交改动（或非 git 仓库），"
            f"未自动更新；请手动 `git pull`。")
        return result

    ok, out = _git("pull", "--ff-only", GIT_REMOTE, GIT_BRANCH)
    result["applied"] = ok
    new_local = local_version()
    if ok:
        result["local"] = new_local
        result["message"] = f"已更新到最新版本 v{new_local}。"
    else:
        result["message"] = f"自动更新失败（{out[:120]}），请手动 `git pull`。"
    return result


def cmd_main():
    ap = argparse.ArgumentParser(description="每日自更新检查（版本文件比对）")
    ap.add_argument("--apply", action="store_true",
                    help="有更新就 git pull --ff-only 拉取（默认只提示）")
    ap.add_argument("--force", action="store_true",
                    help="忽略「今天已查」的幂等，强制检查")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()
    res = check(apply=args.apply, force=args.force)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        tag = "🆕" if res["update_available"] else "✅"
        print(f"{tag} {res['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(cmd_main())
