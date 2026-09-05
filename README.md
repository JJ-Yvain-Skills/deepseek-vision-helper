# ZCode Vision Hook —— 给纯文本模型装上"眼睛"

粘贴/拖入图片提交消息时，自动调用视觉模型（默认 agnes-2.5-flash）识别，
把结果以 `[Vision result] ...` 注入对话上下文，纯文本模型（DeepSeek 等）即可直接回答图片相关问题。

## 原理

```
粘贴图片 + 提问 → UserPromptSubmit Hook → 从会话 DB input_history 取本次提交的附件 → 调视觉 API → 结果注入上下文 → 文本模型回答
```

- Hook 事件：`UserPromptSubmit`（每次用户提交消息时触发，无图时静默跳过，开销约 0.2 秒）
- 取图（权威信号）：`~/.zcode/cli/db/db.sqlite` 的 `input_history.attachments` 记录本次
  实际提交的附件（`zcode-artifact://` URI，写入时机早于 hook 触发约 1s）→ 精确识别。
  **输入框里"贴了又删"的附件（磁盘文件残留、无提交记录）不再被误识别**（2026-08-11 修复）。
- 兜底：会话 DB 不可用（路径缺失/查询失败）时降级"新鲜度门控"扫描——只识别
  `fresh_seconds`（默认 300s）内新落盘的附件，防陈年残留误识别。
- 续传：预算截断的附件标记 `_skipped`，由"继续"或后续 PreToolUse 补识别。
- `PreToolUse` 不再扫描新附件（防"贴在输入框还没发"的图被提前识别）；只做续传。
- 注入：stdout 输出 `{"additionalContext": "[Vision result] ..."}`
- 失败静默：无图 / 接口报错 → 空输出（exit 0），不影响对话

## 文件

| 文件 | 作用 |
|---|---|
| `vision_hook.py` | Hook 脚本 + CLI 批量模式（Python 3，仅标准库） |
| `vision_mcp.py` | MCP server：把识别能力注册为工具，主/子 Agent 任务中遇图可主动调用（薄壳，复用 vision_hook.py CLI） |
| `config.json` | 提供商配置 + API key（**密钥，勿提交仓库**） |
| `vision_hook.log` / `vision_mcp.log` | 运行日志（调试用，不含 key） |

## MCP 主动调用通道（任务中遇图）

用户贴图由 hook 自动识别（被动）；**任务中遇到图片文件**（主 Agent / 子 Agent 分析截图、图表、报错界面等）时，模型通过 MCP 工具主动识别：

- 工具：`vision_recognize(path, question?)`（单图）、`vision_batch(folder, out?)`（批量落盘）
- 注册：`~/.zcode/cli/config.json` 的 `mcp.servers`（见下），改后重启 ZCode 或 `/mcp connect vision` 生效
- **薄壳设计**：MCP 内部 subprocess 调 `vision_hook.py --files/--folder`，识别逻辑/路由/降级/配置全复用——优化 hook/CLI 自动继承，MCP 不会失效或脱节
- 使用引导在 SKILL.md「主动识别通道」章节

```json
{
  "mcp": {
    "servers": [
      {
        "name": "vision",
        "command": "<python 解释器绝对路径>",
        "args": ["<vision_mcp.py 绝对路径>"],
        "protocolVersion": "auto"
      }
    ]
  }
}
```

## 自动路由（单后端 agnes + 分批节流 + 定时重试）

| 场景 | 行为 |
|---|---|
| 任意张数 | 统一走 `provider`（默认 agnes / agnes-2.5-flash，免费） |
| 批量（超过 `batch_chunk_size` 张） | 不换后端，**分批提交**：每批 N 张串行识别，批间暂停 `batch_chunk_pause` 秒（避开免费后端限流） |
| 单张报错（网络波动/超时/429/5xx/1302/1305） | 按 `retry_delays` 定时退避轮询重试（默认 2s→5s→10s，共 4 次尝试） |
| 认证/额度类错误（401/402/403） | 不重试（重试无意义），快速失败并在日志标明 |
| 手动强制 | 环境变量 `VISION_PROVIDER=zhipu` 强制只用某 provider |
| 可选降级 | 配置 `fallback_provider` 后，provider 失败会降级到它重试该图（默认不配置） |

配置项（`config.json`，改动即时生效）：

| 键 | 默认 | 说明 |
|---|---|---|
| `provider` | `agnes` | 常规后端 |
| `batch_chunk_size` | 3 | 分批大小：每识别 N 张暂停一次 |
| `batch_chunk_pause` | 2 | 批间暂停秒数 |
| `retry_delays` | [2, 5, 10] | 单张失败后的定时退避重试间隔（秒），列表长度即重试次数 |
| `fallback_provider` | （无） | 可选降级后端，配置了才启用 |
| `max_images` | 4 | 单次最多识别张数（超出部分注入时注明） |
| `per_image_max_chars` / `total_max_chars` | 2000 / 8000 | 单张/总注入长度上限 |
| `session_db_path` | 由 artifacts_dir 推导 | 会话 DB 路径（`input_history` 权威信号来源；默认 `~/.zcode/cli/db/db.sqlite`） |
| `fresh_seconds` | 300 | DB 不可用时的兜底扫描新鲜度窗口（秒） |

批量行为：每张图独立调用、**串行**执行（避免撞免费后端并发限流），每 `batch_chunk_size`
张后暂停 `batch_chunk_pause` 秒再提交下一批，结果合并注入（`图1: ... 图2: ...`）。
单张最坏耗时 ≈ (1+重试次数) × 单张超时；嫌慢可调小 `retry_delays` 或 `timeout_seconds`。

Provider 说明：

| provider | 后端 | 说明 |
|---|---|---|
| `agnes` | agnes-2.5-flash（apihub.agnes-ai.com） | 当前唯一在用后端，免费、稳定 |
| `zhipu` | 免费 GLM-4.6V-Flash | 备选（config.example 示例，本机当前未配置） |

> 2026-09-05 变更：移除 opencode.ai 网关的 `mimo` 后端（该工作区额度耗尽，HTTP 401
> CreditsError），批量与降级统一改走 agnes + 分批 + 定时重试。如需恢复多后端，
> 在 `providers` 里加条目并配 `fallback_provider` 即可。

## 批量识别文件夹（数十张图）

交互式贴图适合 1~5 张。**几十张图请用命令行批量模式**，结果写文件、不占对话上下文：

```bash
# 扫描目录下所有图片（递归），结果写入 results.md
python vision_hook.py --folder "D:/图片目录" --out "D:/图片目录/results.md"

# 指定文件列表
python vision_hook.py --files a.png b.png c.png --out results.md

# 强制某 provider / 限量
python vision_hook.py --folder "D:/图片目录" --provider zhipu --max 20 --out results.md
```

- 路由与 hook 完全一致：统一走默认 provider（agnes），批量分批提交、批间暂停，单张失败定时退避重试（`--provider` 可强制）
- 支持 png/jpg/jpeg/webp/gif/bmp；串行执行，每张独立调用
- **在对话里直接说**："识别 D:/xxx 下所有图片，结果存到 results.md"——DeepSeek 会自己调用这个脚本，再读文件帮你汇总，几十张图也不怕
- `--out` 建议必填：结果落盘后模型按需读取，避免把几十段描述灌进上下文

## 故障排查

1. **完全不生效**：查看 `vision_hook.log` 是否有 `hook fired`；没有说明 hook 没被调用
   （检查 `~/.zcode/cli/config.json` 的 `hooks.enabled: true`）。
2. **`no image found`**：日志里看是"无附件提交"还是"权威信号缺失走兜底"。如果确认贴了图
   且日志显示 `session db not found`，检查 `session_db_path` 配置（默认由 `artifacts_dir`
   推导为 `~/.zcode/cli/db/db.sqlite`）；显示 `no input_history row for session` 说明
   hook 触发早于记录写入（极罕见，ZCode 实测早约 1s 写入）。
3. **`vision api failed`**：看具体错误；`1305` 是平台过载，稍后重试即可；401 是 key 无效。
4. **Hook 没跑起来但配置正确**：用下面的命令手动测脚本。

### 手动测试

```bash
# 无图场景（应无输出、退出码 0）
echo '{"hook_event_name":"UserPromptSubmit","session_id":"x","transcript_path":"不存在的文件","prompt":"hi"}' \
  | "/c/Users/JiangLiu/AppData/Roaming/uv/python/cpython-3.12-windows-x86_64-none/python.exe" \
  "C:/Users/JiangLiu/.zcode/vision-hook/vision_hook.py"
```

### 备选：只用 PreToolUse 事件（不推荐）

识别以 `UserPromptSubmit` 为唯一触发（提交级附件识别）；`PreToolUse` 只做预算截断的
续传，不再扫描新附件（防止"贴在输入框还没发"的图被提前识别——旧图混入的根源之一）。
请保持配置中 `UserPromptSubmit` 注册，不要只挂 `PreToolUse`。

## 安全提醒

- key 只存在本机 `config.json`；不要在聊天中明文发送、不要提交到任何仓库。
- 若 key 曾在聊天/日志中泄露过，到对应 provider 的控制台重置（本机当前：agnes）。
