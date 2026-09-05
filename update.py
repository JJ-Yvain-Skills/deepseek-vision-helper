#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek Vision Helper 更新执行器。

对比 GitHub 远程版本与本地安装，有新版则覆盖安装；**始终保留用户数据**
（config.json / vision_hook_state.json / *.log / results/）。

用法：
  python update.py                        # 默认更新 ~/.zcode/vision-hook 与 ~/.zcode/skills/deepseek-vision-helper
  python update.py --hook-dir <路径> --skill-dir <路径>
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO_URL = "https://github.com/JJ-Yvain-Skills/deepseek-vision-helper"
RAW_BASE = "https://raw.githubusercontent.com/JJ-Yvain-Skills/deepseek-vision-helper/main"

# 用户数据：更新时绝不触碰
KEEP = {"config.json", "vision_hook_state.json", "vision_hook.log", "results"}


def fetch_raw(path):
    url = "%s/%s" % (RAW_BASE, path)
    req = urllib.request.Request(url, headers={"User-Agent": "vision-helper-updater/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def remote_version():
    try:
        return fetch_raw("VERSION").decode("utf-8").strip()
    except Exception as e:
        print("无法连接 GitHub 获取版本: %s" % e)
        return None


def local_version(hook_dir):
    p = os.path.join(hook_dir, "VERSION")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return None


def git_clone():
    """git clone 到临时目录；返回仓库根路径；失败返回 None。"""
    tmp = tempfile.mkdtemp(prefix="vision-update-")
    try:
        subprocess.run(["git", "clone", "--depth", "1", REPO_URL, os.path.join(tmp, "repo")],
                       check=True, capture_output=True, timeout=120)
        return os.path.join(tmp, "repo")
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return None


def sync_dir(src, dst, keep):
    """把 src 目录内容同步到 dst；keep 集合中的顶层文件名跳过。返回更新清单。"""
    if not os.path.isdir(dst):
        os.makedirs(dst, exist_ok=True)
    updated = []
    for name in os.listdir(src):
        if name in keep:
            continue
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
            updated.append(name + "/")
        else:
            shutil.copy2(s, d)
            updated.append(name)
    return updated


def download_fallback(hook_dir, skill_dir):
    """git 不可用时按固定文件清单下载。"""
    files = [
        ("VERSION", os.path.join(hook_dir, "VERSION")),
        ("hook/vision_hook.py", os.path.join(hook_dir, "vision_hook.py")),
        ("hook/config.example.json", os.path.join(hook_dir, "config.example.json")),
        ("skills/deepseek-vision-helper/SKILL.md", os.path.join(skill_dir, "SKILL.md")),
    ]
    updated = []
    for src, dst in files:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(fetch_raw(src))
        updated.append(os.path.basename(dst))
    return updated


def backup_old(hook_dir, keep=3):
    """更新前把旧脚本与版本标记备份到 hook_dir/backup/<时间戳>/，保留最近 keep 份。"""
    root = os.path.join(hook_dir, "backup")
    try:
        os.makedirs(root, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        d = os.path.join(root, stamp)
        os.makedirs(d, exist_ok=True)
        for f in ("vision_hook.py", "VERSION"):
            src = os.path.join(hook_dir, f)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(d, f))
        # 清理旧备份
        dirs = sorted(x for x in os.listdir(root)
                      if os.path.isdir(os.path.join(root, x)))
        while len(dirs) > keep:
            shutil.rmtree(os.path.join(root, dirs.pop(0)))
        return d
    except Exception as e:
        print("备份失败(继续更新): %s" % e)
        return None


def main():
    ap = argparse.ArgumentParser(description="DeepSeek Vision Helper 更新执行器")
    ap.add_argument("--hook-dir", default=os.path.expanduser("~/.zcode/vision-hook"),
                    help="hook 脚本安装目录(默认 ~/.zcode/vision-hook)")
    ap.add_argument("--skill-dir", default=os.path.expanduser("~/.zcode/skills/deepseek-vision-helper"),
                    help="skill 安装目录(默认 ~/.zcode/skills/deepseek-vision-helper)")
    args = ap.parse_args()

    latest = remote_version()
    if latest is None:
        sys.exit(1)
    cur = local_version(args.hook_dir)
    if cur == latest:
        print("已是最新版本: %s" % cur)
        return

    print("检测到新版本: %s -> %s" % (cur or "未安装版本标记", latest))
    bak = backup_old(args.hook_dir)
    if bak:
        print("旧版本已备份: %s" % bak)
    repo = git_clone()
    if repo:
        print("使用 git 全量同步...")
        updated = []
        # VERSION 在仓库根，单独复制到 hook 安装目录
        shutil.copy2(os.path.join(repo, "VERSION"), os.path.join(args.hook_dir, "VERSION"))
        updated.append("VERSION")
        updated += sync_dir(os.path.join(repo, "hook"), args.hook_dir, KEEP)
        updated += sync_dir(os.path.join(repo, "skills", "deepseek-vision-helper"),
                            args.skill_dir, set())
        shutil.rmtree(os.path.dirname(repo), ignore_errors=True)
    else:
        print("git 不可用，按固定文件清单下载...")
        updated = download_fallback(args.hook_dir, args.skill_dir)

    print("✅ 更新完成: %s -> %s" % (cur or "?", latest))
    print("   更新文件: %s" % ", ".join(sorted(set(updated))))
    print("   用户数据(config.json / state / log / results)已保留")
    print("   提示: 重启 ZCode 客户端后生效")


if __name__ == "__main__":
    main()
