既然前端页面已经准备好，并且你们要跳过硬件那种“播报验证码”的绑定流程，接下来你需要将服务端的逻辑与 App 现有的用户体系打通。

具体操作分为两大部分：**提供 HTTP 接口给 App 获取认证信息**，以及**规范 App 与 AI 服务端的 WebSocket 交互协议**。

以下是详细的操作步骤和接口文档设计：

### 第一步：在业务后端提供 Token 签发接口 (HTTP API)

你需要提供一个 HTTP 接口（比如 `GET /api/ai/get_token`），供 App 并在建立 WebSocket 连接前调用。这个接口的作用是根据当前登录的用户，利用 `xiaozhi-server` 的 `AuthManager` 直接签发合法的 Token。

**接口逻辑参考（服务端 Python 伪代码）：**

Python

```
from core.auth import AuthManager

# 这里的 secret_key 必须与 xiaozhi-server 配置中的 auth.secret_key 保持一致
auth_manager = AuthManager(secret_key="your_server_secret_key")

def get_ai_token(current_user):
    # 使用用户的唯一标识（如 UUID 或 UserID）作为设备 ID
    device_id = f"app_user_{current_user.id}"
    client_id = device_id 
    
    # 生成 Token，此 Token 仅包含签名与时间戳
    token = auth_manager.generate_token(client_id=client_id, username=device_id)
    
    return {
        "device_id": device_id,
        "token": token,
        "ws_url": "wss://你的AI服务端域名"
    }
```

**App 收到响应后，将其保存，用于下一步的连接。**

------

### 第二步：App 建立 WebSocket 连接并鉴权

App 拿到 `device_id` 和 `token` 后，向服务端的 WebSocket 地址发起连接。

必须在 WebSocket 请求的 HTTP Header 中带上认证三元组：

- `Device-ID`: `<你的 device_id>`
- `Client-ID`: `<你的 device_id>`
- `Authorization`: `Bearer <生成的 Token>`

------

### 第三步：WebSocket 握手协议 (Hello)

WebSocket 连接成功后，App 需要立刻发送一条 `hello` 握手消息进行会话初始化。

**1. App 发送 Hello 消息：**

JSON

```
{
    "type": "hello",
    "device_id": "app_user_123",
    "device_name": "用户昵称或手机型号",
    "token": "生成的 Token",
    "features": {
        "mcp": false 
    }
}
```

**2. 服务端回复 Hello 响应：**

服务端验证通过后，会返回包含 `session_id` 的消息，代表握手成功，可以开始交互了：

JSON

```
{
    "type": "hello",
    "session_id": "xxxxx-xxxx-xxxx"
}
```

------

### 第四步：核心交互协议规范

前端页面需要实现以下核心消息的发送与接收：

#### 1. 文本对话（类似豆包的输入框发文字）

**App 发送文本指令：**

JSON

```
{
    "type": "listen",
    "state": "detect",
    "text": "你今天的血糖数据怎么样？"
}
```

*[说明：发送此消息后，服务端的大模型开始推理]*

#### 2. 语音对话（按住说话 / 实时语音）

- **App 发送语音：** 直接通过 WebSocket 发送二进制（Binary）音频数据。根据服务端的默认要求，通常是 Opus 编码格式的音频帧。

- **App 接收 STT（语音转文字）结果：** 用户说话时，服务端会实时返回识别出的文字，App 可用于在界面上展示用户说的话：

  JSON

  ```
  {
      "type": "stt",
      "text": "识别出的文字内容..."
  }
  ```

#### 3. 接收 AI 的回复 (文字 + 语音)

不管是文本还是语音提问，服务端都会返回大模型的文字结果和 TTS 语音流。App 需要监听并处理以下两种类型的消息：

- **接收 LLM 文字回复：** 用于在对话流中展示 AI 的文字气泡。如果有表情，还可以配合触发 App 内小人的动画。

  JSON

  ```
  {
      "type": "llm",
      "text": "这是 AI 回复的文本",
      "emotion": "happy" 
  }
  ```

- **接收 TTS 语音控制消息及二进制音频流：** 服务端会发送 `tts` 状态消息控制语音的播放节奏，随后下发二进制音频供 App 播放。

  JSON

  ```
  // TTS 开始
  {"type": "tts", "state": "start", "session_id": "..."}
  
  // 句子开始 (可以在这里控制小人张嘴动画)
  {"type": "tts", "state": "sentence_start", "text": "这是当前在读的句子"}
  
  // (中间会收到连续的二进制音频数据用于播放)
  
  // 句子结束
  {"type": "tts", "state": "sentence_end", "text": "这是当前在读的句子"}
  
  // TTS 彻底结束 (关闭小人张嘴动画，清空音频缓冲)
  {"type": "tts", "state": "stop"}
  ```

#### 4. 打断机制（重要交互体验）

如果 AI 正在长篇大论（TTS 状态尚未 `stop`），用户此时又点击了输入框发了新消息，或者按下了语音键，App 需要主动发送一个**打断消息**给服务端清空当前队列：

JSON

```
{
    "session_id": "当前AI正在回复的 session_id",
    "type": "abort",
    "reason": "wake_word_detected" 
}
```

### 总结

你当前阶段的任务是：

1. 和后端同事定好 **Step 1** 的发 Token 接口，让前端拿到合法的连接凭证。
2. 让前端同事按照 **Step 2 到 Step 4** 的数据结构，将原先基于网页调用的 WebSocket 逻辑移植到移动端框架（比如 iOS/Android 的 WebSocket 客户端，或者 Flutter/React Native 的 WebSocket 组件）上。音频播放部分需要对接手机原生的音频解码播放器。