# xiaozhi-server 血糖关键词入口过滤方案

## 1. 目标

- 在主流程入口处加一个轻量过滤；
- 命中“查自己的血糖数据”类请求时，优先调用 `get_glucose_data`；
- 先播报最近一条血糖值；
- 然后把工具结果接回原有 `function_call` 主链路，让主 LLM 基于真实数据补充 2 条简短建议；
- 未命中时，完全走原有流程。

## 2. 插入位置

插入点放在：

- `from core.handle.prefilterHandler import try_prefilter_route`

- `core/handle/receiveAudioHandle.py` 的 `startToChat(...)`

调用顺序保持为：

1. ASR 得到文本；
2. 入口过滤先判断是否是“查自己的血糖数据”；
3. 命中则先走 `get_glucose_data`；
4. 不命中则继续原有 `handle_user_intent(...)` 和 `conn.chat(...)`。

也就是说，入口过滤只是主流程开口处的一个“前置判断”，不是一条独立业务支线。

## 3. 正确链路

命中血糖数据查询时，正确链路应为：

1. 用户说：“我的手机号为 133xxxx0789，请查最近 2 小时的血糖”
2. `startToChat(...)` 先调用入口过滤器；
3. 入口过滤识别为“查血糖数据”请求；
4. 过滤器提取轻量参数，比如 `phone_number`、`time_range`；
5. 直接调用 `get_glucose_data`；
6. 工具返回完整血糖报告；
7. 从报告里提取“最近一条血糖值”；
8. 先通过现有 TTS 流程播报一句，例如：“查到了，你最近一条血糖数据是 9.1 mmol/L。”
9. 然后把这次工具调用结果重新写回 `dialogue`，接回标准 `function_call` 主链路；
10. 主 LLM 基于真实血糖报告补充 2 条简短建议；
11. 建议继续走原有文本转语音输出。

这一点很关键：

- 第一段“快速播报”可以由入口过滤直接发出；
- 第二段“建议回复”必须尽量回到原主链路，而不是在过滤器里再写一套简化对话逻辑。

---

## 4. 命中条件

入口过滤只拦截明确的数据查询意图，例如：

- “帮我查一下最近两小时血糖”
- “看看我现在血糖怎么样”
- “我这两天的血糖趋势怎么样”
- “请你查询 13312340778 最近半小时的血糖记录”

不要使用过宽规则，比如：

```python
r"血糖"
```

这种写法会把大量普通知识问答错误拦截，直接把系统变笨。

推荐规则应至少满足下面之一：

- 有“查 / 查询 / 看看 / 测 / 获取”这类数据查询动词；
- 有“最近两小时 / 这两天 / 当前 / 最新”这类时间或实时查询特征；
- 有明显的第一人称数据请求，比如“我的血糖数据怎么样”。

---

## 5. 参数提取

入口过滤只做轻量参数提取，不做复杂状态管理。

推荐提取：

- `phone_number`
- `time_range`

示例：

- “我的手机号为 13312340778，请查最近 2 小时的血糖”
  - `phone_number=13312340778`
  - `time_range=最近2小时`

- “我这两天的总体血糖数据怎么样”
  - `time_range=这两天`

如果没提手机号：

- 让 `get_glucose_data` 自己走 `conn.headers["phone_number"]`；
- 如果工具仍然发现缺手机号，就直接返回原始提示语即可；
- 不要在过滤器里继续维护“等待用户补手机号”的状态机。

---

## 6. 工具返回后的处理原则

### 6.1 先播报一个值

命中成功后，先从工具返回文本里提取：

- 最新血糖值
- 最新时间（可选）

然后先播报一句短结果。

例如：

- “查到了，你最近一条血糖数据是 9.1 mmol/L。”

这样可以显著缩短用户感知延迟。

### 6.2 再回主链路

工具返回 `Action.REQLLM` 时，后续不要在过滤器里自己重新造一个“简化版聊天流程”。

正确做法是：

1. 把这次工具调用信息写回 `dialogue`
2. 把工具结果作为 `tool` 消息写回 `dialogue`
3. 再继续调用原本的 `function_call` 主链路

这样第二段建议仍然由原有主 LLM 负责，能保留：

- 系统角色设定
- 对话上下文
- 原有 function calling 行为
- 原有 TTS 输出链路

### 6.3 只做简短建议约束

虽然第二段建议回主链路生成，但可以给一个非常轻的约束，例如：

- 用户已经听到了最新血糖值
- 不要重复报数值
- 只补充 2 条简短建议

这个约束应该是“给主链路的补充提示”，而不是替代主链路。

---

## 7. 实现骨架

推荐保留一个轻量入口文件：

- `core/handle/prefilterHandler.py`

职责仅限于：

- 识别是否命中血糖数据查询
- 提取手机号和时间范围
- 调一次 `get_glucose_data`
- 先播报一个值
- 把工具结果回接主链路

核心伪代码如下：

```python
async def try_prefilter_route(conn, text: str) -> bool:
    plain = extract_plain_text(text)
    if not hit_blood_glucose_query(plain):
        return False

    if not tool_available(conn, "get_glucose_data"):
        return False

    await send_stt_message(conn, plain)

    function_call_data = {
        "name": "get_glucose_data",
        "id": uuid(),
        "arguments": json.dumps(extract_args(plain), ensure_ascii=False),
    }

    def process():
        result = call_tool(conn, function_call_data)
        if not result:
            conn.executor.submit(conn.chat, plain)
            return

        conn.dialogue.put(Message(role="user", content=plain))

        if result.action == Action.RESPONSE:
            speak_txt(conn, result.response or result.result)
            return

        tool_text = result.result or result.response
        if is_direct_tool_reply(tool_text):
            speak_txt(conn, tool_text)
            return

        quick_reply = build_quick_glucose_reply(tool_text)
        if quick_reply:
            speak_txt(conn, quick_reply)

        tool_context = build_tool_context(tool_text, quick_reply_sent=bool(quick_reply))
        continue_main_chain_with_tool_result(conn, function_call_data, tool_context)

    conn.executor.submit(process)
    return True
```

---

## 8. 当前实现约束

当前血糖插件：

- 文件：`plugins_func/functions/get_glucose_data.py`
- 工具名：`get_glucose_data`
- 返回：`Action.REQLLM`

因此入口过滤器的职责不是“替代插件回答”，而是：

1. 让工具先拿到真实数据；
2. 先抢一条最快结论；
3. 再把真实数据交还主链路继续回答。

---

## 9. 测试建议

1. 命中血糖数据查询
   - 输入：“我的手机号为 13312340778，请查询最近 2 小时的血糖”
   - 预期：先播报最近一条血糖值，再由主链路补充 2 条建议。
2. 时间范围提取
   - 输入：“我这两天的总体血糖数据怎么样”
   - 预期：命中过滤，提取 `这两天`，而不是默认最近 15 分钟。
3. 仅知识问答
   - 输入：“为什么空腹血糖会高”
   - 预期：不过滤，放行原有主链路。
4. 工具缺手机号
   - 输入：“帮我查一下最近半小时血糖”
   - 预期：如果设备未绑定手机号，则直接播报工具返回的提示语，不进入小型状态机。
