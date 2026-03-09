# xiaozhi-server 调用延迟定位与落盘方案（ASR vs LLM）

## 1. 目标

你关心的是：**主要耗时在语音转文本（ASR）还是大模型（LLM）**。

本方案提供一套可落地的方法：

1. 在 `xiaozhi-server` 主链路加分段耗时日志；
2. 用统一 `turn_id` 串起一次用户输入全链路；
3. 日志落盘到 `tmp/`目录下与已有的server.log区分；
4. 用简单统计脚本得到每个阶段的 max/p95，快速定位最慢环节。

---

## 2. 现状（代码里已有的耗时信息）

目前项目里已经有部分性能日志：

- `ASRProviderBase.handle_voice_stop` 有 `总处理耗时` 记录。
- `intent_llm` provider 有意图识别总耗时 / LLM 调用耗时。

但仍有两个缺口：

1. **缺少统一链路ID**（一轮对话难串起来）。
2. **缺少分段拆解**（ASR内部、chat首Token、tool执行、TTS首包等）。

---

## 3. 分段模型（建议统一）

一次语音请求建议拆成以下阶段：

- `audio_to_asr_text_ms`：音频结束到 ASR 返回文本
- `intent_ms`：意图识别耗时（仅 `intent_llm` 有）
- `llm_prepare_ms`：调用 LLM 前准备耗时（上下文/memory/functions）
- `llm_first_token_ms`：LLM 首 token 延迟（最关键体验指标）
- `llm_stream_total_ms`：LLM 全量输出耗时
- `tool_exec_ms`：工具执行耗时（单工具或总和）
- `tts_first_packet_ms`：文本转到第一包音频发送耗时
- `turn_total_ms`：从 `startToChat` 到本轮可播报完成总耗时

> 最终判定规则：比较 `audio_to_asr_text_ms` 和 `llm_first_token_ms / llm_stream_total_ms` 的 p95 / max。

---

## 4. 实施步骤

## 4.1 为每轮请求生成 `turn_id`

位置：`core/handle/receiveAudioHandle.py -> startToChat`

示例：

```python
import time
import uuid

turn_id = uuid.uuid4().hex[:8]
turn_start = time.monotonic()

conn.current_turn_profile = {
    "turn_id": turn_id,
    "start_monotonic": turn_start,
}

conn.logger.bind(tag=TAG).info(
    f"[latency][{turn_id}] turn_start text={actual_text[:50]}"
)
```

说明：

- 用 `time.monotonic()`，避免系统时间跳变；
- 后续所有阶段日志都带 `[latency][turn_id]`。

---

## 4.2 ASR 段打点

位置：`core/providers/asr/base.py -> handle_voice_stop`

建议记录：

- `asr_decode_ms`（Opus->PCM / 文件准备）
- `asr_infer_ms`（`speech_to_text_wrapper`）
- `voiceprint_ms`（如果开了声纹）
- `audio_to_asr_text_ms`（总计）

示例：

```python
t0 = time.monotonic()
# ... decode / assemble
asr_start = time.monotonic()
asr_result = await asr_task
asr_infer_ms = (time.monotonic() - asr_start) * 1000

total_ms = (time.monotonic() - t0) * 1000
logger.bind(tag=TAG).info(
    f"[latency][{conn.session_id}] stage=asr audio_to_asr_text_ms={total_ms:.2f} asr_infer_ms={asr_infer_ms:.2f}"
)
```

---

## 4.4 LLM 段打点（重点）

位置：`core/connection.py -> chat`

建议新增：

1. `llm_prepare_ms`：`self.llm.response(...)` 或 `response_with_functions(...)` 前准备耗时
2. `llm_first_token_ms`：进入流式循环后第一段内容到达时间
3. `llm_stream_total_ms`：流式结束总耗时
4. `tool_exec_ms`：等待所有 tool future 的时间

示例：

```python
llm_begin = time.monotonic()
llm_responses = self.llm.response(...)
prepare_ms = (time.monotonic() - llm_begin) * 1000

first_token_ts = None
stream_begin = time.monotonic()
for response in llm_responses:
    if first_token_ts is None:
        first_token_ts = time.monotonic()
        first_token_ms = (first_token_ts - llm_begin) * 1000
        self.logger.bind(tag=TAG).info(
            f"[latency][{turn_id}] stage=llm_first_token ms={first_token_ms:.2f}"
        )

stream_total_ms = (time.monotonic() - stream_begin) * 1000
```

工具耗时建议在等待 futures 前后打点：

```python
tool_t0 = time.monotonic()
# future.result() ...
tool_exec_ms = (time.monotonic() - tool_t0) * 1000
self.logger.bind(tag=TAG).info(
    f"[latency][{turn_id}] stage=tool_exec ms={tool_exec_ms:.2f} count={len(tool_calls_list)}"
)
```

---

## 4.5 工具执行细分（可选但推荐）

位置：`core/providers/tools/unified_tool_manager.py -> execute_tool`

每个工具加单独日志，便于发现哪个插件慢：

```python
start = time.monotonic()
result = await executor.execute(self.conn, tool_name, arguments)
cost_ms = (time.monotonic() - start) * 1000
self.logger.info(f"[latency] stage=tool name={tool_name} ms={cost_ms:.2f}")
```

---

## 4.6 日志落盘

当前 `config/logger.py` 已配置文件输出（默认 `tmp/server.log`，异步写入）。

你只需要：

1. 确认 `config.yaml`：

```yaml
log:
  log_level: DEBUG
  log_dir: tmp
  log_file: server.log
```

2. 统一用 `[latency]` 关键字输出。

这样所有性能日志会自动保存到 `tmp/server.log`。

---

## 5. 统计“最慢阶段”方法

## 5.1 建议先跑 50~100 轮样本

覆盖：

- 纯闲聊（LLM）
- 血糖查询（prefilter+func）
- 天气/音乐等工具调用

## 5.2 用脚本统计（示例）

```python
# scripts/analyze_latency.py
import re
from collections import defaultdict

pattern = re.compile(r"stage=([a-zA-Z0-9_]+).*?ms=([0-9.]+)")
stats = defaultdict(list)

with open("tmp/server.log", "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        if "[latency]" not in line:
            continue
        m = pattern.search(line)
        if m:
            stage = m.group(1)
            ms = float(m.group(2))
            stats[stage].append(ms)

for stage, values in stats.items():
    values.sort()
    p95 = values[int(len(values) * 0.95) - 1] if len(values) >= 20 else max(values)
    print(f"{stage}: count={len(values)} avg={sum(values)/len(values):.1f} p95={p95:.1f} max={max(values):.1f}")
```

输出后你会直接看到：

- 是 `audio_to_asr_text_ms` 最大（ASR瓶颈）
- 还是 `llm_first_token_ms/llm_stream_total_ms` 最大（LLM瓶颈）
- 还是 `tool_exec_ms` 最大（插件/外部接口瓶颈）

---

## 6. 典型结论判读

- `audio_to_asr_text_ms` 高：
  - 优先检查 ASR provider（本地模型算力、远程ASR网络、音频长度）。
- `intent_ms` 高：
  - `intent_llm` 增加了前置一轮 LLM，若追求时延可切 `function_call`。
- `llm_first_token_ms` 高：
  - 模型首Token慢（模型/网络/上下文过长）。
- `tool_exec_ms` 高：
  - 某插件依赖外部API慢，需缓存/超时/降级。

---

## 7. 低风险落地顺序（推荐）

1. 先只加日志，不改业务行为；
2. 采样并统计，确认最大瓶颈；
3. 再做针对性优化（ASR/LLM/插件）；
4. 最后接入你要的血糖前置过滤，观察命中场景延迟是否显著下降。

这样可以避免“先改一堆，再找不到真正慢点”的问题。
