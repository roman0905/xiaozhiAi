import asyncio
import json
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.handle.intentHandler import speak_txt
from core.handle.sendAudioHandle import send_stt_message
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.dialogue import Message
from plugins_func.register import Action

TAG = __name__

BLOOD_GLUCOSE_TOOL = "get_glucose_data"
GLUCOSE_CONTEXT_ATTR = "_prefilter_glucose_context"
GLUCOSE_CONTEXT_TTL_SECONDS = 600
BLOOD_GLUCOSE_PATTERNS = [
    re.compile(r"(查|查询|看看|看下|看一看|测|获取|调取).*(血糖|血糖记录|血糖数据|血糖趋势|血糖值)"),
    re.compile(
        r"((?:最近|过去|近|这)[\d一二三四五六七八九十两半俩仨几]+(?:个)?(?:分钟|小时|天)).*"
        r"(血糖|血糖记录|血糖数据|血糖趋势|血糖值)"
    ),
    re.compile(
        r"(现在|当前|最新|最近).*(血糖|血糖记录|血糖数据|血糖趋势|血糖值)"
        r".*(多少|怎么样|数据|记录|趋势|数值|值)"
    ),
    re.compile(
        r"(我(的|这)|帮我|给我|请你).*(血糖|血糖记录|血糖数据|血糖趋势|血糖值)"
        r".*(多少|怎么样|如何|数据|记录|趋势|最新|最近|当前|现在)"
    ),
]
BLOOD_GLUCOSE_FOLLOWUP_PATTERNS = [
    re.compile(r"(数据|记录|趋势|平均|总体|整体).*(怎么样|如何|呢)?"),
    re.compile(r"(数据|记录|趋势|平均|总体|整体).*(又是怎么样的|又怎么样)"),
]
DIRECT_TOOL_REPLY_KEYWORDS = [
    "请先绑定手机号码",
    "手机号码格式不正确",
    "暂无数据",
    "获取数据失败",
    "传感器暂无数据",
    "网络异常",
    "数据解析出错",
]


def _extract_plain_text(text: str) -> str:
    """从可能的 JSON 包裹文本中提取纯文本内容。"""
    try:
        if text.strip().startswith("{") and text.strip().endswith("}"):
            data = json.loads(text)
            if isinstance(data, dict) and "content" in data:
                return data.get("content", "")
    except Exception:
        pass
    return text


def _hit_blood_glucose_query(text: str) -> bool:
    """仅拦截明确的血糖数据查询请求，不接管一般控糖知识问答。"""
    return any(pattern.search(text) for pattern in BLOOD_GLUCOSE_PATTERNS)


def _hit_blood_glucose_followup(conn: "ConnectionHandler", text: str) -> bool:
    """
    允许在同一会话里延续上一轮血糖查询上下文，例如：
    "最近三天的数据又是怎么样的" / "最近三小时呢"
    这类句子不再要求重复说"血糖"。
    """
    context = _get_glucose_context(conn)
    if not context or context.get("topic") != "glucose":
        return False
    # 有活跃血糖上下文且含明确时间范围（例如"最近三小时呢?"），直接拦截
    if _extract_time_range(text):
        return True
    # 无时间范围但含数据相关关键词（例如"数据怎么样呢"）
    return any(pattern.search(text) for pattern in BLOOD_GLUCOSE_FOLLOWUP_PATTERNS)


def _should_prefilter_glucose(conn: "ConnectionHandler", text: str) -> bool:
    return _hit_blood_glucose_query(text) or _hit_blood_glucose_followup(conn, text)


def _get_glucose_context(conn: "ConnectionHandler") -> Optional[dict[str, Any]]:
    context = getattr(conn, GLUCOSE_CONTEXT_ATTR, None)
    if not context:
        return None
    if time.time() - context.get("updated_at", 0) > GLUCOSE_CONTEXT_TTL_SECONDS:
        setattr(conn, GLUCOSE_CONTEXT_ATTR, None)
        return None
    return context


def _remember_glucose_context(
    conn: "ConnectionHandler", phone_number: Optional[str] = None
) -> None:
    context = _get_glucose_context(conn) or {}
    context["topic"] = "glucose"
    context["updated_at"] = time.time()
    if phone_number:
        context["phone_number"] = phone_number
    setattr(conn, GLUCOSE_CONTEXT_ATTR, context)


def _extract_phone_number(text: str) -> Optional[str]:
    phone_match = re.search(r"1[3-9]\d{9}", text)
    if phone_match:
        return phone_match.group(0)
    return None


def _extract_time_range(text: str) -> Optional[str]:
    time_match = re.search(
        r"((?:最近|过去|这|近)[\d一二三四五六七八九十两半俩仨几]+(?:个)?(?:分钟|小时|天)|"
        r"半小时|一小时|两小时|三小时|半天|一天|两天|三天|一周|一个礼拜|一星期)",
        text,
    )
    if time_match:
        return time_match.group(0)
    return None


def _build_tool_args(conn: "ConnectionHandler", text: str) -> dict[str, Any]:
    args: dict[str, Any] = {}
    phone_number = _extract_phone_number(text)
    if not phone_number:
        context = _get_glucose_context(conn)
        if context:
            phone_number = context.get("phone_number")
    if not phone_number:
        # 兜底：从设备绑定的 headers 读取（未来 MAC 绑定方案走此路径）
        phone_number = conn.headers.get("phone_number")
    time_range = _extract_time_range(text)
    if phone_number:
        args["phone_number"] = phone_number
    if time_range:
        args["time_range"] = time_range
    return args


def _extract_latest_glucose_snapshot(tool_text: str) -> dict[str, Optional[str]]:
    snapshot = {"value": None, "time": None}

    time_match = re.search(r"最新的一条传感器数据状态（时间：([^）]+)）", tool_text)
    if time_match:
        snapshot["time"] = time_match.group(1)

    value_match = re.search(r"血糖值\s*\(value\):\s*([0-9.]+)\s*mmol/L", tool_text)
    if value_match:
        snapshot["value"] = value_match.group(1)

    return snapshot


def _build_quick_glucose_reply(tool_text: str) -> Optional[str]:
    snapshot = _extract_latest_glucose_snapshot(tool_text)
    value = snapshot.get("value")
    if not value:
        return None
    return f"你最近一条血糖数据是 {value} mmol/L。"


def _build_tool_context(tool_text: str, quick_reply_sent: bool) -> str:
    if quick_reply_sent:
        return (
            f"{tool_text}\n\n"
            "补充要求：用户已经听到了最新一条血糖值。"
            "请直接给出2条简短建议，不要重复报血糖数值。"
        )
    return (
        f"{tool_text}\n\n"
        "补充要求：请先用一句话给出结论，再给出2条简短建议。"
    )


def _build_fallback_advice(
    conn: "ConnectionHandler", tool_text: str, original_text: str, quick_reply_sent: bool
) -> str:
    if any(keyword in tool_text for keyword in DIRECT_TOOL_REPLY_KEYWORDS):
        return tool_text

    if not getattr(conn, "llm", None):
        return tool_text

    if quick_reply_sent:
        system_prompt = (
            f"{tool_text}\n\n"
            "用户已经听到了最新一条血糖值。"
            "请直接给出2条简短建议，不要重复报血糖数值。"
            "不要使用标题、序号或 Markdown。总字数不超过50字。"
        )
    else:
        system_prompt = (
            f"{tool_text}\n\n"
            "请先给出一句简短结论，再给出2条简短建议。"
            "不要使用标题、序号或 Markdown。总字数不超过60字。"
        )

    try:
        return conn.llm.response_no_stream(
            system_prompt=system_prompt,
            user_prompt=f"用户原话：{original_text}",
        )
    except Exception as exc:
        conn.logger.bind(tag=TAG).warning(f"血糖前置过滤降级回复失败，返回原始结果: {exc}")
        return tool_text


def _enqueue_action_marker(
    conn: "ConnectionHandler", sentence_id: str, sentence_type: SentenceType
) -> None:
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=sentence_id,
            sentence_type=sentence_type,
            content_type=ContentType.ACTION,
        )
    )


def _continue_main_chain_with_tool_result(
    conn: "ConnectionHandler",
    function_call_data: dict[str, Any],
    tool_text: str,
) -> bool:
    """
    将工具结果接回标准 function_call 主链路，而不是在前置层自建对话分支。
    """
    if not getattr(conn, "llm", None):
        return False

    tool_name = function_call_data.get("name")
    if not tool_name:
        return False

    tool_call_id = function_call_data.get("id") or str(uuid.uuid4())
    tool_arguments = function_call_data.get("arguments", "")

    conn.dialogue.put(
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": tool_call_id,
                    "function": {
                        "arguments": "{}" if tool_arguments == "" else tool_arguments,
                        "name": tool_name,
                    },
                    "type": "function",
                    "index": 0,
                }
            ],
        )
    )
    conn.dialogue.put(
        Message(role="tool", tool_call_id=tool_call_id, content=tool_text)
    )

    conn.client_abort = False
    conn.sentence_id = str(uuid.uuid4().hex)
    _enqueue_action_marker(conn, conn.sentence_id, SentenceType.FIRST)
    try:
        return bool(conn.chat(None, depth=1))
    except Exception as exc:
        conn.logger.bind(tag=TAG).warning(f"血糖工具回接主链路失败，转降级回复: {exc}")
        return False
    finally:
        _enqueue_action_marker(conn, conn.sentence_id, SentenceType.LAST)


def _ensure_tool_available(conn: "ConnectionHandler") -> bool:
    if not getattr(conn, "func_handler", None):
        return False
    if not getattr(conn.func_handler, "finish_init", False):
        return False
    return conn.func_handler.has_tool(BLOOD_GLUCOSE_TOOL)


async def try_prefilter_route(conn: "ConnectionHandler", text: str) -> bool:
    """
    主流程入口处的轻量过滤器。
    命中血糖数据查询时先走工具，再回到主 function_call 链路；
    未命中时完全放行原有流程。
    """
    plain = _extract_plain_text(text)
    if not plain or not _should_prefilter_glucose(conn, plain):
        return False

    if not _ensure_tool_available(conn):
        return False

    tool_args = _build_tool_args(conn, plain)
    _remember_glucose_context(conn, tool_args.get("phone_number"))

    await send_stt_message(conn, plain)
    conn.client_abort = False

    function_call_data = {
        "name": BLOOD_GLUCOSE_TOOL,
        "id": str(uuid.uuid4().hex),
        "arguments": json.dumps(tool_args, ensure_ascii=False),
    }

    def process_glucose_query() -> None:
        try:
            result = asyncio.run_coroutine_threadsafe(
                conn.func_handler.handle_llm_function_call(conn, function_call_data),
                conn.loop,
            ).result(timeout=15)
        except Exception as exc:
            conn.logger.bind(tag=TAG).error(f"血糖工具调用失败，回退主链路: {exc}")
            conn.executor.submit(conn.chat, plain)
            return

        if not result:
            conn.executor.submit(conn.chat, plain)
            return

        if result.action == Action.RESPONSE:
            reply_text = result.response or result.result
            if reply_text:
                conn.dialogue.put(Message(role="user", content=plain))
                conn.sentence_id = str(uuid.uuid4().hex)
                speak_txt(conn, reply_text)
            return

        if result.action != Action.REQLLM:
            conn.executor.submit(conn.chat, plain)
            return

        tool_text = result.result or result.response
        if not tool_text:
            conn.executor.submit(conn.chat, plain)
            return

        if any(keyword in tool_text for keyword in DIRECT_TOOL_REPLY_KEYWORDS):
            conn.dialogue.put(Message(role="user", content=plain))
            conn.sentence_id = str(uuid.uuid4().hex)
            speak_txt(conn, tool_text)
            return

        conn.dialogue.put(Message(role="user", content=plain))
        quick_reply = _build_quick_glucose_reply(tool_text)
        quick_reply_sent = bool(quick_reply)
        if quick_reply:
            conn.sentence_id = str(uuid.uuid4().hex)
            speak_txt(conn, quick_reply)

        tool_context = _build_tool_context(tool_text, quick_reply_sent)
        if _continue_main_chain_with_tool_result(conn, function_call_data, tool_context):
            return

        fallback_reply = _build_fallback_advice(
            conn, tool_text, plain, quick_reply_sent
        )
        conn.sentence_id = str(uuid.uuid4().hex)
        speak_txt(conn, fallback_reply or tool_text)

    conn.executor.submit(process_glucose_query)
    return True
