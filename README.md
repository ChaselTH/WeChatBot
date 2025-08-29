# WeChatBot

该机器人通过 ADB 与 OpenAI 结合实现简单的自动回复功能。

## 配置

运行前请编辑根目录下的 `config.ini`，可配置项包括：

- `SEND_BTN`、`CHAT_ENTRY` 等设备坐标
- OpenAI 接口相关设置：`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`（需自行提供）
- 轮询间隔 `POLL_INTERVAL`、回复最大 tokens `MAX_TOKENS` 等
- `MENTIONS`：触发机器人的@昵称（多个用逗号分隔）

修改完成后执行 `python main.py` 即可启动机器人。

