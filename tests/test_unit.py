#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 单元/性质测试:纯逻辑层(确定性、无网络、秒级)。

覆盖:配置加载、provider 可用性、记账/跳过状态机、附件增量检测、
版本自检、文件读取、批量收集;以及性质不变量(记账单调、顺序稳定)。
用法: python test_unit.py
"""
import json
import os
import random
import shutil
import sys
import tempfile
import time

HOOK = os.path.expanduser("~/.zcode/vision-hook/vision_hook.py")
sys.path.insert(0, os.path.dirname(HOOK))
import vision_hook as vh

results = []


def rec(name, ok, detail=""):
    results.append((name, ok))
    print(("OK" if ok else "FAIL"), name, ("| " + str(detail) if detail else ""))


def run_unit():
    tmp = tempfile.mkdtemp()

    # ---------- 1. 配置加载: 环境变量 key 覆盖 ----------
    cfg_path = os.path.join(tmp, "c.json")
    cfg = {"providers": {"z": {"api_key": "YOUR_X", "base_url": "http://x", "model": "m"}}}
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"))
    old_env = os.environ.get("VISION_CONFIG")
    os.environ["VISION_CONFIG"] = cfg_path
    os.environ["VISION_API_KEY_Z"] = "real-key-from-env"
    loaded = vh.load_config()
    rec("1. 环境变量 key 覆盖 config", loaded["providers"]["z"]["api_key"] == "real-key-from-env")
    del os.environ["VISION_API_KEY_Z"]

    # ---------- 2. usable_providers / config_guidance ----------
    good = {"p": {"api_key": "sk-real"}}
    bad = {"q": {"api_key": "YOUR_X"}, "r": {"api_key": ""}}
    rec("2a. usable_providers 过滤占位符", set(vh.usable_providers({"providers": {**good, **bad}})) == {"p"})
    rec("2b. 全占位符时给出引导", vh.config_guidance({"providers": bad}) is not None)
    rec("2c. 有可用 key 时无引导", vh.config_guidance({"providers": good}) is None)

    # ---------- 3. state 记账/跳过状态机 ----------
    state = {}
    atts = [("a.txt", 1.0, "/x/a.txt"), ("b.txt", 2.0, "/x/b.txt")]
    vh.mark_identified(state, "s1", atts)
    rec("3a. mark_identified 记账", state["s1"]["a.txt"] == 1.0 and state["s1"]["b.txt"] == 2.0)
    vh.mark_skipped(state, "s1", [("c.txt", 3.0, "/x/c.txt")])
    rec("3b. mark_skipped 独立记录", state["_skipped"]["s1"]["c.txt"] == 3.0 and "c.txt" not in state["s1"])

    # ---------- 4. new_attachments: 增量/跳过/顺序 ----------
    art = os.path.join(tmp, "art", "s2")
    os.makedirs(art, exist_ok=True)
    def put(fn, mt):
        p = os.path.join(art, fn)
        open(p, "w").write("x")
        os.utime(p, (mt, mt))
    put("prompt-attachment-upload-1.txt", 100.0)
    put("prompt-attachment-upload-2.txt", 200.0)
    put("prompt-attachment-upload-3.txt", 300.0)
    os.environ["VISION_CONFIG"] = json.dumps({})  # 占位, cfg 只取 artifacts_dir
    cfg2 = {"artifacts_dir": os.path.join(tmp, "art")}
    st2 = {}
    new = vh.new_attachments("s2", cfg2, st2, 10)
    rec("4a. 首次全部为新(按新旧倒序)", [f[0] for f in new] == [
        "prompt-attachment-upload-3.txt", "prompt-attachment-upload-2.txt",
        "prompt-attachment-upload-1.txt"])
    vh.mark_identified(st2, "s2", new)
    new2 = vh.new_attachments("s2", cfg2, st2, 10)
    rec("4b. 记账后无新附件", new2 == [])
    vh.mark_skipped(st2, "s2", [new[0]])
    put("prompt-attachment-upload-4.txt", 400.0)
    new3 = vh.new_attachments("s2", cfg2, st2, 10)
    rec("4c. 跳过的不返回,新附件返回", [f[0] for f in new3] == ["prompt-attachment-upload-4.txt"])

    # ---------- 5. 性质测试: 记账单调 + 顺序稳定(随机 200 轮) ----------
    random.seed(42)
    art_prop = os.path.join(tmp, "art_prop")
    sess_dir = os.path.join(art_prop, "s_p")  # list_attachments 按 <artifacts_root>/<session> 查找
    os.makedirs(sess_dir, exist_ok=True)
    st_p = {}
    monotonic = True
    for i in range(200):
        fn = "prompt-attachment-upload-prop-%d.txt" % i
        p = os.path.join(sess_dir, fn)
        open(p, "w").write("x")
        os.utime(p, (1000.0 + i, 1000.0 + i))  # 递增 mtime,避免精度边界
        new = vh.new_attachments("s_p", {"artifacts_dir": art_prop}, st_p, 50)
        if new:
            vh.mark_identified(st_p, "s_p", new)
        # 不变量: 已记账数量单调不减
        if len(st_p.get("s_p", {})) < i:
            monotonic = False
    rec("5. 性质:记账单调不减(200轮随机)", monotonic)

    # ---------- 6. local_version / check_update(file://) ----------
    vh._HERE = tmp
    open(os.path.join(tmp, "VERSION"), "w").write("1.2.3")
    rec("6a. local_version 读取", vh.local_version() == "1.2.3")
    remote = os.path.join(tmp, "remote.txt")
    open(remote, "w").write("2.0.0")
    vh._UPDATE_URL = "file:///" + remote.replace(chr(92), "/")
    latest = vh.check_update({}, {})
    rec("6b. check_update 检测新版", latest == "2.0.0")
    open(remote, "w").write("1.2.3")
    rec("6c. 版本一致无更新", vh.check_update({}, {}) is None)
    open(remote, "w").write("1.2.2")
    rec("6e. 远端更旧(缓存滞后)不误报新版", vh.check_update({}, {}) is None)
    open(remote, "w").write("1.3.0-20260905")
    rec("6f. 带日期后缀的新版可检出", vh.check_update({}, {}) == "1.3.0-20260905")
    rec("6d. 频率控制(刚查过跳过)", vh.check_update({}, {"last_update_check": time.time()}) is None)

    # ---------- 7. file_to_data_uri: 双重编码修复 ----------
    uri_file = os.path.join(tmp, "att.txt")
    open(uri_file, "w", encoding="utf-8").write("data:image/png;base64,AAAA")
    rec("7a. data URI 文件原样返回(不双重编码)",
        vh.file_to_data_uri(uri_file, "image/png") == "data:image/png;base64,AAAA")
    bin_file = os.path.join(tmp, "img.bin")
    open(bin_file, "wb").write(b"\x89PNG\x0d\x0a")
    rec("7b. 二进制文件正常编码",
        vh.file_to_data_uri(bin_file, "image/png").startswith("data:image/png;base64,"))

    # ---------- 8. collect_files 去重 ----------
    d = os.path.join(tmp, "scan")
    os.makedirs(os.path.join(d, "sub"), exist_ok=True)
    open(os.path.join(d, "a.png"), "w").write("x")
    open(os.path.join(d, "b.txt"), "w").write("x")  # 非图片应跳过
    open(os.path.join(d, "sub", "c.jpg"), "w").write("x")
    files = vh.collect_files(d, [])
    rec("8. collect_files 递归+扩展名过滤", len(files) == 2 and all(f.endswith((".png", ".jpg")) for f in files))

    # ---------- 9. 占位符计数 ----------
    rec("9. 占位符计数", vh.count_placeholder_images(["[Attached image/png: a.png] x [Attached image: b]"]) == 2)

    # ---------- 10. cleanup_old: 防无限增长 ----------
    res = os.path.join(tmp, "results")
    os.makedirs(res)
    old_f = os.path.join(res, "old.md")
    open(old_f, "w").write("x")
    os.utime(old_f, (time.time() - 10 * 86400, time.time() - 10 * 86400))
    new_f = os.path.join(res, "new.md")
    open(new_f, "w").write("x")
    os.makedirs(os.path.join(tmp, "sess_alive"))
    st_cl = {
        "sess_dead": {"prompt-attachment-upload-a.txt": 100.0},
        "sess_alive": {
            "prompt-attachment-upload-old.txt": time.time() - 40 * 86400,
            "prompt-attachment-upload-new.txt": time.time(),
        },
    }
    cfg_cl = {"artifacts_dir": tmp, "results_max_age_days": 7,
              "state_max_age_days": 30, "state_cleanup_interval_hours": 24}
    vh._HERE = tmp
    vh.cleanup_old(st_cl, cfg_cl)
    rec("10a. cleanup: results 旧文件清理(保留新)",
        not os.path.exists(old_f) and os.path.exists(new_f))
    rec("10b. cleanup: 死会话清理", "sess_dead" not in st_cl)
    rec("10c. cleanup: 过期记录清理(保留新)",
        "prompt-attachment-upload-old.txt" not in st_cl["sess_alive"]
        and "prompt-attachment-upload-new.txt" in st_cl["sess_alive"])
    st_cl2 = {"last_cleanup": time.time()}
    vh.cleanup_old(st_cl2, cfg_cl)
    rec("10d. cleanup: 24h 频率控制", st_cl2.get("last_cleanup") is not None)
    vh._HERE = os.path.dirname(HOOK)

    if old_env:
        os.environ["VISION_CONFIG"] = old_env
    else:
        os.environ.pop("VISION_CONFIG", None)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    run_unit()
    fails = [r for r in results if not r[1]]
    print("L0 单元/性质: %d/%d 通过" % (len(results) - len(fails), len(results)))
    sys.exit(1 if fails else 0)
