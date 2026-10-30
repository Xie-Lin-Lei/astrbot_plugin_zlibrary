# astrbot_plugin_zlibrary

在 QQ 里发一条命令就能从 Z-Library 搜书，选中后机器人把电子书文件直接发给你；也可以发一张书籍封面图，让它自己认书。

> 需要一个 Z-Library 账号（免费账号每天有下载额度），其余都能在插件面板里配。

## 三步开始

**1. 安装**

把整个目录放到 AstrBot 的 `data/plugins/` 下，或在 WebUI「插件管理 → 从文件/文件夹导入」上传 zip。AstrBot 会按 `requirements.txt` 自动安装 `curl-cffi`（首次需要能连 PyPI，稍等片刻）。

**2. 改三个参数**

插件管理 → **Z-Library 找书** → 配置：

| 参数 | 必填 | 填什么 |
| --- | --- | --- |
| `email` | ✅ | Z-Library 账号邮箱 |
| `password` | ✅ | Z-Library 账号密码 |
| `proxy` | 国内必填 | 能访问 Z-Library 的代理，例如 `http://127.0.0.1:7897` |

其余参数保持默认即可：

```json
{
  "base_url": "https://z-library.ec, https://1lib.sk",
  "proxy": "",
  "impersonate": "chrome",
  "cover_search": true,
  "search_limit": 8,
  "send_file": true,
  "max_file_mb": 200
}
```

上面三步**不需要任何模型 API Key**，只要 Z-Library 账号。可选的「以图找书」才会用到 AstrBot 里已配置的视觉模型（见下文）。

**3. 验证并开用**

私聊机器人发送 `找书检测`，看到「配置体检」里账号/密码已填、站点显示「接口正常」、最后一行「登录成功」，就可以发了：

```
找书 三体
选书 1
```

## 命令

| 命令 | 别名 | 作用 |
| --- | --- | --- |
| `找书 <书名/作者/关键词>` | `zhaoshu` | 搜索并返回带编号的结果列表 |
| `选书 <编号>` | `xuanshu` | 下载该结果并以 QQ 文件发送，附当日下载额度 |
| `以图找书` | `找书封面` / `coverbook` | 发送封面图后识别书名并搜索 |
| `找书检测` | `zlibcheck` | 检查配置、域名连通性与登录状态 |

群聊里需要唤醒机器人：`@机器人 找书 三体`，或使用 AstrBot 的唤醒前缀（默认 `/`）。

### 以图找书怎么用（可选功能）

先发封面图，再发 `以图找书`（图片默认保留 10 分钟）；也可以图片和命令放在同一条消息里。

**这个功能要用到 AstrBot 里的对话模型，但模型和 API Key 不在本插件里配置。** 插件只是按 ID 去调用你 AstrBot 中已经配好的模型：

1. 在 AstrBot「服务提供商 / 模型」里添加一个**支持图片**的模型（如 `deepseek-flash`、`glm-4.6v`、`gpt-4o`、`qwen-vl`），API Key 填在那里。
2. 在插件配置的 `vision_provider_id` 里填它的 ID——就是提供商列表里显示的那个名字，例如 `deepseek/deepseek-flash`。
3. 可以按顺序写多个、用逗号分隔，前一个被限流或出错会自动换下一个：

```
deepseek/deepseek-flash, zhipu/glm-4.6v-flash
```

留空则直接使用当前默认对话模型。

**不想配模型也完全没问题**：把 `cover_search` 关掉即可，`找书` / `选书` / `找书检测` 都不依赖任何模型，只需要 Z-Library 账号。

## 环境要求

| 项目 | 说明 |
| --- | --- |
| Z-Library 账号 | 免费账号即可，每日下载额度有限 |
| 网络 | 服务器要能访问 Z-Library。国内网络会被 DNS 污染，必须配 `proxy` |
| 对话模型 | **只有「以图找书」需要**。模型和 API Key 在 AstrBot「服务提供商」里配置，插件只按 ID 调用，不保存任何 Key。`找书` / `选书` / `找书检测` 不需要模型 |
| QQ 协议端 | 发文件依赖 OneBot 的 `upload_group_file` / `upload_private_file`，需与 AstrBot 同机；若协议端在别的机器/容器，配置 AstrBot 的 `callback_api_base`，插件会改发临时链接 |

## 配置项

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `base_url` | string | `https://z-library.ec, https://1lib.sk` | 站点域名，可填多个（逗号分隔），按顺序尝试 |
| `email` | string | 空 | Z-Library 账号邮箱，**必填** |
| `password` | string | 空 | Z-Library 账号密码，**必填** |
| `proxy` | string | 空 | HTTP 代理，国内必填 |
| `impersonate` | string | `chrome` | 模拟的浏览器指纹，被拦时可换 `chrome124` / `edge101` / `firefox133` |
| `vision_provider_id` | string | 空 | 封面识别用的模型 ID，逗号分隔按顺序尝试；空则跟随默认模型 |
| `cover_search` | bool | `true` | 是否启用「以图找书」 |
| `cover_ttl_minutes` | int | `10` | 先发图片再发命令时，图片的有效时间 |
| `vision_prompt` | text | 空 | 覆盖内置的识别提示词 |
| `search_limit` | int | `8` | 每次返回的结果数量（1-50） |
| `order` | string | `bestmatch` | 排序：`bestmatch` / `mostpopular` / `newest` |
| `extensions` | list | `[]` | 限定格式，如 `["PDF","EPUB"]`；空为不限 |
| `languages` | list | `[]` | 限定语言，如 `["chinese"]`；空为不限 |
| `send_file` | bool | `true` | 关闭则只回下载链接，不发文件 |
| `max_file_mb` | int | `200` | 超过该大小只发链接；0 为不限 |
| `download_timeout` | int | `180` | 下载超时（秒） |

## 常见问题

### 连不上：`远程计算机拒绝网络连接` / DNS 解析到奇怪 IP

国内网络的典型症状：域名被解析到假 IP。配 `proxy` 解决，代理必须装在 **AstrBot 所在的那台机器**上，填 `http://127.0.0.1:7897`（Clash 默认混合端口，其他客户端可能是 7890/1080/10809）。

### `HTTP 403 Just a moment...` / `HTTP 513 Verifying your browser`

站点前面的 Cloudflare / DiamWall 在按 **TLS 指纹**拦人，改 User-Agent 没用。插件用 `curl_cffi` 模拟 Chrome 指纹来通过，先确认 `找书检测` 第一行显示 `引擎：curl_cffi（chrome 指纹）`；显示 `aiohttp` 说明依赖没装上，重载插件或手动 `pip install curl-cffi`。仍有域名被拦就换域名——`找书检测` 会把每个域名标成「接口正常」或「浏览器验证」。

### `模型繁忙/被限流（429）`

模型服务端过载，不是配置问题。稍后重试，或在 `vision_provider_id` 里加备用模型（逗号分隔）。

### 识别失败 / 读不出书名

封面文字要清晰完整。推理类模型如果只返回思考过程，插件会自动从 `reasoning_content` 提取书名；实在不行直接 `找书 书名`。

### 提示「已下载，但发送文件失败：send c2c file message rejected」

这是 QQ 侧拒绝了文件消息，不是插件下载失败——文件已经在服务器的 `data/plugin_data/astrbot_plugin_zlibrary/downloads/` 里。

插件现在优先调用 OneBot 的 `upload_private_file` / `upload_group_file`（先上传拿 `file_id`，再发文件消息），这条路径比直接把文件当消息段塞进 `send_private_msg` 更容易成功。如果仍然被拒，通常是协议端或账号侧的风控限制，可以：

- 检查协议端是否支持私聊文件（部分实现只支持群文件）；
- 在 AstrBot 配置 `callback_api_base`，让它改发临时链接；
- 或把 `send_file` 关掉，只回下载链接。

### 收不到文件

见「环境要求 → QQ 协议端」。群聊还要保证群允许成员上传文件。

### 下载链接获取失败

多半是当日下载额度用完（免费账号每天有限额），第二天再试或升级账号。

## 兼容性

在 AstrBot **4.29.x** 上测试通过。老版本 AstrBot 缺少 `GreedyStr` 时插件会自动降级（不再因导入失败而加载不了），此时多词查询会从原始消息文本里补全，因此 `找书 数据结构与算法 (C++版)` 这类带空格、括号的查询依然可用。

## 说明

插件通过 Z-Library 网页版接口（`/eapi/*`）以你自己的账号登录、搜索和下载。请遵守 Z-Library 的使用条款、注意账号每日下载限额，仅用于个人学习用途。

下载的书籍暂存在 `data/plugin_data/astrbot_plugin_zlibrary/downloads/`，24 小时后自动清理。

## 许可

MIT
