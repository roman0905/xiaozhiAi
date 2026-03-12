import requests
import re
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.utils.cache.manager import cache_manager, CacheType
from core.utils.latency_tracker import log_latency
from datetime import datetime, timedelta

TAG = __name__
logger = setup_logging()

# 更新函数描述，让LLM知道它能拿到的实际数据指标（新接口仅包含血糖值）
GET_GLUCOSE_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_glucose_data",
        "description": (
            "获取用户的真实佩戴血糖设备数据。当用户询问自己或某个账号的血糖数据、血糖记录等实际情况时调用本函数。"
            "可以查询最近一段时间的传感器记录（包含血糖值），如果未指定时间范围，默认返回最近15分钟的数据。"
            "支持多种时间单位：分钟、小时、天，会自动转换为分钟数。"
            "如果用户提到了手机号或账号，请将其作为 phone_number 参数传入。"
            "请基于返回的真实数据进行专业、准确的解答。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "phone_number": {
                    "type": "string",
                    "description": "用户的手机号码，11位数字，例如：13312340778。如果用户提到了账号或手机号，请提取并传入",
                },
                "minutes": {
                    "type": "integer",
                    "description": "查询最近多少分钟的数据，例如：15表示查询最近15分钟的数据",
                },
                "time_range": {
                    "type": "string",
                    "description": "时间范围描述，支持语音输入格式，如：'15分钟'、'2小时'、'1天'、'半小时'、'一个星期'等",
                },
                "start_time": {
                    "type": "string",
                    "description": "开始时间，格式：YYYY-MM-DD HH:MM:SS。如果提供，将覆盖minutes和time_range参数",
                },
                "end_time": {
                    "type": "string",
                    "description": "结束时间，格式：YYYY-MM-DD HH:MM:SS。默认为当前时间",
                },
            },
            "required": [],
        },
    },
}

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    # 新接口无需鉴权 token，已保持默认 headers
}


def parse_time_range_to_minutes(time_range_str):
    """
    将语音输入的时间范围转换为分钟数
    """
    if not time_range_str:
        return 15  # 默认15分钟

    time_range_str = time_range_str.strip().lower()

    # 特殊词汇映射
    special_mappings = {
        "半小时": 30, "半个小时": 30, "一刻钟": 15, "一个小时": 60, "一小时": 60,
        "两小时": 120, "三小时": 180, "半天": 720, "一天": 1440,
        "两天": 2880, "三天": 4320, "一周": 10080, "一个礼拜": 10080, "一星期": 10080,
    }

    for key, value in special_mappings.items():
        if key in time_range_str:
            return value

    chinese_numbers = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        "半": 0.5, "俩": 2, "两": 2, "仨": 3, "几": 3,
    }

    patterns = [
        r'(\d+(?:\.\d+)?)\s*(?:个)?(?:分钟?|min|mins?|minute|minutes)',
        r'(\d+(?:\.\d+)?)\s*(?:个)?(?:小时?|时|hour|hours?|hr|hrs?)',
        r'(\d+(?:\.\d+)?)\s*(?:个)?(?:天|日|day|days?)',
        r'([一二三四五六七八九十半俩两仨几]+)\s*(?:个)?(?:分钟?|min|mins?)',
        r'([一二三四五六七八九十半俩两仨几]+)\s*(?:个)?(?:小时?|时|hour|hours?)',
        r'([一二三四五六七八九十半俩两仨几]+)\s*(?:个)?(?:天|日|day|days?)',
    ]

    for i, pattern in enumerate(patterns):
        match = re.search(pattern, time_range_str)
        if match:
            value_str = match.group(1)
            try:
                value = float(value_str)
            except ValueError:
                value = 0
                for char in value_str:
                    if char in chinese_numbers:
                        if char == "十" and value == 0:
                            value = 10
                        elif char == "十":
                            value *= 10
                        else:
                            value += chinese_numbers[char]

            if i in [0, 3]: return int(value)
            elif i in [1, 4]: return int(value * 60)
            elif i in [2, 5]: return int(value * 24 * 60)

    number_match = re.search(r'(\d+)', time_range_str)
    if number_match:
        number = int(number_match.group(1))
        if number <= 60:
            return number * 60 if "时" in time_range_str or "hour" in time_range_str else number
        elif number <= 24:
            return number * 60
        else:
            return number

    logger.warning(f"无法解析时间范围: {time_range_str}，使用默认值15分钟")
    return 15


def fetch_stomed_data(phone_number, start_time=None, end_time=None, minutes=15):
    """
    通过新接口直接获取传感器真实数据，并根据时间范围在本地进行过滤
    """
    try:
        url = f"http://pre-api.stomed.cn/api/sensor/sensor/readings?phoneNumber={phone_number}"
        logger.info(f"请求传感器数据 - 手机号: {phone_number}")
        res = requests.get(url, headers=HEADERS, timeout=10).json()

        sensor_data = res.get("readings", [])
        if not sensor_data or not isinstance(sensor_data, list):
            return None, "传感器暂无数据上报。"

        # 计算时间过滤范围
        end_dt = datetime.now() if not end_time else datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S")
        if not start_time:
            start_dt = end_dt - timedelta(minutes=minutes)
        else:
            start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")

        # 过滤时间段内的数据
        filtered_readings = []
        for item in sensor_data:
            time_ms = item.get("time")
            if time_ms:
                # 毫秒时间戳转 datetime
                item_dt = datetime.fromtimestamp(time_ms / 1000.0)
                if start_dt <= item_dt <= end_dt:
                    filtered_readings.append(item)

        # 按照时间从近到远(倒序)排列
        filtered_readings.sort(key=lambda x: x.get("time", 0), reverse=True)
        return filtered_readings, "获取成功"

    except requests.exceptions.RequestException as e:
        logger.error(f"请求数据服务失败: {str(e)}")
        return None, "网络异常，请求血糖服务器失败。"
    except Exception as e:
        logger.error(f"解析血糖数据时出错: {str(e)}")
        return None, "数据解析出错，请稍后重试。"


def format_glucose_report(readings, time_range_minutes):
    """
    格式化真实传感器数据报告，供 AI 理解并进行回答
    """
    time_desc = format_time_description(time_range_minutes)
    
    if not readings:
        return f"查询成功，但在{time_desc}内未找到该设备的佩戴更新数据。"

    report = f"以下是传感器实际测量的真实数据报告（{time_desc}，共找到 {len(readings)} 条记录）：\n\n"

    # 根据过滤后的实际数据提取血糖值并进行统计，确保统计范围与用户查询范围一致
    glu_values = [r.get('value') for r in readings if isinstance(r.get('value'), (int, float))]
    if glu_values:
        avg_glu = sum(glu_values) / len(glu_values)
        max_glu = max(glu_values)
        min_glu = min(glu_values)
        report += f"【统计信息】\n"
        report += f"· 平均血糖: {avg_glu:.1f} mmol/L\n"
        report += f"· 最高血糖: {max_glu} mmol/L\n"
        report += f"· 最低血糖: {min_glu} mmol/L\n\n"

    # 最近一条数据的详细指标
    latest = readings[0]
    latest_time_str = datetime.fromtimestamp(latest.get('time', 0) / 1000.0).strftime('%Y-%m-%d %H:%M:%S')
    report += f"【最新的一条传感器数据状态（时间：{latest_time_str}）】\n"
    report += f"· 血糖值 (value): {latest.get('value', 'N/A')} mmol/L\n\n"

    # 附加上近 5 条数据的趋势列表
    report += "【最近5条数据详情记录】\n"
    for r in readings[:5]:
        t_str = datetime.fromtimestamp(r.get('time', 0) / 1000.0).strftime('%m-%d %H:%M:%S')
        report += f"[{t_str}] 血糖: {r.get('value', 'N/A')} mmol/L\n"

    return report


def format_time_description(minutes):
    if minutes < 60:
        return f"最近{minutes}分钟"
    elif minutes < 1440:
        hours = minutes // 60
        remaining_minutes = minutes % 60
        if remaining_minutes == 0:
            return f"最近{hours}小时"
        else:
            return f"最近{hours}小时{remaining_minutes}分钟"
    else:
        days = minutes // 1440
        remaining_hours = (minutes % 1440) // 60
        if remaining_hours == 0:
            return f"最近{days}天"
        else:
            return f"最近{days}天{remaining_hours}小时"


@register_function(
    name="get_glucose_data",
    desc=GET_GLUCOSE_FUNCTION_DESC,
    type=ToolType.SYSTEM_CTL
)
def get_glucose_data(conn, phone_number: str = None, minutes: int = None,
                     time_range: str = None, start_time: str = None, end_time: str = None):
    """
    获取用户的血糖真实佩戴数据
    """
    # 手机号优先级：LLM提取 > 设备headers
    if not phone_number:
        phone_number = conn.headers.get("phone_number")
    # 如果仍然没有手机号，要求用户提供
    if not phone_number:
        return ActionResponse(
            Action.REQLLM,
            "请先绑定手机号码，或者在对话中告诉我您的手机号，我才能帮您查询设备的各项真实数据！",
            None
        )

    # 验证手机号格式
    if not re.match(r'^1[3-9]\d{9}$', phone_number):
        return ActionResponse(
            Action.REQLLM,
            "手机号码格式不正确，请提供11位中国大陆手机号，例如：13312340778",
            None
        )

    device_id = conn.headers.get("device-id")
    logger.info(f"设备 {device_id} 请求真实佩戴数据，手机号: {phone_number[:3]}****{phone_number[-4:]}")
    
    # 智能解析时间范围
    final_minutes = 15  # 默认值
    if start_time and end_time:
        pass
    elif time_range:
        final_minutes = parse_time_range_to_minutes(time_range)
        logger.info(f"解析时间范围 '{time_range}' 为 {final_minutes} 分钟")
    elif minutes is not None:
        final_minutes = minutes

    # ==== 核心改动：调用单步数据获取函数 ====
    readings, error_msg = fetch_stomed_data(phone_number, start_time, end_time, final_minutes)

    turn_id = getattr(conn, "current_turn_id", "")

    if readings is None:
        log_latency("glucose_fetch", turn_id, 0.0,
                    text=f"手机号:{phone_number[:3]}****{phone_number[-4:]} | 失败:{error_msg}")
        return ActionResponse(Action.REQLLM, f"获取数据失败：{error_msg}", None)

    # 构建简略摘要写入 latency.log
    time_desc = format_time_description(final_minutes)
    count = len(readings)
    latest = readings[0] if readings else {}
    latest_val = latest.get("value", "N/A")
    latest_time_str = (
        datetime.fromtimestamp(latest.get("time", 0) / 1000.0).strftime("%H:%M:%S")
        if latest.get("time") else "N/A"
    )
    glu_values = [r.get("value") for r in readings if isinstance(r.get("value"), (int, float))]
    avg_str = f"{sum(glu_values)/len(glu_values):.1f}" if glu_values else "N/A"
    log_latency(
        "glucose_fetch", turn_id, 0.0,
        text=(
            f"手机号:{phone_number[:3]}****{phone_number[-4:]} | "
            f"{time_desc} | 共{count}条 | "
            f"最新:{latest_val}mmol/L@{latest_time_str} | 均值:{avg_str}mmol/L"
        ),
    )

    # 格式化报告
    report = format_glucose_report(readings, final_minutes)

    return ActionResponse(Action.REQLLM, report, None)