# SnapStudy · 截屏识题学习助手

> 屏幕上的题目自动识别、AI 即时给出解析，手机浏览器实时同步 —— 让学习不断流。

## 为什么做这个项目

在刷题、看网课、做练习时遇到不会的题目，传统流程是：

```text
拿起手机 → 打开搜题 App → 拍照 → 等待识别 → 查看解析
```

整个过程频繁打断学习节奏。本项目把这条链路压缩成一步：

**程序自动截取屏幕上的题目 → AI 即时给出解析 → 手机浏览器实时显示**。

题目在哪，解析就在哪，无需离开当前学习界面。

## 功能特性

- **自动截屏识别**：按设定间隔自动截屏，屏幕内容无变化时自动跳过，避免重复请求、节省 Token
- **多模态模型解析**：调用视觉大模型识别题目并给出解析
- **手机实时同步**：通过 WebSocket 推送到手机浏览器，电脑在做什么，手机就能看到解析
- **手动触发**：手机端可随时手动发起一次识别
- **历史记录**：默认保留最近 200 条解析结果，可随时回看、一键清空
- **配置热更新**：网页端即可修改模型配置并立即生效
- **广泛兼容**：支持任意 OpenAI 兼容 API，以及 MiniMax Token Plan 的 VLM 专用端点
- **视觉能力提醒**：检测到模型未识图时主动告警，避免使用纯文本模型导致误判

## 工作原理

```text
┌─────────────┐   截屏    ┌──────────────┐   Base64   ┌─────────────┐
│  屏幕内容    │ ────────> │ capture.py   │ ─────────> │ llm_client  │
└─────────────┘           └──────────────┘            └──────┬──────┘
                                                             │ 解析结果
                                                             v
┌─────────────┐  WebSocket  ┌──────────────┐            ┌─────────────┐
│ 手机浏览器   │ <────────── │ server/app.py│ <───────── │  历史记录    │
└─────────────┘             └──────────────┘            └─────────────┘
```

## 环境要求

- **Windows**
- **Python 3.10+**（安装时建议勾选 `Add python.exe to PATH`）
- 一个支持图片识别的多模态大模型 API Key

> **请注意**：如果 `python --version` 没有任何输出，说明该命令被 Microsoft Store
> 的 0 字节占位程序（`%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe`）拦截了。
> 这不影响本项目 —— `start.bat` 会自动跳过它，找到真正的 Python。

## 快速开始

### 一键启动

**双击 `start.bat`** 即可，全程无需命令行操作。

它会自动完成以下流程，可重复运行，已完成的步骤会自动跳过：

```text
1/5  校验 Python 3.10+
2/5  创建 / 复用项目级虚拟环境 .venv
3/5  校验并安装 requirements.txt 中的依赖
4/5  从 config.example.yaml 生成 config.yaml
5/5  启动服务
```

> 首次运行时，创建虚拟环境与下载依赖可能耗时几分钟，窗口里会持续有输出，请耐心等待；已完成的步骤在后续启动会自动跳过。

**填写 API Key**：首次运行会自动生成 `config.yaml`，用记事本打开并填入你的密钥：

```yaml
llm:
  api_base: https://api.deepseek.com/v1   # 任意 OpenAI 兼容地址
  api_key: your-api-key-here              # 换成你自己的 Key
  model: deepseek-flash                   # 必须是视觉模型
```

> `config.yaml` 已被 `.gitignore` 忽略，不会被上传到仓库，请放心填写。也可以启动后在手机网页端修改。

启动成功后窗口会打印访问地址（默认端口 `8765`，被占用时自动向后探测）：

```text
服务已启动，手机浏览器访问: http://192.168.1.100:8765
```

关闭窗口或按 `Ctrl + C` 即可停止服务。

### 手机端使用

确保手机与电脑处于**同一局域网**，用手机浏览器打开终端打印的地址即可（以终端实际打印的为准，端口可能因自动避让而与配置值不同）：

- 点击「自动截屏」开始持续识别
- 点击「截屏一次」手动触发
- 解析结果通过 WebSocket 实时推送显示

## 配置说明

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `capture.interval` | 自动截图间隔（秒） | `10` |
| `capture.quality` | JPEG 压缩质量 | `60` |
| `capture.region` | 截屏区域 `[left, top, width, height]`，不填为全屏 | 全屏 |
| `history.max_records` | 历史记录保留条数 | `200` |
| `llm.api_base` | OpenAI 兼容 API 地址，程序会在其后拼接 `/chat/completions` | `https://api.deepseek.com/v1` |
| `llm.api_key` | API 密钥，**必须替换成自己的** | `your-api-key-here` |
| `llm.model` | 模型名称，**必须是支持图片输入的视觉模型** | `deepseek-flash` |
| `llm.system_prompt` | 系统提示词，用于约束输出格式 | 见 `llm_client.py` |
| `server.host` | 监听地址 | `0.0.0.0` |
| `server.port` | 监听端口，**被占用时自动向后探测** | `8765` |

> 内置默认的 `llm.model` 是 `deepseek-flash`（支持图片输入），但仍请按下方表格确认你的服务商与可用模型名。

### 推荐的视觉模型

| 服务商 | 模型名示例 | `api_base` 示例 |
| --- | --- | --- |
| OpenAI | `gpt-5.6-terra` | `https://api.openai.com/v1` |
| DeepSeek | `deepseek-flash` | `https://api.deepseek.com/v1` |
| 通义千问 | `qwen3-vl-plus` | 阿里云百炼 OpenAI 兼容地址 |
| 智谱 AI | `glm-4.6v` | 智谱 OpenAI 兼容地址 |
| 豆包 | `doubao-seed-1.6-vision` | 火山方舟 OpenAI 兼容地址 |
| Moonshot / Kimi | `kimi-k2.6` | `https://api.moonshot.cn/v1` |
| SiliconFlow | `Qwen/Qwen3-VL-235B-A22B-Instruct` | `https://api.siliconflow.cn/v1` |
| MiniMax | Token Plan VLM 端点 | `https://api.minimax.chat/v1` |

> 模型名会随各家厂商迭代变化，上表仅作起点，请以对应官方文档为准。

#### DeepSeek

DeepSeek 已提供支持图片输入的 `deepseek-flash`：

```yaml
llm:
  api_base: https://api.deepseek.com/v1
  model: deepseek-flash
```

图片以 `data:image/jpeg;base64,...` 内联传入（本项目即采用这种方式）；`image_url.detail` 支持 `low` / `high` / `original` / `auto`。注意**图片只能出现在 user 消息中**，放进 system 或 assistant 消息会返回 400。

> 旧模型名 `deepseek-v4-flash-vision-exp` 已下线：调用仍会被接受，但请求实际由最新的 Flash 模型承接，新接入请直接使用 `deepseek-flash`。

#### MiniMax

`api_base` 填 `https://api.minimax.chat/v1` 即可。程序会识别 MiniMax 域名并自动改走 Token Plan 的 VLM 专用端点（`/v1/coding_plan/vlm`），无需额外配置。

> 纯文本模型无法识别截图，程序会在检测到未识图时给出提示。

## 项目结构

```text
.
├── main.py                 # 程序入口
├── config.py               # 配置加载与持久化
├── capture.py              # 屏幕截图模块（含内容变化检测）
├── llm_client.py           # LLM 客户端（OpenAI 兼容 + MiniMax VLM）
├── config.example.yaml     # 配置模板
├── requirements.txt        # 依赖清单
├── launcher.py             # 启动器内部实现（由 start.bat 调用）
├── start.bat               # 唯一启动入口（双击即可）
└── server/
    ├── app.py              # FastAPI 服务 + WebSocket 推送
    └── templates/
        └── index.html      # 手机端页面
```

## 常见问题

**Q：模型回复“没有收到图片”？**

说明当前 `llm.model` 不是视觉模型。请更换为支持图片识别的多模态模型。

**Q：双击 `start.bat` 提示找不到 Python？**

先确认已安装 Python 3.10+。若 `python --version` 没有任何输出，说明 PATH 里的 `python`
被 Microsoft Store 的 0 字节占位程序拦截了；`start.bat` 已能自动跳过它。若仍找不到，
请重新安装 Python 并勾选 `Add python.exe to PATH`。详见上方「环境要求」。

**Q：端口被占用了怎么办？**

默认端口为 `8765`（已避开 8000 / 8080 / 3000 等常用端口）。若该端口仍被占用，程序会**自动向后探测可用端口**并在终端打印实际地址，以打印出来的地址为准即可；也可以在 `config.yaml` 中通过 `server.port` 手动指定，或关闭占用该端口的程序。

**Q：手机打不开页面？**

检查手机与电脑是否在同一局域网、电脑防火墙是否放行该端口、`server.host` 是否为 `0.0.0.0`。

**Q：自动截屏不触发识别？**

程序会在屏幕内容与上一次完全一致时跳过请求，这是节省 Token 的正常行为；屏幕内容变化后会自动继续。

## 免责声明

本项目仅供**学习交流与技术研究**所用：

- 请勿将本项目用于任何违反法律法规、校规校纪或考试纪律的场景，由此产生的一切后果由使用者自行承担；
- 本项目不鼓励、不支持任何形式的学术不端行为；
- 一经下载、克隆或使用本项目，即视为已阅读并同意上述条款。

## License

[MIT](LICENSE)
