# 插件开发约定

本仓库是独立的 MaiBot SDK 2.x 插件，不是 MaiBot 主程序。运行入口和 Hook 在 `plugin.py`，Bilibili 链接、yt-dlp 和字幕处理在 `bilibili.py`，OSS 与 Fun-ASR 在 `cloud.py`。先阅读 `README.md` 中的安装、工作流程和验证边界；不要为了插件修复直接修改 MaiBot 主程序。

## 版本与配置

- 准备发布功能或修复时，同步更新 `_manifest.json` 与 `pyproject.toml` 的插件发行版本，以及 `README.md` 中的版本示例；运行 `uv lock` 更新 `uv.lock`，不要手改锁文件，也不要无意升级依赖。
- `[plugin].config_version` 是配置结构版本，不随代码发行版本自动变化。仅在配置字段、类型、含义或 TOML 分区变化时按配置迁移流程处理；普通 Bug 修复保持现有版本和用户的 `config.toml` 不变。
- `config.toml` 含 Cookie 和云服务密钥，不能提交、复制到测试样例或打印到日志。`bin/` 中的 yt-dlp、FFmpeg 由部署者提供，不提交到 Git。

## 验证与交付

- 本地运行 `uv sync --dev`、`uv run pytest -q`、`uv run ruff check .`、`uv lock --check` 和 `git diff --check`；为失败路径增加能区分正确行为与错误实现的测试。
- 单元测试不能代替 MaiBot Host、QQ/NapCat、Maisaka、OSS 与 Fun-ASR 的服务器联调。推送 Git 仓库不等于服务器插件更新或插件市场立即同步；报告时区分本地修改、提交、推送和实际部署状态。
- 要在新版插件市场收录可安装版本，代码推送后还需发布 GitHub Release：Tag 使用 `vX.Y.Z` 或 `X.Y.Z`，指向 `_manifest.json` 中版本号一致的提交。只推送分支会在定时索引后更新旧版展示信息，不会生成发布版本记录；详见 [插件中心的版本规则](https://github.com/Mai-with-u/plugin-repo/blob/main/VERSIONING.md)。创建 Release 是对外发布，须先取得明确授权。
- 不要在诊断时暴露服务器配置或聊天记录中的凭据。未经明确授权，不覆盖服务器插件、不重启运行中的 MaiBot，也不手动触发插件市场或其他共享服务的更新流程。
