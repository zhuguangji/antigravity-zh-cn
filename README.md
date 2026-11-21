# Antigravity 2.34.0 Windows 简体中文补丁

## 补丁内容

- [桌面补丁](01-desktop.zh-CN.json)适用于 `resources/app.asar`，覆盖窗口菜单、
  启动及退出提示、更新对话框、欢迎向导、文件选择器，以及部分 SSH 和通行密钥提示。
- [主界面补丁](02-interface.zh-CN.json)适用于 `resources/bin/language_server.exe`
  内置的网页资源，覆盖主界面、会话和项目操作、常用设置、工具提示、通知、
  登录界面及部分报错。界面布局的名称和说明在显示时翻译，原有设置值仍用原标识符。

两个 JSON 以资源文件名分组，每项依次记录原始 UTF-8 字节偏移、原文片段、译文片段。
部分占位符分隔的片段分别列出，原有变量表达式不会被替换。
项目中的 Token、API、MCP、SSH、WSL、Git 和品牌名称按原术语保留。

## 安装或恢复

下载仓库后，在 PowerShell 中进入仓库目录。需要 Python 3.11 或更高版本。
安装时，脚本会优先使用本机已有的 7-Zip。安装和恢复前均需彻底退出 Antigravity。
脚本会校验原始 2.34.0 文件的 SHA-256。

安装补丁：

```powershell
python .\antigravity-zh-CN.py apply
```

恢复原版：

```powershell
python .\antigravity-zh-CN.py restore
```

备份位于安装目录下的 `zh-CN-backup-2.34.0` 文件夹。
其他安装路径可在命令末尾追加 `--install "完整安装路径"`。
原始文件校验失败时脚本会停止。Antigravity 更新后需使用对应版本的补丁。

## 覆盖范围

此补丁来自安装版资源。部分低频页面、Windows / Electron 自带文案、第三方页面、
远程主机产生的消息及服务端动态文本仍可能显示英文。
