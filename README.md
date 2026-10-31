# astrbot_plugin_lazy_image

图片懒加载：聊天里的图片不再塞进上下文，而是落盘编号，上下文只留 `[图#N]`；需要知道图里是什么时，模型调 `view_image(N)`，由视觉模型看图并返回文字。

## 解决的问题

AstrBot 默认会把图片以 base64 放进上下文，**每轮请求都重发一遍**。一张 768×1024 的图约 160KB base64、上千 vision token，十张图就是 1.6MB + 上万 token；服务器上行只有 3 Mbps 时，光传输就要几十秒。

这个插件把图挪到磁盘：

| | 默认行为 | 用本插件后 |
| --- | --- | --- |
| 普通聊天（历史 10 张图） | 1.6MB 上传 + 1.5 万 token | ~0 + 纯文本 |
| 发图问一句 | 同上 | 35KB 缩略图，模型直接看 |
| 需要看旧图 | 已经在上下文里了 | 一次 `view_image`（模型直接看原图） |

## 工作方式

```
用户发图（不管有没有 @ 机器人）
  → 插件落盘 plugin_data/astrbot_plugin_lazy_image/<会话>/12.png
  → 上下文里的图片块换成 [图片]，消息末尾补 "[图#12]"

模型需要看旧图
  → 调 view_image(12)
  → 聊天模型自己支持视觉时：插件把原图（768 长边）作为 ImageContent 返回，
    AstrBot 会把它追加到上下文，模型**直接看到图**
  → 聊天模型不支持视觉时：退回让视觉模型描述成文字
```

为什么历史图要走视觉模型：AstrBot 的 `on_llm_request` 只在对话开始时执行一次，
工具循环中途无法再把图塞进请求；所以历史图只能由插件调视觉模型转述成文字返回。
当前消息的图不受这个限制（在请求前就能带上）。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/img_list` | 看本会话存了哪些图 |
| `/img_show 12` | 把 #12 重新发出来 |
| `/img_clean` | 清理超过保留天数的图 |

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | true | 总开关 |
| `keep_current_image` | true | 当前消息的图直接带给聊天模型（512 缩略图，约 35KB）；关掉就只给 `[图#N]` |
| `vision_provider` | 空 | 看图用的视觉模型，空=用 `provider_settings.default_image_caption_provider_id` |
| `vision_max_side` | 768 | 传给视觉模型的图片长边 |
| `vision_prompt` | 空 | 自定义看图指令 |
| `llm_max_side` | 512 | 直接注入时的图长边 |
| `max_images_per_request` | 2 | 单次请求最多注入几张 |
| `max_images_per_message` | 3 | 一条消息最多处理几张 |
| `retention_days` | 7 | 保留天数，0=不删 |
| `inject_hint` / `hint_items` | true / 5 | 是否把可用编号注入系统提示 |
| `capture_hint_seconds` | 180 | 先发图再提问时，多久内自动补 `[图#N]` |
| `allowed_sessions` / `blocked_sessions` | 空 | 生效范围 |

## 注意

- 图片落盘后用的是**本地路径**，删掉 `plugin_data/astrbot_plugin_lazy_image/` 就全没了
- 只影响上下文，不影响绘图插件：`edit_image` / `describe_image_as_prompt` 仍然直接从消息里取图
- 对话模型支持视觉时，`view_image` 直接把原图塞回上下文，不存在"缓存文字"这回事
