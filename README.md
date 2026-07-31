# Bilibili 视频信息与 AI 总结

MaiBot SDK 2.x 插件。它会在所有被 MaiBot 适配器策略放行的群聊中识别 Bilibili 视频链接，阻止原有回复链，并发送视频元数据和不超过 300 字的 AI 总结。

## 工作流程

1. 识别普通 BV/AV 链接、移动端链接、`b23.tv` / `bili2233.cn` 短链、App 分享文本和番剧 `ep` 链接。
2. 使用插件 `bin/` 目录中的 yt-dlp 读取标题、简介、作者、发布时间、时长、封面和字幕。
3. 元数据一取得就发送“封面 + 文本”，随后发送“AI总结中”。不会额外发送“视频解析中”。
4. 优先使用中文人工字幕、中文自动字幕、其他语言人工字幕、其他语言自动字幕。
5. 没有可用字幕时下载 MP3，上传到私有 OSS，并通过异步 `fun-asr` 转写。
6. 使用 MaiBot 主程序公开的 `utils` LLM 任务判断字幕/转写是有效内容、部分有效、纯音乐/BGM，还是无意义的识别结果。无有效语音时丢弃转写，以自然措辞概览标题和简介，不在总结中回显 ASR、资料不足等内部处理过程；部分有效时只采用可靠内容。
7. LLM 在最终生成总结时再次复核内容有效性，并输出简体中文纯文本总结。
8. ASR 完成后删除 OSS 对象；本地 Cookie、音频和其他临时文件也会立即删除。

触发消息会由插件写入 Maisaka 上下文。元数据、状态、失败说明和最终总结使用 `storage_message=true` 与 `sync_to_maisaka_history=true` 发送。

## 安装

将本目录放入 MaiBot 的第三方插件目录，然后下载与操作系统及 CPU 架构匹配的预编译文件：

- yt-dlp：[官方 Releases 最新版](https://github.com/yt-dlp/yt-dlp/releases/latest)；Windows 通常下载 `yt-dlp.exe`。
- FFmpeg：[官方下载页](https://ffmpeg.org/download.html)；FFmpeg 官网会按系统列出源码和可信的预编译构建提供方。

请只从上述官方页面或 FFmpeg 官方列出的构建提供方下载。解压后，将可执行文件放入插件目录的 `bin/`：

```text
bilibili-video-info/
└── bin/
    ├── ffmpeg.exe    # Windows
    └── yt-dlp.exe
```

Linux/macOS 使用无扩展名的 `bin/ffmpeg` 与 `bin/yt-dlp`，并自行添加可执行权限：

```bash
chmod +x bin/ffmpeg bin/yt-dlp
```

插件只检查并使用 `ffmpeg` 和 `yt-dlp`；FFmpeg 压缩包中附带的 `ffprobe` 不是本插件的必需文件。插件不会下载或更新这些二进制，也不会在非 Windows 系统上尝试执行 `.exe` 文件。`bin/` 已加入 `.gitignore`，请勿将二进制提交到 Git。

放置后可先确认二进制能够正常运行。Windows PowerShell：

```powershell
.\bin\yt-dlp.exe --version
.\bin\ffmpeg.exe -version
```

Linux/macOS：

```bash
./bin/yt-dlp --version
./bin/ffmpeg -version
```

Python 依赖已在 Manifest v2 中声明，MaiBot Host 会安装 `httpx` 和 `oss2`。独立先期验证可使用：

```bash
uv sync --dev
uv run pytest -q
uv run ruff check .
```

## 配置

先复制示例配置，再编辑插件目录中的 `config.toml`：

```bash
cp config.example.toml config.toml
```

Windows PowerShell：

```powershell
Copy-Item config.example.toml config.toml
```

### 插件版本与配置版本

插件发行版本与配置结构版本相互独立：

- `_manifest.json` 和 `pyproject.toml` 中的版本表示插件代码发行版本。
- `[plugin].config_version` 表示 `config.toml` 的结构版本，用于配置兼容与迁移。

功能改进、Bug 修复和提示词调整通常只提升插件发行版本。只有新增、删除或重命名配置字段，或者修改字段类型、含义或 TOML 分区时，才提升 `config_version`。因此插件 v0.1.3 继续使用 `config_version = "0.1.0"` 是预期行为，现有配置无需迁移。

### Bilibili Cookie

直接粘贴浏览器复制出的单行 Cookie 请求头：

```toml
[bilibili]
cookie = "buvid3=...; SESSDATA=...; bili_jct=...; DedeUserID=..."
```

留空时匿名访问。插件只会将 Cookie 发送给 `bilibili.com` 及其子域，不会随字幕重定向发送到 CDN 或其他站点。Cookie 运行时会转换成权限受限的临时 Netscape 文件，且不会写入日志或聊天上下文。

### 百炼 Fun-ASR 与 OSS

```toml
[aliyun]
region = "cn-beijing" # 或 ap-southeast-1
workspace_id = "百炼 Workspace ID"
api_key = "对应地域的百炼 API Key"
oss_endpoint = "https://oss-cn-beijing.aliyuncs.com"
oss_bucket = "私有 Bucket 名称"
oss_access_key_id = "OSS AccessKey ID"
oss_access_key_secret = "OSS AccessKey Secret"
oss_object_prefix = "maibot-bilibili-video-info"
signed_url_ttl_seconds = 7200
asr_timeout_seconds = 1800
asr_poll_interval_seconds = 3
```

插件使用稳定模型名 `fun-asr`，通过 `POST /services/audio/asr/transcription` 提交异步任务并轮询结果。提交私有 OSS 签名 URL 时会启用百炼的 OSS 资源解析，并保留对象路径中的斜杠以提高下载兼容性。OSS 身份至少需要目标前缀的 `PutObject`、`GetObject` 和 `DeleteObject` 权限。

请额外为 `oss_object_prefix` 配置短期生命周期规则（建议 1 天后自动删除），用于清理进程崩溃或持续网络故障下无法主动删除的极少数残留对象。

> `config.toml` 中的 Cookie、API Key 和 OSS 密钥是明文。该文件已加入 `.gitignore`，仍请限制文件权限，不要强制提交填写过凭据的配置文件。

### 处理限制与总结

默认值：

- 最长视频 60 分钟；超限时仍发送元数据，但不执行总结。
- 全局并发 2，同群并发 1。
- 同群同视频 10 分钟内去重。
- 群聊展示的简介最多 200 字，传给 LLM 的仍是完整简介。
- 最终总结最多 300 个 Unicode 字符。
- 字幕/转写会先由 LLM 判断是否确有可总结的语音内容；纯音乐、BGM、噪声、重复拟声和明显 ASR 幻觉不会作为视频观点。
- 长字幕/转写按 12000 字分段提炼后再汇总。

对应配置位于 `[plugin]` 和 `[summary]`。

## 生效范围

插件自身没有群黑白名单，只处理群聊，不处理私聊。MaiBot `adapter_policy.toml` 中的群黑白名单在消息进入插件 Hook 前执行，因此仍然有效，插件不会绕过主程序的适配器策略。

一条消息只处理第一个有效视频链接，不批量处理合集、系列、收藏夹或番剧季度页。普通多 P 链接未指定 `p` 时按 yt-dlp 默认处理 P1；需要其他分 P 时请发送带 `?p=N` 的具体链接。

## 失败与降级

- 字幕读取失败：改用音频与 Fun-ASR。
- 音频下载、OSS 或 ASR 失败：明确提示，并仅根据标题和简介生成概览。
- Fun-ASR 返回内部服务错误：自动重新提交一次；最终失败日志会包含脱敏后的子任务错误码、Task ID 和 Request ID，方便进一步排查。
- 字幕或 ASR 结果被 LLM 判定为纯音乐/BGM 或无意义内容：不采用该文本，只根据标题和简介生成带信息边界的简短概览。
- LLM 或元数据解析失败：发送简化错误，不回显 Cookie、API Key、签名 URL 等敏感信息。
- 会员、付费、充电、地区限制或已删除视频能否处理取决于 Cookie 对应账号权限和 yt-dlp 支持情况。

## 验证边界

本仓库只进行插件级单元测试、SDK 声明检查和 yt-dlp 公开样例元数据探测。群消息拦截、平台发送、Maisaka 上下文同步、主程序 LLM、OSS 与 Fun-ASR 的完整联调需在实际 MaiBot 部署中人工验证。
