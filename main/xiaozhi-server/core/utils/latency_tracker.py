"""实时耗时记录工具

- 单独写入 tmp/latency.log（与 server.log 区分，不污染主日志）
- 单位：秒
- 所有日志均带 [latency] 关键字，可直接 grep 分析
- 支持 turn_id 串联同一轮对话的各阶段
- 中文友好格式，包含对话内容
"""

import os
import time
from loguru import logger as _base_logger

_latency_logger = None
_initialized = False

# ── 阶段中文名映射 ────────────────────────────────────────────────────────────
_STAGE_LABELS = {
    "asr_start":        "语音识别·开始",
    "asr_infer":        "语音识别·推理",
    "asr_total":        "语音识别·完成",
    "intent_llm":       "意图识别",
    "llm_prepare":      "大模型·准备",
    "llm_first_token":  "大模型·首字",
    "llm_stream_total": "大模型·完成",
    "tool_exec":        "工具调用",
    "tool_exec_total":  "工具调用·汇总",
    "tts_synthesis":    "语音合成",
}

_TEXT_MAX_LEN = 50  # 对话文本最大展示字符数

def _truncate(text: str, max_len: int = _TEXT_MAX_LEN) -> str:
    """截断过长的文本，保留首尾"""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _setup_latency_logger():
    """初始化耗时日志 sink（仅执行一次）"""
    global _latency_logger, _initialized
    if _initialized:
        return _latency_logger

    try:
        from config.config_loader import load_config
        config = load_config()
        log_dir = config.get("log", {}).get("log_dir", "tmp")
    except Exception:
        log_dir = "tmp"

    os.makedirs(log_dir, exist_ok=True)
    latency_log_path = os.path.join(log_dir, "latency.log")

    # 添加专用 sink，仅接受带 _latency=True extra 标记的记录
    _base_logger.add(
        latency_log_path,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}",
        level="DEBUG",
        filter=lambda record: record["extra"].get("_latency", False),
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
    )

    _latency_logger = _base_logger.bind(_latency=True)
    _initialized = True
    return _latency_logger


def get_latency_logger():
    """获取耗时专用 logger（首次调用时自动初始化）"""
    return _setup_latency_logger()


def log_latency(stage: str, turn_id: str = "", elapsed_s: float = 0.0, **kwargs):
    """写一条中文友好的耗时日志。

    输出示例::

        ──────────────────────────────────────────────
        轮次[d79f132d] ▶ 新一轮对话开始
        轮次[d79f132d] │ 语音识别·推理 │ 1.547s
        轮次[d79f132d] │ 语音识别·完成 │ 1.578s │ 识别内容: 今天天气怎么样
        轮次[d79f132d] │ 大模型·首字   │ 2.562s │ 对话内容: 今天天气怎么样
        轮次[d79f132d] │ 工具调用      │ 0.688s │ 工具: get_weather
        轮次[d79f132d] │ 语音合成      │ 2.828s │ 合成内容: 今天天气晴朗，温度...

    Args:
        stage:     阶段 key，如 ``asr_total`` / ``llm_first_token`` / ``tts_synthesis``
        turn_id:   本轮对话唯一 ID
        elapsed_s: 耗时（秒）
        **kwargs:  附加信息：
                   - ``text``: 对话/识别/合成的文本内容
                   - ``tool``: 工具名
                   - ``chars``: 合成字符数
                   - ``count``: 工具数量
                   - ``depth``: 递归深度（工具调用后回 LLM 的次数）
    """
    # asr_start 耗时恒为 0，仅做轮次分隔标记
    if stage == "asr_start":
        ll = get_latency_logger()
        ll.info(f"{'─' * 60}")
        ll.info(f"轮次[{turn_id}] ▶ 新一轮对话开始")
        return

    label = _STAGE_LABELS.get(stage, stage)

    # 构建附加描述
    detail_parts = []
    text = kwargs.get("text")
    if text:
        # 根据阶段选择不同的标签
        if stage.startswith("asr"):
            detail_parts.append(f"识别内容: {_truncate(text)}")
        elif stage.startswith("tts"):
            detail_parts.append(f"合成内容: {_truncate(text)}")
        elif stage.startswith("intent"):
            detail_parts.append(f"用户说: {_truncate(text)}")
        else:
            detail_parts.append(f"对话内容: {_truncate(text)}")
    if kwargs.get("tool"):
        detail_parts.append(f"工具: {kwargs['tool']}")
    if kwargs.get("chars") is not None:
        detail_parts.append(f"{kwargs['chars']}字")
    if kwargs.get("count") is not None:
        detail_parts.append(f"共{kwargs['count']}个工具")
    depth = kwargs.get("depth")
    if depth is not None and int(depth) > 0:
        detail_parts.append(f"工具结果回传第{depth}轮")

    detail_str = f" │ {', '.join(detail_parts)}" if detail_parts else ""
    line = f"轮次[{turn_id}] │ {label:<10} │ {elapsed_s:.3f}s{detail_str}"
    get_latency_logger().info(line)


class LatencyTimer:
    """上下文管理器：自动计时并写耗时日志。

    用法::

        with LatencyTimer("asr_infer", turn_id=conn.current_turn_id):
            result = await asr_call(...)

        # 或者手动读取耗时
        timer = LatencyTimer("llm_prepare", turn_id=tid)
        with timer:
            ...
        print(timer.elapsed_s)
    """

    def __init__(self, stage: str, turn_id: str = "", **kwargs):
        self.stage = stage
        self.turn_id = turn_id
        self.kwargs = kwargs
        self._start: float = 0.0
        self.elapsed_s: float = 0.0

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.elapsed_s = time.monotonic() - self._start
        status = "error" if exc_type else "ok"
        log_latency(
            self.stage,
            self.turn_id,
            self.elapsed_s,
            status=status,
            **self.kwargs,
        )
        return False  # 不吞掉原始异常
