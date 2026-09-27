# Coding Plan 额度查询

一个 Windows 桌面小工具，把你在各家 AI 编程订阅里的**额度窗口**和**本机 token 消耗**汇总到一个界面里。

- **云端额度**：OpenCode Go（opencode zen）、Command Code、Cline Pass、阶跃星辰 StepFun、Grok Build
- **本机用量**：dsh 账本（`ledger.json`）、Grok 本机会话库、opencode 本地库
- 单文件 exe、双击即用、无需安装 Python
- 密钥只存在你自己的电脑上（`%APPDATA%\OpenCodeGoChecker\`），不经过任何第三方服务器

> 这是一个**本地工具**：它直接调用各平台的官方接口，你的 API Key 只在你本机和官方服务器之间传输。

---

## 快速开始

### 方式一：下载现成的 exe（推荐给普通用户）

到 [Releases](../../releases) 下载 `OpenCodeGoChecker.exe`，双击运行。

**运行要求**：Windows 10/11。需要 [Microsoft Edge WebView2 运行时](https://developer.microsoft.com/microsoft-edge/webview2/)（Windows 11 自带；Windows 10 若提示缺少，装一次即可）。

首次打开时没有任何密钥，点左侧的 **+** 添加即可。

### 方式二：从源码运行

```bat
git clone <this-repo>
cd OpenCodeGoChecker
pip install -r requirements.txt
python opencode_go_checker.py
```

### 方式三：自己打包 exe

```powershell
pip install -r requirements.txt pyinstaller
pwsh -File build.ps1
# 产物：dist\OpenCodeGoChecker.exe
```

---

## 添加密钥

点左侧 **+**，选择供应商，粘贴对应的 API Key：

| 供应商 | Key 从哪来 |
|---|---|
| OpenCode Go | opencode zen 控制台 |
| Command Code | commandcode.ai 账户页 |
| Cline Pass | cline.bot 账户页 |
| 阶跃星辰 StepFun | platform.stepfun.com |
| Grok Build | **不需要 Key** —— 它读你本机的 Grok 登录态和会话库 |

---

## 本机用量显示为 0？看这里

「本机用量」来自你电脑上的其他工具的目录。**别人的电脑上这些目录位置和你不一样**，所以程序会自动探测，探测不到时可以手动指定。

打开 **设置（左下齿轮）→ 数据源路径**，面板里会直接告诉你每个数据源是否找到：

| 数据源 | 默认位置 | 说明 |
|---|---|---|
| **dsh 账本** | `<dsh_home>/storages/cost-meter/ledger.json` | 由 `dsh-cost-meter` 插件写入，**按日最精确** |
| **dsh 会话缓存** | `<dsh_home>/storages/session_projcache/` | dsh 自带，**不需要任何插件**；账本不存在时自动用它兜底 |
| **Grok 会话库** | `~/.grok/sessions` | Grok CLI / Grok Build 的会话目录 |
| **opencode 本地库** | `~/.local/share/opencode/opencode.db`、`%APPDATA%\opencode\opencode.db` 等 | opencode 的 SQLite 库 |

`<dsh_home>` 的候选：`~/.dsh`、`%APPDATA%\dsh`、`%LOCALAPPDATA%\DeepSeekHarness\data`、`%LOCALAPPDATA%\dsh`、`<各盘符>:/DeepSeekHarness/data`。程序也会读取 `DSH_HOME`、`DSH_DATA_DIR`、`DSH_PROFILE_DIR`、`GROK_HOME`、`OPENCODE_DATA`、`XDG_DATA_HOME`。

### 没装 dsh-cost-meter 插件也能用

这是**重点适配过的场景**。dsh 的费用账本 `ledger.json` 是 `dsh-cost-meter` 插件写的；没装这个插件的机器上它不存在。

旧版本此时本机用量全是 0。现在会自动回退到 **dsh 自己的会话缓存** `storages/session_projcache/`（里面每个会话都带 `costUsage`，字段结构与账本完全一致），因此：

- **合计是准确的** —— token、缓存、费用都能算出来；
- **按日归属是近似的** —— 以「会话创建日」为准，一个跨天的长会话会整段计入创建日；
- 界面会用醒目的 **「dsh会话缓存」** 徽章和一行说明标注这一点，设置页也会显示「⚠ 未检测到 dsh-cost-meter 插件」。

装了插件之后，程序会自动改用账本，按日数据就是精确的。

**这些数据源全都是可选的。** 一个都没有也能正常用 —— 云端额度部分照样工作，本机用量部分会明确告诉你「未找到」，不会假装是 0。

---

## 常见问题

**Q：点「立即查询」一直转圈？**
A：已修复。旧版本的根因是：只要接口或本地文件里出现一个 `NaN` / `Infinity`，Python 回传给界面的 JSON 就会包含非法字面量，前端 `JSON.parse` 抛错，而这个错误发生在 Promise 回调被删除之后 —— 于是 Promise 永不返回，按钮永久卡住，且因为一次返回包含全部密钥，**所有渠道都会一起卡死**。现在后端对每个返回值做净化，前端每个调用都有超时兜底，任何情况下按钮都会恢复。

如果仍然转圈超过 2 分钟，界面会自己提示超时；此时请到 **设置 → 数据源路径** 检查路径，或查看日志（见下）。

**Q：程序双击没反应 / 打开就退出？**
A：看日志 `%APPDATA%\OpenCodeGoChecker\startup.log`。最常见的原因是缺少 WebView2 运行时。

**Q：我的密钥存在哪？会不会被上传？**
A：只存在 `%APPDATA%\OpenCodeGoChecker\opencode_go_keys.json`，是本机文件，程序不会上传到任何地方。本仓库的 `.gitignore` 也把它排除在外，并带一个提交前自检脚本：

```bat
python tools\check_secrets.py --strict
```

**Q：能不能在 macOS / Linux 上跑？**
A：界面层用的是 pywebview，理论可行，但目前只在 Windows 上验证过（图标、DPI、WebView2 都是 Windows 专用分支）。

---

## 数据口径说明

- **额度窗口**（5 小时 / 每周 / 每月）来自各平台官方接口，是**账号级**的。
- **本机用量**只统计你这台机器上的记录。账号在多台设备共用、或本地会话库被清理过时，两者对不上是正常的，界面会如实标注。
- **按日归属统一用「本地日期」**：dsh 账本的日键是 `dsh-cost-meter` 插件用本地时间写的（`localDayKey()`），Grok 会话库、dsh 会话缓存同理；Cline 官方接口给的是 UTC 时间戳，程序会换算成本地日期再入库，这样三个源合并成一张趋势图时口径一致。
  程序会**从账本自身的会话时间戳自动判定**它用的是本地还是 UTC 口径，不靠硬编码——历史账本被迁移过也不会错位。
- 费用换算：StepFun 按官方人民币定价 + 实时汇率折算；其他渠道按官方美元定价表估算。账本里有实际计费金额时以账本为准，估算值会标 `cost_est`。

## 安全说明

这是一个**密钥管理工具**，所以做了几件额外的事：

- 界面里所有来自外部的字符串（模型名、供应商名、账户名、远端报错原文、账本键名）在拼进 HTML 前都会转义，避免被构造成可执行脚本；
- 密钥只写 `%APPDATA%`，仓库里有 `.gitignore` 与提交前自检脚本；
- 导出备份的文件名（`opencode_keys_backup_<日期>.json`）也在 `.gitignore` 里——**如果你手动导出过备份，请不要把它放进仓库目录再 commit**。

---

## 项目结构

```
opencode_go_checker.py        主程序（单文件，含界面）
assets/app.ico                应用图标
assets/tailwindcss.min.js     界面样式（构建时内联进 exe）
OpenCodeGoChecker.spec        PyInstaller 打包配置（相对路径）
build.ps1                     一键构建 + 冒烟测试
tools/check_secrets.py        提交前密钥泄露自检
.github/workflows/release.yml 打 tag 自动构建并发布 exe
```

---

## 参与贡献

欢迎提 Issue 和 PR。提交前请先跑一次：

```bat
python tools\check_secrets.py --strict
```

**请不要提交任何真实密钥、`%APPDATA%` 下的运行数据、或你本机的绝对路径。**

---

## 许可证

[MIT](LICENSE)
