#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZCode Vision Hook —— 给纯文本模型（DeepSeek 等）装上"眼睛"。

触发时机：UserPromptSubmit / PreToolUse（任一事件触发都会尝试取图并识别）。
流程：
  1. 从 stdin 读取 ZCode 注入的 JSON（session_id / transcript_path / prompt / tool_name ...）
  2. 取图（按优先级）：
     a. transcript 里的图片 part（file / Anthropic 内联 base64 / zcode-artifact:// URI）
     b. 【核心】提交级附件识别：UserPromptSubmit 时查会话 DB
        （~/.zcode/cli/db/db.sqlite）input_history.attachments——这是 ZCode
        本次实际提交的附件（zcode-artifact:// URI），写入时机早于 hook 触发约 1s。
        只识别提交记录里的附件；"贴了又删"的附件（磁盘文件残留、无提交记录）
        不再被误识别（2026-08-11 修复：旧实现扫描全部未记账文件导致旧图混入）。
     c. 权威信号缺失（DB 不可用）→ 降级"新鲜度门控"扫描：只识别 fresh_seconds
        内新落盘的 prompt-attachment-upload-*.txt，防陈年残留误识别。
     d. 续传通道：预算截断的附件（state._skipped）在后续事件（"继续"/PreToolUse）补识别。
  3. 按路由规则选择视觉后端，逐张识别（见"路由"）
  4. stdout 输出 {"additionalContext": "[Vision result] ..."} 注入对话上下文
  5. 至少一张识别成功后，把本次识别的附件记入 state（下次不再重复注入）

路由（config.json 可调）：
  - 所有图片统一走 provider（默认 agnes / agnes-2.5-flash）；批量不再切换后端，
    改为分批提交：每 batch_chunk_size 张一批、批间暂停 batch_chunk_pause 秒（串行节流，
    避开免费后端限流）
  - 单张失败由 call_vision 按 retry_delays 定时退避轮询重试兜底（默认 2s/5s/10s 三次；
    仅网络/超时/限流类错误重试，认证/额度类错误快速失败）
  - 可选 fallback_provider：配置了才启用（provider 失败后降级重试该图），默认不配置
  - 环境变量 VISION_PROVIDER=xxx 可强制只用某 provider（调试用）
  - 环境变量 VISION_CONFIG=/path/to/config.json 可指定配置文件（测试用）

安全：API key 只存放在 config.json（不要提交到仓库）。
行为：无图 / 全部识别失败时静默退出（无输出、exit 0），不影响正常对话。
调试：运行日志写入同目录 vision_hook.log。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOG = os.path.join(_HERE, "vision_hook.log")
_STATE_FILE = os.path.join(_HERE, "vision_hook_state.json")
_LOG_MAX_BYTES = 1024 * 1024  # 日志轮转阈值(默认 1MB,可由 config.log_max_bytes 覆盖)
_UPDATE_URL = os.environ.get(
    "VISION_UPDATE_URL",
    "https://raw.githubusercontent.com/JJ-Yvain-Skills/deepseek-vision-helper/main/VERSION")


def _rotate_log():
    """当前日志超过阈值时归档为 .1(覆盖旧备份),保留最近两段日志。"""
    try:
        if os.path.exists(_LOG):
            os.replace(_LOG, _LOG + ".1")
    except Exception:
        pass


def log(msg):
    try:
        if _LOG_MAX_BYTES and os.path.exists(_LOG) and os.path.getsize(_LOG) >= _LOG_MAX_BYTES:
            _rotate_log()
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


def load_config():
    path = os.environ.get("VISION_CONFIG") or os.path.join(_HERE, "config.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    # 环境变量覆盖 API key（优先级高于 config.json）：
    #   VISION_API_KEY_<PROVIDER 大写、连字符转下划线>，如 VISION_API_KEY_ZHIPU
    for name, p in cfg.get("providers", {}).items():
        env_key = os.environ.get("VISION_API_KEY_" + name.upper().replace("-", "_"))
        if env_key:
            p["api_key"] = env_key
    return cfg


def usable_providers(cfg):
    """返回配置了真实 key 的 provider（剔除 YOUR_ 占位符与空值）。"""
    return {n: p for n, p in cfg.get("providers", {}).items()
            if p.get("api_key") and not str(p["api_key"]).startswith("YOUR_")}


def config_guidance(cfg):
    """未配置任何可用 key 时返回引导文案（供日志/CLI 输出），否则返回 None。"""
    if usable_providers(cfg):
        return None
    return ("[vision helper] 未配置可用的 API key。两种配置方式："
            "① 设置环境变量 VISION_API_KEY_<PROVIDER>（如 VISION_API_KEY_ZHIPU）；"
            "② 编辑 %s 的 providers 填入真实 key（推荐 agnes：apihub.agnes-ai.com；国内用户可用智谱 bigmodel.cn 免费注册 GLM-4.6V-Flash）"
            % os.path.join(_HERE, "config.json"))


def parse_transcript(path):
    """把 transcript 解析为消息列表 [{'role','texts','images'}]，容忍多种序列化格式。"""
    messages = []
    if not path or not os.path.exists(path):
        return messages
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            msg = {"role": obj.get("role", "unknown"), "texts": [], "images": []}

            def scan(node):
                if isinstance(node, dict):
                    t = node.get("type")
                    if t == "file" and str(node.get("mime", "")).startswith("image/"):
                        msg["images"].append(node)
                    elif t == "image":  # Anthropic 风格内联图片
                        msg["images"].append(node)
                    elif t == "text" and node.get("text"):
                        msg["texts"].append(str(node["text"]))
                    else:
                        for v in node.values():
                            scan(v)
                elif isinstance(node, list):
                    for v in node:
                        scan(v)

            scan(obj)
            messages.append(msg)
    return messages


def read_uri_file(path, mime):
    """读取 artifacts 里的附件文件（内容是 data URI 或裸 base64），规整成 data URI。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read().strip()
    if content.startswith("data:"):
        return content
    if content.startswith("base64,"):
        return "data:%s;%s" % (mime, content)
    return "data:%s;base64,%s" % (mime, content)


def resolve_image(info, session_id, cfg):
    """把图片信息解析成 (mime, data_uri)；无法解析返回 None。"""
    mime = info.get("mime") or "image/png"
    url = info.get("url") or ""

    # 1) Anthropic 风格内联 base64 / data URI
    src = info.get("source")
    if isinstance(src, dict):
        if src.get("type") == "base64" and src.get("data"):
            return mime, "data:%s;base64,%s" % (mime, src["data"])
        d = src.get("data")
        if d and str(d).startswith("data:"):
            return mime, d

    # 2) 直接内联 data URI
    if str(url).startswith("data:"):
        return mime, url

    # 3) zcode-artifact:// URI → 磁盘 artifacts 目录
    if str(url).startswith("zcode-artifact://"):
        rest = url[len("zcode-artifact://"):]
        parts = rest.split("/", 1)
        sess = parts[0] or session_id
        name = parts[1] if len(parts) > 1 else ""
        base = os.path.expanduser(cfg.get("artifacts_dir", "~/.zcode/cli/artifacts"))
        d = os.path.join(base, sess)
        if not name or not os.path.isdir(d):
            return None
        # 优先：文件名包含 URI 尾部（实测格式 prompt-attachment-upload-*-<tail>.txt）
        for fn in os.listdir(d):
            if fn.endswith(".txt") and name in fn:
                return mime, read_uri_file(os.path.join(d, fn), mime)
        # 兜底：该会话最新的粘贴附件
        cands = [fn for fn in os.listdir(d)
                 if fn.startswith("prompt-attachment-upload") and fn.endswith(".txt")]
        if cands:
            newest = max(cands, key=lambda fn: os.path.getmtime(os.path.join(d, fn)))
            return mime, read_uri_file(os.path.join(d, newest), mime)
    return None


# ---------- state 增量附件监控（贴图识别的核心通道） ----------
# ZCode 的 hook transcript 只含纯文本（UserPromptSubmit 仅 prompt、PreToolUse 为空），
# 图片 part 永远不会出现在 transcript 里；粘贴的图片附件会落盘到
# ~/.zcode/cli/artifacts/<会话>/prompt-attachment-upload-*.txt。
# 因此：记录"已识别附件"状态，每次 hook 只识别新落盘的附件，可靠且天然去重。


def load_state():
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
            return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_state(state):
    try:
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, _STATE_FILE)  # 原子替换，避免半写状态
    except Exception:
        pass


def list_attachments(session_id, cfg):
    """列出该会话 artifacts 目录下全部粘贴附件，返回 [(filename, mtime, abspath)]（按新旧升序）。"""
    base = os.path.expanduser(cfg.get("artifacts_dir", "~/.zcode/cli/artifacts"))
    d = os.path.join(base, session_id or "")
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        if fn.startswith("prompt-attachment-upload") and fn.endswith(".txt"):
            p = os.path.join(d, fn)
            try:
                out.append((fn, os.path.getmtime(p), p))
            except OSError:
                continue
    out.sort(key=lambda x: x[1])
    return out


def new_attachments(session_id, cfg, state, max_n):
    """返回 state 未记录（或 mtime 已变化）的附件 [(fn, mtime, path)]，按新旧倒序，最多 max_n。

    仅用于"权威信号缺失"时的降级扫描（配合 fresh_seconds 门控）；
    被 _skipped 标记的附件（预算截断）不在此列，走 skipped_attachments 续传。
    """
    files = list_attachments(session_id, cfg)
    known = state.get(session_id, {}) if isinstance(state, dict) else {}
    skipped = (state.get("_skipped", {}).get(session_id, {})
               if isinstance(state, dict) else {})
    new = [(fn, mt, p) for fn, mt, p in files
           if known.get(fn) != mt and skipped.get(fn) != mt]
    new.sort(key=lambda x: x[1], reverse=True)
    return new[:max_n]


def mark_skipped(state, session_id, attachments):
    """把因单次上限被截断的附件标记为"已跳过"（filename -> mtime）。

    不混入下次注入，也不占用"已识别"名额；用户重发该图（新文件名）仍可识别。
    """
    skipped = state.setdefault("_skipped", {}).setdefault(session_id, {})
    for fn, mt, _ in attachments:
        skipped[fn] = mt


def cleanup_old(state, cfg):
    """低频清理，防止 state 记账与 results 落盘无限增长。

    - results/ 中超过 results_max_age_days（默认 7 天）的文件删除
    - state 中：会话目录已不存在的整会话记录删除；附件 mtime 超过
      state_max_age_days（默认 30 天）的单条记录删除（防重复语义早已无意义）
    - 每 state_cleanup_interval_hours（默认 24 小时）执行一次（state 记 last_cleanup）
    """
    interval = float(cfg.get("state_cleanup_interval_hours", 24))
    now = time.time()
    if now - float(state.get("last_cleanup", 0)) < interval * 3600:
        return
    state["last_cleanup"] = now

    # 1) results 旧文件清理
    res_dir = os.path.join(_HERE, "results")
    max_days = float(cfg.get("results_max_age_days", 7))
    if os.path.isdir(res_dir):
        for fn in os.listdir(res_dir):
            p = os.path.join(res_dir, fn)
            try:
                if now - os.path.getmtime(p) > max_days * 86400:
                    os.remove(p)
            except OSError:
                pass

    # 2) state 过期记录清理
    state_days = float(cfg.get("state_max_age_days", 30))
    base = os.path.expanduser(cfg.get("artifacts_dir", "~/.zcode/cli/artifacts"))
    for sess, recs in list(state.items()):
        if sess in ("_skipped", "_results_path", "last_update_check",
                    "last_notified_version", "last_cleanup"):
            continue
        if not isinstance(recs, dict):
            continue
        if not os.path.isdir(os.path.join(base, sess)):
            del state[sess]  # 会话目录已不存在 → 整会话记录删除
            continue
        stale = [fn for fn, mt in recs.items()
                 if isinstance(mt, (int, float)) and now - mt > state_days * 86400]
        for fn in stale:
            del recs[fn]


def mark_identified(state, session_id, attachments):
    """把已成功识别的附件记入 state（filename -> mtime），并移出跳过表。"""
    known = state.get(session_id, {})
    skipped = state.get("_skipped", {}).get(session_id, {})
    for fn, mt, _ in attachments:
        known[fn] = mt
        if skipped.get(fn) == mt:
            del skipped[fn]
    state[session_id] = known


# ---------- 提交级附件识别（权威信号） ----------
# 平台事实（ZCode 3.7.x 源码级验证）：
#   1) 附件在粘贴时即物化写盘（writePromptAttachment → artifacts/<会话>/
#      prompt-attachment-upload-*.txt），文件名每次粘贴唯一；UI 移除附件
#      （removeAttachment）只撤销 objectURL + 取消传输，没有任何删文件 RPC
#      → "贴了又删"的附件文件永久残留磁盘；
#   2) hook 的 UserPromptSubmit transcript 只含 prompt 纯文本（CLI 端
#      F2n("user", e.prompt) 构造），不含任何附件信号；
#   3) 唯一权威信号：~/.zcode/cli/db/db.sqlite 的 input_history.attachments，
#      记录本次实际提交的附件（zcode-artifact:// URI 或本地路径），实测写入
#      时机早于 UserPromptSubmit hook 触发约 1s。
# 因此：以 input_history 为"本次提交了什么"的唯一依据；只识别提交记录里的附件，
# 磁盘上残留但从未提交的文件（删图/换图）不再被误识别。


def db_path_from_cfg(cfg):
    """会话 DB 路径：优先 config.session_db_path，否则由 artifacts_dir 推导。

    artifacts_dir 默认 ~/.zcode/cli/artifacts → 推导 ~/.zcode/cli/db/db.sqlite。
    """
    p = cfg.get("session_db_path")
    if p:
        return os.path.expanduser(p)
    art = os.path.expanduser(cfg.get("artifacts_dir", "~/.zcode/cli/artifacts"))
    return os.path.join(os.path.dirname(art), "db", "db.sqlite")


def submitted_attachments(session_id, cfg):
    """从会话 DB 的 input_history 读取最近一次用户提交的附件（权威信号）。

    返回 [(path, content, mime)]（仅图片附件）；以下情形返回 None（无法判定）：
      - DB 文件不存在 / 打开失败 / 查询失败
      - 该会话没有 input_history 行
    返回 [] 表示"有行但本次提交无附件"（权威：本次无需识别任何附件）。
    """
    import sqlite3
    db = db_path_from_cfg(cfg)
    if not os.path.isfile(db):
        log("session db not found: %s" % db)
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
        cur = con.cursor()
        row = cur.execute(
            "SELECT attachments FROM input_history WHERE session_id=? AND kind='prompt' "
            "ORDER BY time_created DESC, id DESC LIMIT 1", (session_id,)).fetchone()
        con.close()
    except Exception as e:
        log("session db query failed: %s" % e)
        return None
    if row is None:
        log("no input_history row for session")
        return None
    att = (row[0] or "").strip()
    if not att:
        return []  # 权威：本次提交无附件
    try:
        arr = json.loads(att)
    except Exception:
        log("input_history attachments parse failed")
        return None
    out = []
    for a in arr if isinstance(arr, list) else []:
        if not isinstance(a, dict):
            continue
        # ZCode 的附件 JSON 无 mime 字段(实测), 图片用 type=="image" 判定
        mime = a.get("mime") or a.get("mimeType") or ""
        if str(a.get("type") or "").lower() == "image" or str(mime).startswith("image/"):
            out.append((str(a.get("path") or ""), str(a.get("content") or ""),
                        str(mime) or "image/png"))
    return out


def resolve_submitted_attachment(session_id, cfg, path, content, mime):
    """把 input_history 附件精确解析为 (mime, data_uri, (fn, mtime, abspath))。

    只做精确匹配（artifactId 在文件名中 / 本地文件直读），不做"最新附件"兜底
    ——兜底正是旧图误识别的来源之一；无法解析返回 None。
    """
    if content.startswith("zcode-artifact://"):
        rest = content[len("zcode-artifact://"):]
        parts = rest.split("/", 1)
        name = parts[1] if len(parts) > 1 else ""
        if name:
            base = os.path.expanduser(cfg.get("artifacts_dir", "~/.zcode/cli/artifacts"))
            d = os.path.join(base, session_id)
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    if (fn.startswith("prompt-attachment-upload") and fn.endswith(".txt")
                            and name in fn):
                        p = os.path.join(d, fn)
                        try:
                            mt = os.path.getmtime(p)
                        except OSError:
                            continue
                        return mime, read_uri_file(p, mime), (fn, mt, p)
            log("submitted attachment artifact not found: %s" % content)
        return None
    if path and os.path.isfile(path):  # 本地文件附件直读
        return mime, file_to_data_uri(path, mime), (os.path.basename(path),
                                                    os.path.getmtime(path), path)
    return None


def skipped_attachments(session_id, cfg, state):
    """返回 state._skipped 中仍存在于磁盘的附件 [(fn, mtime, abspath)]（按新旧倒序）。

    续传通道：预算截断的附件在此标记，由"继续"或后续事件补识别。
    """
    files = list_attachments(session_id, cfg)
    skipped = (state.get("_skipped", {}).get(session_id, {})
               if isinstance(state, dict) else {})
    out = [(fn, mt, p) for fn, mt, p in files if skipped.get(fn) == mt]
    out.sort(key=lambda x: x[1], reverse=True)
    return out


# ---------- 版本自检（agent-reach 模式：检查自动、更新需确认） ----------
# 仅在"识别成功注入"时顺带检查（有图场景用户在场），频率受
# update_check_interval_hours 控制（默认 24h）；有新版时在注入末尾附一行提示，
# 同版本不重复提醒；网络失败静默。更新由用户一句话触发（update.py）。


def local_version():
    """读取本地 VERSION 文件（数据目录优先，其次脚本目录）；缺失返回 None。"""
    for base in (os.environ.get("ZCODE_PLUGIN_DATA") or "", _HERE):
        p = os.path.join(base, "VERSION")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    v = f.read().strip()
                return v or None
            except Exception:
                return None
    return None


def _ver_tuple(v):
    """'0.1.12-20260905' → (0, 1, 12)；非数字段截断，仅用于新旧比较。"""
    parts = []
    for seg in str(v).split("-")[0].split("."):
        if seg.isdigit():
            parts.append(int(seg))
        else:
            break
    return tuple(parts)


def check_update(cfg, state):
    """对比远程 VERSION；仅当远程版本更新时返回新版本号，否则 None。

    只判"不等"会被 CDN 缓存滞后坑到（仓库迁移/刚推送时旧地址可能返回更旧的版本，
    误触发"检测到新版本"并诱发自动更新装回旧代码），故比较新旧而非不等。
    """
    cur = local_version()
    if not cur:
        return None  # 本地无版本标记 → 跳过（不打扰）
    interval = float(cfg.get("update_check_interval_hours", 24))
    last = state.get("last_update_check", 0)
    if time.time() - last < interval * 3600:
        return None  # 频率控制：interval 小时内只查一次
    state["last_update_check"] = time.time()  # 先记账：失败也不在 interval 内重试
    try:
        req = urllib.request.Request(_UPDATE_URL,
                                     headers={"User-Agent": "zcode-vision-hook/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            latest = resp.read().decode("utf-8").strip()
    except Exception:
        return None  # 网络失败静默
    if not latest or latest == cur:
        return None
    return latest if _ver_tuple(latest) > _ver_tuple(cur) else None


_PLACEHOLDER_RE = None  # 延迟初始化（避免 import 顺序问题）


def count_placeholder_images(texts):
    """统计最新用户消息文本里的图片占位符标记（如 [Attached image/png: image.png]）。

    当图片因主模型不支持图片输入而被裁剪成纯文本时，transcript 里只剩这种标记，
    但实际附件仍会落盘到 artifacts 目录——靠它触发兜底取图（保证"发图必识别"）。
    """
    global _PLACEHOLDER_RE
    if _PLACEHOLDER_RE is None:
        import re
        _PLACEHOLDER_RE = re.compile(r"\[Attached image[^\]]*\]", re.IGNORECASE)
    n = 0
    for t in texts:
        n += len(_PLACEHOLDER_RE.findall(t or ""))
    return n


# 识别角色约束：把视觉后端钉死在"纯识别工具"上，
# 只输出图片中的事实，禁止模型自己的分析/建议/总结/反问/常识补充。
_SYSTEM_PROMPT = (
    "你是一个图片识别工具。只输出图片中客观存在的内容：完整提取文字、"
    "描述界面元素/图表数据/场景。严禁添加你的分析、总结、建议、评价、猜测或反问；"
    "严禁输出图片之外的知识；如果图片中没有的信息，不要提及。"
)


def call_vision(cfg, data_uri, question, provider):
    """调用 OpenAI 兼容 /chat/completions，返回识别文本；失败返回 None。

    兜底重试：网络波动/超时/连接重置/限流(408/429/5xx/1302/1305)按 retry_delays
    定时退避轮询重试（默认 [2,5,10] 即最多 1+3 次尝试）；认证/额度类错误
    (401/402/403 等其余 4xx)重试无意义，立即失败。
    """
    providers = cfg.get("providers", {})
    if provider not in providers:
        log("provider missing in config: %s" % provider)
        return None
    p = providers[provider]
    payload = {
        "model": p["model"],
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": question or cfg.get(
                    "default_question",
                    "请识别这张图片：如果包含文字请完整提取；如果是界面、报错页面或图表，请说明其内容与含义。只输出图片中的事实，不要分析、建议或反问。")},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]},
        ],
        "max_tokens": cfg.get("max_tokens", 4000),
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "zcode-vision-hook/1.0",
    }
    headers[p.get("header", "Authorization")] = p.get("auth_prefix", "") + p["api_key"]
    body = json.dumps(payload).encode("utf-8")
    delays = cfg.get("retry_delays", [2, 5, 10])
    last_err = "unknown"
    for attempt in range(1 + len(delays)):
        retryable = False
        try:
            req = urllib.request.Request(p["base_url"], data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=cfg.get("timeout_seconds", 90)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return str(data["choices"][0]["message"]["content"]).strip()
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s: %s" % (e.code, e.read(300).decode("utf-8", "replace"))
            retryable = (e.code in (408, 429, 500, 502, 503, 504)
                         or "1302" in last_err or "1305" in last_err)
        except Exception as e:
            # 网络波动/超时/连接重置等传输层错误：agnes 后端本身稳定，多数重试即可恢复
            last_err = "%s: %s" % (type(e).__name__, str(e)[:300])
            retryable = True
        if not retryable or attempt >= len(delays):
            break
        log("vision api retry (%s) in %ds [%d/%d]: %s" % (
            provider, delays[attempt], attempt + 1, len(delays), last_err))
        time.sleep(delays[attempt])
    log("vision api failed (%s): %s" % (provider, last_err))
    return None


def build_chain(cfg):
    """返回 provider 识别链（保序去重）：常规 provider，可选降级 fallback_provider。

    批量不再换后端（统一走本链，节奏由分批节流 + 定时重试控制）；
    fallback_provider 仅在配置了且与 provider 不同才生效。
    环境变量 VISION_PROVIDER=xxx 强制只用某 provider（调试用）。
    """
    forced = os.environ.get("VISION_PROVIDER")
    if forced:
        return [forced]
    chain = [cfg.get("provider", "agnes")]
    fb = cfg.get("fallback_provider")
    if fb:
        chain.append(fb)
    return list(dict.fromkeys(p for p in chain if p))


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except Exception:
        return
    if not isinstance(payload, dict):
        return  # 畸形 payload(数组/字符串等)静默退出,不崩溃
    session_id = payload.get("session_id", "")
    log("hook fired: event=%s session=%s" % (payload.get("hook_event_name"), session_id))

    cfg = load_config()
    global _LOG_MAX_BYTES
    _LOG_MAX_BYTES = cfg.get("log_max_bytes", 1024 * 1024)
    state = load_state()
    cleanup_old(state, cfg)  # 低频清理:过期 state 记录 + 旧 results 文件
    msgs = parse_transcript(payload.get("transcript_path"))
    log("transcript=%s msgs=%d" % (payload.get("transcript_path"), len(msgs)))

    # 1) 收集本消息内的全部图片
    img_infos = []
    if msgs and msgs[-1].get("images"):
        img_infos = msgs[-1]["images"]

    # 2) 逐张解析成 data URI（单张解析失败不影响其他张；其他客户端的 transcript 内联图）
    resolved = []
    for info in img_infos:
        r = resolve_image(info, session_id, cfg)
        if r:
            resolved.append(r)
        else:
            log("image resolve failed for one part")

    # 3) 【核心】提交级附件识别（权威信号 = 会话 DB input_history.attachments）
    #    ZCode 的 transcript 拿不到图（UserPromptSubmit 仅纯文本 prompt、
    #    PreToolUse 为空）；input_history 记录本次实际提交的附件 URI——
    #    输入框里"贴了又删"的附件不会出现在任何提交记录里，因此不再被误识别
    #    （旧实现扫描全部未记账文件，是"旧图混入"bug 的根因）。
    #    - UserPromptSubmit：权威信号 → 精确识别本次提交的图；
    #      信号缺失（DB 不可用）→ 降级"新鲜度门控"扫描（防陈年残留）。
    #    - PreToolUse：无提交上下文，只做续传，不扫描新附件（防"贴了还没发"
    #      的图被提前识别——旧图混入的另一条路径）。
    identified = []  # 本次识别成功的附件（供 state 记账去重）
    event = payload.get("hook_event_name", "")
    if event == "UserPromptSubmit" and not resolved:
        subs = submitted_attachments(session_id, cfg)
        if subs is None:
            log("authoritative signal unavailable, fallback to fresh scan")
            fresh = float(cfg.get("fresh_seconds", 300))
            now = time.time()
            atts = [(fn, mt, p) for fn, mt, p in new_attachments(session_id, cfg, state, 50)
                    if now - mt < fresh]
            if atts:
                log("fallback fresh attachment(s) detected: %d" % len(atts))
                resolved = [("image/png", read_uri_file(p, "image/png")) for _, _, p in atts]
                identified = atts
        elif subs == []:
            log("no attachments in this submit")
        else:
            known = state.get(session_id, {}) if isinstance(state, dict) else {}
            for path, content, mime in subs:
                r = resolve_submitted_attachment(session_id, cfg, path, content, mime)
                if not r:
                    log("submitted attachment unresolvable")
                    continue
                _mime, _uri, rec = r
                if known.get(rec[0]) == rec[1]:
                    continue  # 已识别过(同图重发/编辑重提), 不重复注入
                resolved.append((_mime, _uri))
                identified.append(rec)
            if identified:
                log("submitted attachment(s) resolved: %d" % len(identified))

    # 4) 续传通道：预算截断的附件（已标记 _skipped）在后续事件补识别
    if not resolved:
        cont = skipped_attachments(session_id, cfg, state)
        if cont:
            log("continuation: %d skipped attachment(s)" % len(cont))
            resolved = [("image/png", read_uri_file(p, "image/png")) for _, _, p in cont]
            identified = cont
    if not resolved:
        log("no image found")
        return

    # 记账与前置检查：本轮不会真正识别时（未配置 key / skip 多模态），
    # 已取到的附件全部记账（防混入）；识别路径则逐张记账（成功即记）。
    guidance = config_guidance(cfg)
    if guidance:
        if identified:
            mark_identified(state, session_id, identified)
            save_state(state)
        log(guidance)
        return

    if cfg.get("skip_when_multimodal") or os.environ.get("VISION_SKIP_MULTIMODAL") == "1":
        if identified:
            mark_identified(state, session_id, identified)
            save_state(state)
        log("skip: multimodal model configured, %d image(s) left to native channel" % len(resolved))
        return

    # 识别队列：不过滤、不截断、不因大小跳过——用户给多少识别多少；
    # 超大图仍尽力发送，失败时在结果中明确标注原因（可压缩后重发）。
    kept = list(resolved)
    big = sum(1 for mime, data_uri in resolved
              if len(data_uri) * 3 // 4 > cfg.get("max_image_bytes", 10 * 1024 * 1024))
    total_in = len(kept)

    # 时间预算：每轮 hook 在此预算内串行识别；预算用完停止，未识别的图
    # 不记账，由用户回复"继续"触发下一轮续传（已识别的逐张记账，绝不重复）。
    budget = float(cfg.get("recognition_time_budget", 240))
    deadline = time.time() + budget

    # 5) 路由：统一走 provider 链（默认仅 agnes）。批量不换后端，改为分批提交：
    #    每 batch_chunk_size 张一批、批间暂停 batch_chunk_pause 秒（串行节流避开
    #    免费后端限流）；单张失败由 call_vision 定时退避重试兜底。
    chain = build_chain(cfg)
    log("routing: %d image(s), chain=%s" % (len(kept), chain))

    # 纯识别指令：不把用户问题传给视觉模型（用户问题中的"如何解决/建议"类
    # 引导会诱发视觉模型输出建议，违背"只交接信息"原则）——视觉请求只用
    # 固定识别指令，建议通道物理关闭；一切判断由主模型基于注入的事实完成。
    recog_q = cfg.get(
        "default_question",
        "请识别这张图片：如果包含文字请完整提取；如果是界面、报错页面或图表，"
        "请说明其内容与含义。只输出图片中的事实，不要分析、建议或反问。")
    total_cap = cfg.get("total_max_chars", 8000)
    # 预算均分：多图时每张分到 total_cap/张数 的注入空间（下限 200 字符），
    # 避免前几张图吃光预算、后面的图被整体截没。
    per_cap = min(cfg.get("per_image_max_chars", 2000),
                  max(200, total_cap // max(len(kept), 1)))

    full_parts = []    # 每张图的完整识别文本（不截断，用于落盘/全量注入）
    inject_parts = []  # 注入用的截断版
    used = set()
    multi = len(kept) > 1
    processed = 0      # 已尝试处理的张数（含失败）
    done = 0           # 识别成功的张数
    chunk_size = max(1, int(cfg.get("batch_chunk_size", 3)))
    chunk_pause = float(cfg.get("batch_chunk_pause", 2))
    for i, (mime, data_uri) in enumerate(kept, 1):
        if time.time() > deadline:
            break  # 时间预算用完：剩余图不记账，下次"继续"续传
        if i > 1 and (i - 1) % chunk_size == 0:
            log("chunk pause: %ds after %d image(s)" % (int(chunk_pause), i - 1))
            time.sleep(chunk_pause)  # 分批节流：每完成一批歇一下再提交下一批
        q = ("图%d。%s" % (i, recog_q)) if multi else recog_q
        ok = False
        for prov in chain:
            desc = call_vision(cfg, data_uri, q, prov)
            if desc:
                used.add(prov)
                full_parts.append(("图%d: " % i) + desc if multi else desc)
                d = desc[:per_cap]
                if len(desc) > per_cap:
                    d += "…(截断)"
                inject_parts.append(("图%d: " % i) + d if multi else d)
                ok = True
                done += 1
                break
        processed = i
        if ok:
            # 逐张记账：识别成功立即标记，续传不会重复识别
            if identified and i - 1 < len(identified):
                mark_identified(state, session_id, [identified[i - 1]])
                save_state(state)
        else:
            full_parts.append("图%d: (识别失败)" % i)
            inject_parts.append("图%d: (识别失败)" % i)
            log("all providers failed for image %d" % i)

    remaining = total_in - processed

    # 预算截断：未处理的附件标记 _skipped（不混入未提交文件），
    # 供"继续"/后续 PreToolUse 续传补识别。
    if remaining > 0 and identified and processed < len(identified):
        mark_skipped(state, session_id, identified[processed:])
        save_state(state)

    # 完整优先注入：识别结果全程完整保留（full_text 不截断）。
    # 未超 total_cap → 全量注入；超限 → 完整结果落盘 results/ 目录，
    # 注入截断版并给出文件路径，需要全量时可读取——识别从不丢信息。
    full_text = "\n".join(full_parts)
    if big:
        full_text += "\n（%d 张图片超过大小限制，已尽力识别；个别失败请压缩后重发）" % big
    if len(full_text) <= total_cap:
        result = full_text
    else:
        result = "\n".join(inject_parts)
        result = result[:total_cap]
        # 完整结果落盘:同一会话复用同一文件(后续轮次追加,路径恒定),
        # 避免续传多轮生成一堆文件、模型/用户不知道读哪个。
        try:
            res_dir = os.path.join(_HERE, "results")
            os.makedirs(res_dir, exist_ok=True)
            path = state.get("_results_path", {}).get(session_id or "")
            if not path or not os.path.exists(path):
                path = os.path.join(res_dir, "%s_%s.md" % (session_id or "session",
                                                           time.strftime("%Y%m%d_%H%M%S")))
                state.setdefault("_results_path", {})[session_id or ""] = path
                save_state(state)
                mode = "w"
            else:
                mode = "a"
            with open(path, mode, encoding="utf-8") as f:
                f.write(("\n\n" if mode == "a" else "") + full_text)
            result += ("\n（识别已完成，但内容较长，完整结果已存至 %s，"
                       "需要完整内容时可读取该文件）" % path)
        except Exception as e:
            log("save full result failed: %s" % e)

    if not inject_parts:
        # 有图但一张都没处理（理论极小概率）或全部失败：告知用户，不静默
        result += ("\n（本次 %d 张图片均未能识别：请检查 API key / 网络，"
                   "下次触发将自动重试）" % total_in)
        log("no vision result at all")
    elif remaining > 0:
        result += ("\n（识别进度：已识别 %d/%d 张，还有 %d 张将自动继续识别）" % (
            done, total_in, remaining))
    elif inject_parts and all(p.endswith("(识别失败)") for p in inject_parts):
        # 全部失败：明确告知，下次触发自动重试
        result += ("\n（本次 %d 张图片均未能识别：请检查 API key / 网络，"
                   "下次触发将自动重试）" % total_in)
    elif big:
        result += "\n（%d 张图片超过大小限制，已尽力识别）" % big

    # 版本自检：有新版 → 注入末尾附提示（同版本不重复提醒；失败/无本地版本静默）
    try:
        latest = check_update(cfg, state)
        if latest and state.get("last_notified_version") != latest:
            state["last_notified_version"] = latest
            result += "\n（已检测到新版本 %s（当前 %s），将自动更新）" % (
                latest, local_version())
            log("update available: %s -> %s" % (local_version(), latest))
            save_state(state)
    except Exception:
        pass

    log("vision ok: %d image(s) processed, %d done, %d remaining, providers=%s" % (
        processed, done, remaining, ",".join(sorted(used))))
    # 注入带时间戳轮次标识：对话中多次贴图时，主模型可凭 [Vision result @HH:MM:SS]
    # 区分各轮识别结果，以最新时间戳为准。
    print(json.dumps({"additionalContext": "[Vision result @%s] " % time.strftime("%H:%M:%S") + result},
                     ensure_ascii=False))


def collect_files(folder, files):
    """收集待识别图片文件（目录递归 + 显式文件列表，去重保序）。"""
    exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
    result = []
    if folder:
        for root, _, names in os.walk(folder):
            for n in sorted(names):
                if os.path.splitext(n)[1].lower() in exts:
                    result.append(os.path.join(root, n))
    if files:
        result.extend(os.path.abspath(f) for f in files if os.path.isfile(f))
    seen, out = set(), []
    for f in result:
        rp = os.path.realpath(f)
        if rp not in seen:
            seen.add(rp)
            out.append(f)
    return out


def mime_for(path):
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp"}.get(
        os.path.splitext(path)[1].lower().lstrip("."), "image/png")


def file_to_data_uri(path, mime):
    import base64
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8").strip()
        if text.startswith("data:"):
            return text  # 文件内容已是 data URI（如 artifacts 附件），直接使用，避免双重编码
    except Exception:
        pass
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


def run_folder_mode(cfg, files, question, out_path, chain, per_cap, plain=False):
    """批量识别：逐张调用（串行），结果写文件或打印。返回 None（写文件）或文本。

    plain=True（单文件、无 --out）时输出纯描述文本，便于模型直接读取（主动调用模式）。
    """
    lines, ok_count = [], 0
    chunk_size = max(1, int(cfg.get("batch_chunk_size", 3)))
    chunk_pause = float(cfg.get("batch_chunk_pause", 2))
    for i, path in enumerate(files, 1):
        if i > 1 and (i - 1) % chunk_size == 0:
            log("chunk pause: %ds after %d image(s)" % (int(chunk_pause), i - 1))
            time.sleep(chunk_pause)  # 分批节流：每完成一批歇一下再提交下一批
        name = os.path.basename(path)
        mime = mime_for(path)
        try:
            data_uri = file_to_data_uri(path, mime)
        except Exception as e:
            lines.append("%d. **%s** — (读取失败: %s)" % (i, name, e))
            continue
        if len(data_uri) * 3 // 4 > cfg.get("max_image_bytes", 10 * 1024 * 1024):
            lines.append("%d. **%s** — (超出大小限制)" % (i, name))
            continue
        desc = None
        for prov in chain:
            desc = call_vision(cfg, data_uri, question, prov)
            if desc:
                break
        if desc:
            ok_count += 1
            if plain:
                lines.append(desc[:per_cap])
            else:
                lines.append("%d. **%s** — %s" % (i, name, desc[:per_cap]))
        else:
            lines.append("%d. **%s** — (识别失败)" % (i, name))
        log("folder mode: %d/%d done (ok=%d)" % (i, len(files), ok_count))
    text = "\n".join(lines)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("# 批量识图结果（共 %d 张，成功 %d 张）\n\n%s\n" % (len(files), ok_count, text))
        log("folder mode done: %d files -> %s (ok %d)" % (len(files), out_path, ok_count))
        return None
    return text


def main_cli(argv):
    import argparse
    ap = argparse.ArgumentParser(
        description="DeepSeek Vision Helper 批量识图（路由与 hook 一致）。"
                    "支持模型主动调用：python vision_hook.py --files <图片> --question <问题>")
    ap.add_argument("--folder", help="扫描并识别目录下所有图片（递归）")
    ap.add_argument("--files", nargs="*", help="指定图片文件列表")
    ap.add_argument("--out", help="结果写入文件（推荐；不写则打印到 stdout）")
    ap.add_argument("--provider", help="强制使用某 provider（如 agnes/zhipu）")
    ap.add_argument("--max", type=int, default=0, help="最多识别张数（0=不限）")
    ap.add_argument("--question", default=None, help="识别问题（默认用 config 的 default_question）")
    args = ap.parse_args(argv)
    if not args.folder and not args.files:
        ap.error("需要 --folder 或 --files")
    cfg = load_config()
    global _LOG_MAX_BYTES
    _LOG_MAX_BYTES = cfg.get("log_max_bytes", 1024 * 1024)
    guidance = config_guidance(cfg)
    if guidance:
        print(guidance)
        sys.exit(1)
    files = collect_files(args.folder, args.files)
    if args.max > 0:
        files = files[:args.max]
    if not files:
        print("未找到图片文件")
        return
    # 路由与 hook 一致：统一走 provider 链（批量不换后端，分批节流见 run_folder_mode）
    if args.provider:
        chain = [args.provider]
    else:
        chain = build_chain(cfg)
    log("folder mode: %d file(s), chain=%s" % (len(files), chain))
    question = args.question or cfg.get("default_question")
    # 单文件且不落盘：输出纯描述文本（模型主动调用模式）
    plain = len(files) == 1 and not args.out
    text = run_folder_mode(cfg, files, question, args.out, chain,
                           cfg.get("per_image_max_chars", 2000), plain=plain)
    if text is not None:
        print(text)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main_cli(sys.argv[1:])
    else:
        main()
