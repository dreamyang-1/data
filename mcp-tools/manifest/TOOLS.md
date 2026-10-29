# YoAgent MCP 工具扩展清单（一期）

> 本文档基于 `/root/yyy/YoAgent工具扩展清单（一期）(1).xlsx` 整理，记录每个工具的安装状态、启动方式、外部依赖与 TODO 项。

## 目录结构

```
/root/yyy/mcp-tools/
├── npm/                # npm 包工作区（package.json，npx 即用无需 install）
├── pip/                # pip 安装目录（PYTHONPATH 入口）
│   ├── imagesorcery_mcp/
│   ├── mcp_run_python/   # 替代空壳的 mcp_run_isolated_python
│   └── mcp_email.py
├── source/             # git clone 的源码仓库
│   ├── email-mcp-diskd/        # TypeScript，已 build 出 dist/main.js
│   └── onlyoffice-mcp-server/  # Python，pip install -e 注册
├── wrappers/           # 自封装 MCP
│   └── office_mcp.py   # LibreOffice + Pandoc
├── configs/
│   └── mcp_servers.json  # Claude Desktop / Cursor / VS Code 通用配置
├── start.sh            # 启动脚本：./start.sh <tool> [stdio|http] [port]
└── manifest/
    └── TOOLS.md        # 本文档
```

## 安装状态总览

> smoke test 实测：8 ✅ 真可用 + 2 ⚠️ 待配置 + 2 ❌ 不可用 = 12 个已 build 完成
>
> **去重策略**：每个大类里同功能 MCP 只保留 1 个"主推可用"，其他同功能的标 🔁（冗余备份）或 ❌（已覆盖）。

### 按类别去重视图（每类 1 个主推即可）

| # | 类别 | 主推 ⭐ | 备选 🔁 | 不可用/未装 |
|---|------|--------|---------|-------------|
| 1 | **文档自动化** | ⭐ **office-mcp**（LibreOffice+Pandoc 转换） | 🔁 onlyoffice-mcp-server（Python 编辑 DOCX/XLSX/PPTX，功能互补不重叠） | ℹ️ python-docx 等：已通过 office-mcp 覆盖 |
| 2 | **浏览器自动化** | ⭐ **playwright-mcp** | - | ❌ mcp-web-engine |
| 3 | **图片处理** | ⭐ **mcp-image-optimizer**（npm，5 个 resize/crop/format tool 最简） | 🔁 imagesorcery-mcp（含 OCR+YOLO 视觉特色，非纯冗余）<br>🔁 image-processing-mcp-server（功能完全重叠 mcp-image-optimizer，**建议直接卸载**） | ❌ glm4v-vision-mcp<br>❌ vision-foundation-mcp |
| 4 | **邮件管理** | ⭐ **mcp-email**（Python，MCP server 可启动不报错，但收发邮件仍需邮箱凭据） | 🔁 email-mcp-server（npm，启动就报错需 2 env，tool 多 4×）<br>🔁 email-mcp-diskd（启动就报错需 4 env，tool 多 10×，重度自动化） | ❌ mbsync-mcp |
| 5 | **代码沙箱** | ⭐ **mcp-run-python** | - | ❌ codemode-lite<br>❌ ~~mcp-run-isolated-python~~（空壳）<br>❌ container-mcp<br>❌ OnlyBoxes |
| 6 | **HTML 原型** | ⛔ 全军覆没 | - | ❌ ~~ui-mcp~~（@forgespace 停维）<br>❌ ~~uiforge-mcp~~（同上）<br>❌ AI-Diagram-Prototype-Generator<br>❌ modao-proto-mcp |
| 7 | **其他** | ℹ️ Pandoc MCP Server（已通过 office-mcp 覆盖） | - | ❌ mcp-skills-registry |

**类别可用率**：7 大类中 **6 类有可用 MCP**（85%），1 类全军覆没（HTML 原型）。

### 完整工具清单

| # | 类别 | 工具 | 状态 | 启动方式 |
|---|------|------|------|---------|
| 1 | 文档自动化 | ⭐ **office-mcp** (LibreOffice+Pandoc) | ✅ 真可用 | `./start.sh office-mcp` |
| 2 | 文档自动化 | 🔁 **onlyoffice-mcp-server** | ✅ 真可用（功能互补：编辑而非转换） | `./start.sh onlyoffice-mcp` |
| 3 | 文档自动化 | python-docx/openpyxl/python-pptx | ℹ️ 已通过 office-mcp 覆盖 | - |
| 4 | 浏览器自动化 | ⭐ **playwright-mcp** | ✅ 真可用 | `./start.sh playwright-mcp` |
| 5 | 浏览器自动化 | mcp-web-engine | ❌ 未装 | TODO |
| 6 | 图片处理 | ⭐ **mcp-image-optimizer** | ✅ 真可用（最简） | `./start.sh mcp-image-optimizer` |
| 7 | 图片处理 | 🔁 **imagesorcery-mcp** | ✅ 真可用（特色：OCR+YOLO） | `./start.sh imagesorcery-mcp` |
| 8 | 图片处理 | 🔁 image-processing-mcp-server | ✅ 真可用（**与 #6 完全重叠，建议卸载**） | `./start.sh image-processing-mcp-server` |
| 9 | 图片处理 | glm4v-vision-mcp | ❌ 未装 | TODO |
| 10 | 图片处理 | vision-foundation-mcp | ❌ 未装 | TODO |
| 11 | 邮件管理 | ⭐ **mcp-email** (Python) | ✅ 真可用（MCP server 可启动，调用邮件功能时仍需凭据） | `./start.sh mcp-email` |
| 12 | 邮件管理 | 🔁 **email-mcp-diskd** | ⚠️ 需 4 env（47 tools 重度自动化） | `MCP_EMAIL_ADDRESS=xxx ... ./start.sh email-mcp-diskd` |
| 13 | 邮件管理 | mbsync-mcp | ❌ 未装 | TODO |
| 14 | 邮件管理 | 🔁 **email-mcp-server** | ⚠️ 需 2 env（18 tools，Gmail 友好） | `EMAIL_ADDRESS=xxx EMAIL_PASSWORD=xxx ./start.sh email-mcp-server` |
| 15 | 代码沙箱 | codemode-lite | ❌ 未装 | TODO |
| 16 | 代码沙箱 | ⭐ **mcp-run-python** | ✅ 真可用（替代空壳 #17） | `./start.sh mcp-run-python` |
| 17 | 代码沙箱 | ~~mcp-run-isolated-python~~ | ❌ 空壳包 | 已替换为 #16 |
| 18 | 代码沙箱 | container-mcp | ❌ 未装 | TODO |
| 19 | 代码沙箱 | OnlyBoxes | ❌ 未装 | TODO |
| 20 | HTML 原型 | ~~uiforge-mcp~~ | ❌ 不可用（@forgespace 停维 2026-04-20） | TODO |
| 21 | HTML 原型 | ~~ui-mcp~~ (@forgespace) | ❌ 不可用（同上） | TODO |
| 22 | HTML 原型 | AI-Diagram-Prototype-Generator | ❌ 未装 | TODO |
| 23 | HTML 原型 | modao-proto-mcp | ❌ 未装 | TODO |
| 24 | 其他 | Pandoc MCP Server | ℹ️ 已通过 office-mcp 覆盖 | - |
| 25 | 其他 | mcp-skills-registry | ❌ 未装 | TODO |

### 状态统计

| 状态 | 数量 | 工具 |
|------|------|------|
| ⭐ 主推真可用 | **5** | office-mcp, playwright-mcp, mcp-image-optimizer, mcp-email（MCP server 可启动，邮件操作仍需凭据）, mcp-run-python（**5 大类最小可用集**） |
| 🔁 备选真可用 | **3** | onlyoffice-mcp（功能互补）, imagesorcery-mcp（特色 OCR）, email-mcp-server/diskd（看场景） |
| ⚠️ 备选需 env | **2** | email-mcp-server, email-mcp-diskd |
| ❌ 不可用 | **2** | ui-mcp, uiforge-mcp |
| ❌ 未装 | **11** | mcp-web-engine, glm4v-vision-mcp, vision-foundation-mcp, mbsync-mcp, codemode-lite, mcp-run-isolated-python（空壳）, container-mcp, OnlyBoxes, AI-Diagram-Prototype-Generator, modao-proto-mcp, mcp-skills-registry |
| ℹ️ 已覆盖 | **2** | python-docx 等, Pandoc MCP Server |

**build 完成率**：12/14 = 86%（14 个找到源码，12 个 build 成功）
**真正可用率**：8/14 = 57%
**类别覆盖率**：6/7 = 86%（HTML 原型全军覆没）

### 最小可用集（5 个工具覆盖 5 大类）

如果你只想要"够用就行"，启动这 5 个就够了：

```bash
./start.sh office-mcp            # 文档转换（docx/xlsx/pptx → pdf/html）
./start.sh playwright-mcp        # 浏览器（截图/PDF/SPA 抓取）
./start.sh mcp-image-optimizer   # 图片（resize/crop/水印/格式）
./start.sh mcp-email             # 邮件 MCP server（IMAP/SMTP，调用邮件功能时需配置邮箱凭据）
./start.sh mcp-run-python        # 代码沙箱（deno+pyodide）
```

剩余 2 个备选工具按场景启用：
- `onlyoffice-mcp`：需要**编辑** Office 文档（不只是转换）
- `imagesorcery-mcp`：需要 **OCR** 或**目标检测**（YOLO）

可选卸载（纯冗余）：
- `image-processing-mcp-server`：与 `mcp-image-optimizer` 功能完全重叠，npx 即用模式无本地文件需删，从 [mcp_servers.json](file:///root/yyy/mcp-tools/configs/mcp_servers.json) 移除条目即可
- `email-mcp-server` / `email-mcp-diskd`：与 `mcp-email` 功能重叠但 tool 更多，按重度自动化需求保留

---

## 详细说明

### ✅ 真可用（smoke test 通过，无需 env）

#### 1. office-mcp（LibreOffice + Pandoc 自封装）

**位置**：[wrappers/office_mcp.py](file:///root/yyy/mcp-tools/wrappers/office_mcp.py)

**提供 4 个 tool**：
- `libreoffice_convert(input_path, output_path, output_format?)` — Office 格式互转（docx/xlsx/pptx ↔ pdf/html/odt 等）
- `pandoc_convert(input_path, output_path, from_format?, to_format?)` — Markdown/HTML/Word/LaTeX 互转（需 pandoc）
- `libreoffice_version()` — LibreOffice 版本
- `pandoc_version()` — Pandoc 版本（未装返回 not_installed）

**启动**：
```bash
./start.sh office-mcp          # stdio
./start.sh office-mcp http 8109  # streamable-http
```

**外部依赖**：
- ✅ LibreOffice headless (`/usr/bin/soffice`)，已装
- ⚠️ Pandoc（`/usr/bin/pandoc`），未装；装上后 `pandoc_convert` 自动可用：
  ```bash
  apt install -y pandoc
  # 或
  conda install -c conda-forge pandoc
  ```

#### 2. onlyoffice-mcp-server（Python）

**位置**：源码 [source/onlyoffice-mcp-server](file:///root/yyy/mcp-tools/source/onlyoffice-mcp-server)，依赖装入 [pip/](file:///root/yyy/mcp-tools/pip)

**build 步骤**：
1. `pip install --target /root/yyy/mcp-tools/pip mcp<2 python-docx openpyxl python-pptx pydantic matplotlib pillow lxml pyspellchecker pypdf`
2. `pip install -e . --no-deps --target /root/yyy/mcp-tools/pip`（注册 onlyoffice_mcp 包）

**能力**：纯 Python 创建/编辑/转换 DOCX/XLSX/PPTX，含版本历史、图表、信息图、页面样式、拼写检查、查找替换

**smoke test 通过**：
- ✅ `initialize` 返回 `{"serverInfo":{"name":"onlyoffice","version":"1.29.0"}}`
- ✅ `tools/list` 列出 `docx_create` 等大量 tool
- ✅ 不依赖 OnlyOffice Docs 服务（之前我猜错了）

**启动**：`./start.sh onlyoffice-mcp`

#### 3. playwright-mcp (@executeautomation/playwright-mcp-server)

**能力**：网页截图、PDF 生成、表单自动化、SPA 数据抓取，codegen 录制

**smoke test**：✅ `playwright-mcp v1.0.11`，列出 `start_codegen_session` 等 tool

**启动**：`./start.sh playwright-mcp`（npx 即用，首次会下载 Chromium ~200MB）

Windows PowerShell：`.\start.ps1 playwright-mcp`。该入口优先使用 `runtime/node` 中的便携 Node.js 和 `runtime/playwright` 中的 Windows Chromium，无需全局安装 Node.js。

#### 4. mcp-image-optimizer

**能力**：图片缩放/旋转/裁剪/模糊/锐化/格式互转/水印/favicon/批量

**smoke test**：✅ `mcp-image-optimizer v1.0.0`，列出 `optimize_image`

**启动**：`./start.sh mcp-image-optimizer`

#### 5. imagesorcery-mcp

**位置**：`/root/yyy/mcp-tools/pip/imagesorcery_mcp/`

**能力**：缩放/旋转/裁剪/格式互转/OCR（easyocr 模型已装）/目标检测（YOLO/UltraLytics 已装）

**启动**：`./start.sh imagesorcery-mcp`（启动稍慢，Amplitude telemetry 初始化）

#### 6. image-processing-mcp-server（替代已删除的 minimal-image-processing-mcp）

**说明**：原 [cyanheads/minimal-image-processing-mcp](https://github.com/cyanheads/minimal-image-processing-mcp) GitHub 仓库已删除（404），改用功能一致的 npm 包 [image-processing-mcp-server](https://www.npmjs.com/package/image-processing-mcp-server)

**能力**：resize-image/compress-image/convert-image-format/get-image-metadata/crop-image，5 个 tool

**smoke test**：✅ `image-processing v1.0.0`

**启动**：`./start.sh image-processing-mcp-server`

#### 7. mcp-email

**位置**：`/root/yyy/mcp-tools/pip/mcp_email.py`

**能力**：纯 Python IMAP/SMTP，支持 Gmail/Outlook/Yahoo/iCloud/Zoho/自建，stdio + HTTP/OAuth

**smoke test**：✅ `mcp-email v0.5.0`

**启动**：`./start.sh mcp-email`

#### 8. mcp-run-python（替代空壳 mcp-run-isolated-python）

**位置**：`pip/mcp_run_python/`（相对于 `mcp-tools` 目录）

**说明**：原 `mcp-run-isolated-python` 是空壳包，已换用 `mcp-run-python`。注意：`mcp-run-python` 上游已于 2026-01-30 归档，不能作为运行不受信任代码的强安全沙箱。

**能力**：用 deno + pyodide 执行任意 Python 代码沙箱，默认无网络，`run_python_code` tool

**build 步骤**：
1. `pip install --target ./pip mcp-run-python`
2. `npm install -g deno`（deno 二进制 ~50MB）
3. 首次启动会自动下载 deno deps ~100MB（pyodide + express + MCP SDK），后续有缓存秒启

**smoke test**：✅ `MCP Run Python v0.0.13`

**启动**：
```bash
./start.sh mcp-run-python          # stdio
./start.sh mcp-run-python http 8110  # streamable-http
```

Windows PowerShell：
```powershell
.\start.ps1 mcp-run-python             # stdio
.\start.ps1 mcp-run-python http 8110   # streamable-http
```

Windows MCP 客户端可使用 `configs/mcp_servers.windows.json`。启动脚本默认传入 `--disable-networking`；仍建议在设置资源和文件权限限制的容器中运行模型生成的代码。

---

### ⚠️ 已 build 完成，启动需 env 配置

#### 9. email-mcp-diskd (@diskd-ai/email-mcp)

**位置**：源码 [source/email-mcp-diskd](file:///root/yyy/mcp-tools/source/email-mcp-diskd)，dist 产物 `dist/main.js`

**build 步骤**：
1. `cd source/email-mcp-diskd`
2. `npm install --no-audit --no-fund --ignore-scripts`（装 657 个包，~51s）
3. `./node_modules/.bin/tsc -p tsconfig.build.json`（编译 TypeScript -> dist/）
4. **注意**：npm 包 `@diskd-ai/email-mcp` 在 npm registry 上 404，作者只写在 package.json 没真发布，必须本地 build

**能力**：47 个工具，多账户、IMAP/SMTP、OAuth2、邮件调度、IMAP IDLE 监听、AI 分类、日历提取

**smoke test**：✅ build OK（dist/main.js 可执行），启动到 config 加载阶段报"No configuration found"（属正常）

##### 必填 env（4 个，缺一不可）

源码：[src/config/loader.ts](file:///root/yyy/mcp-tools/source/email-mcp-diskd/src/config/loader.ts) 的 `loadFromEnv()`

| env | 必填 | 默认 | 说明 |
|-----|------|------|------|
| `MCP_EMAIL_ADDRESS` | ✅ | - | 邮箱地址，同时用于 IMAP/SMTP 登录 |
| `MCP_EMAIL_IMAP_HOST` | ✅ | - | IMAP 服务器，如 `imap.gmail.com` |
| `MCP_EMAIL_SMTP_HOST` | ✅ | - | SMTP 服务器，如 `smtp.gmail.com` |
| `MCP_EMAIL_PASSWORD` | ⚠️ 二选一 | - | App Password（推荐）；或用下面的 OAuth2 |
| `MCP_EMAIL_OAUTH2_PROVIDER` | ⚠️ 二选一 | - | `google` / `microsoft` / `custom` |

##### 可选 env（带默认值，可省略）

| env | 默认 | 说明 |
|-----|------|------|
| `MCP_EMAIL_ACCOUNT_NAME` | `default` | 账户名 |
| `MCP_EMAIL_USERNAME` | = email | IMAP/SMTP 用户名（默认用 email） |
| `MCP_EMAIL_FULL_NAME` | - | 发件人姓名 |
| `MCP_EMAIL_IMAP_PORT` | `993` | IMAP 端口 |
| `MCP_EMAIL_IMAP_TLS` | `true` | IMAP TLS（设 `false` 关闭） |
| `MCP_EMAIL_IMAP_STARTTLS` | `false` | IMAP STARTTLS |
| `MCP_EMAIL_IMAP_VERIFY_SSL` | `true` | 验证 SSL 证书 |
| `MCP_EMAIL_SMTP_PORT` | `465` | SMTP 端口 |
| `MCP_EMAIL_SMTP_TLS` | `true` | SMTP TLS |
| `MCP_EMAIL_SMTP_STARTTLS` | `false` | SMTP STARTTLS（Outlook 需 `true`） |
| `MCP_EMAIL_SMTP_VERIFY_SSL` | `true` | 验证 SSL |
| `MCP_EMAIL_RATE_LIMIT` | `10` | 每分钟每账户最多邮件数 |
| `MCP_EMAIL_READ_ONLY` | `false` | 设 `true` 禁止写操作 |
| `MCP_EMAIL_WATCHER_ENABLED` | `false` | 启用 IMAP IDLE 实时监听 |
| `MCP_EMAIL_WATCHER_FOLDERS` | `INBOX` | 监听的文件夹，逗号分隔 |
| `MCP_EMAIL_WATCHER_IDLE_TIMEOUT` | `1740` | IDLE 超时秒（IMAP 上限 1800） |
| `MCP_EMAIL_HOOK_ON_NEW_EMAIL` | `notify` | `triage` (AI 分类) / `notify` / `none` |
| `MCP_EMAIL_HOOK_PRESET` | `priority-focus` | `inbox-zero` / `gtd` / `priority-focus` / `notification-only` / `custom` |
| `MCP_EMAIL_HOOK_AUTO_LABEL` | `false` | AI 自动打标签 |
| `MCP_EMAIL_HOOK_AUTO_FLAG` | `false` | 自动标旗 |
| `MCP_EMAIL_HOOK_BATCH_DELAY` | `5` | 批处理延迟秒 |
| `MCP_EMAIL_HOOK_CUSTOM_INSTRUCTIONS` | - | 自定义 AI 提示，如 "我是工程师，@company.com 高优先级" |
| `MCP_EMAIL_ALERT_DESKTOP` | `false` | 桌面通知 |
| `MCP_EMAIL_ALERT_SOUND` | `false` | 声音提醒 |
| `MCP_EMAIL_ALERT_URGENCY_THRESHOLD` | `high` | `urgent`/`high`/`normal`/`low` |
| `MCP_EMAIL_ALERT_WEBHOOK_URL` | - | Slack/Discord/ntfy webhook |
| `MCP_EMAIL_HOOK_AUTO_CALENDAR` | `false` | 自动提取日历事件 |
| `MCP_EMAIL_HOOK_CALENDAR_NAME` | - | 日历名 |
| `MCP_EMAIL_HOOK_CALENDAR_ALARM_MINUTES` | `15` | 提前提醒分钟数 |

##### OAuth2 四件套（仅当不用 password 时）

| env | 说明 |
|-----|------|
| `MCP_EMAIL_OAUTH2_PROVIDER` | `google` / `microsoft` / `custom` |
| `MCP_EMAIL_OAUTH2_CLIENT_ID` | OAuth Client ID |
| `MCP_EMAIL_OAUTH2_CLIENT_SECRET` | OAuth Client Secret |
| `MCP_EMAIL_OAUTH2_REFRESH_TOKEN` | Refresh Token（一次性获取） |

##### 配置文件备选方案（不走 env）

源码 [src/config/xdg.ts](file:///root/yyy/mcp-tools/source/email-mcp-diskd/src/config/xdg.ts)：
```bash
~/.config/email-mcp/config.toml
# 或设 XDG_CONFIG_HOME 改路径
```

模板内容可用 `node dist/main.js setup` 交互生成，或参考 [loader.ts](file:///root/yyy/mcp-tools/source/email-mcp-diskd/src/config/loader.ts) 的 `generateTemplate()` 函数。

##### 常用邮箱最小启动示例

```bash
# Gmail（最常见，需 App Password 不是邮箱密码）
export MCP_EMAIL_ADDRESS=you@gmail.com
export MCP_EMAIL_IMAP_HOST=imap.gmail.com
export MCP_EMAIL_SMTP_HOST=smtp.gmail.com
export MCP_EMAIL_PASSWORD=xxxx-xxxx-xxxx-xxxx   # 16 位 App Password
./start.sh email-mcp-diskd

# Outlook 365（注意 STARTTLS）
export MCP_EMAIL_ADDRESS=you@outlook.com
export MCP_EMAIL_IMAP_HOST=outlook.office365.com
export MCP_EMAIL_SMTP_HOST=smtp.office365.com
export MCP_EMAIL_PASSWORD=your-app-password
export MCP_EMAIL_SMTP_PORT=587
export MCP_EMAIL_SMTP_TLS=false
export MCP_EMAIL_SMTP_STARTTLS=true
./start.sh email-mcp-diskd

# 自建 IMAP（如 163/QQ）
export MCP_EMAIL_ADDRESS=you@163.com
export MCP_EMAIL_IMAP_HOST=imap.163.com
export MCP_EMAIL_SMTP_HOST=smtp.163.com
export MCP_EMAIL_PASSWORD=你的客户端授权码
./start.sh email-mcp-diskd
```

##### 如何拿 Gmail App Password（免费，30 秒）

1. 必须**先开 2FA**：[myaccount.google.com/security](https://myaccount.google.com/security) → "两步验证" → 启用
2. 再开 App Password：[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. "选择应用" → "邮件" → "生成"
4. 拿到 16 位密码（如 `abcd-efgh-ijkl-mnop`），用作 `MCP_EMAIL_PASSWORD`

**安全提示**：App Password 不是你的 Gmail 密码，可单独撤销，权限仅限邮件。

---

#### 10. email-mcp-server (npm)

**位置**：npx 缓存 `/root/.npm/_npx/1ee1bc9e93f08918/node_modules/email-mcp-server/`（v2.0.1，139.7 kB）

**能力**：18 个 tool（5 读 + 5 发 + 8 管理 + 7 自动化），支持 Gmail/Outlook/Yahoo/Zoho/iCloud/AOL/任意 IMAP

**smoke test**：✅ build OK，启动报 "Missing credentials: EMAIL_ADDRESS / EMAIL_PASSWORD"（属正常）

##### 必填 env（仅 2 个）

源码：[email-client.js](file:///root/.npm/_npx/1ee1bc9e93f08918/node_modules/email-mcp-server/build/email-client.js) 的 `EmailClient.create()`

| env | 必填 | 说明 |
|-----|------|------|
| `EMAIL_ADDRESS` | ✅ | 邮箱地址 |
| `EMAIL_PASSWORD` | ✅ | App Password（不是邮箱密码） |

##### 可选 env（用于非内置 provider）

源码内置 12 个 provider 自动识别（gmail/outlook/hotmail/yahoo/zoho 系/icloud/me/aol），其他后端需要：

| env | 默认 | 说明 |
|-----|------|------|
| `IMAP_HOST` | `imap.<域名>` 或内置 | 自定义 IMAP 主机 |
| `SMTP_HOST` | `smtp.<域名>` 或内置 | 自定义 SMTP 主机 |
| `IMAP_PORT` | `993` | IMAP 端口 |
| `SMTP_PORT` | `465` | SMTP 端口 |

##### 内置 Provider 自动识别表

| Email 域 | IMAP | SMTP |
|----------|------|------|
| gmail.com / googlemail.com | imap.gmail.com:993 TLS | smtp.gmail.com:465 TLS |
| outlook.com / hotmail.com | outlook.office365.com:993 TLS | smtp.office365.com:587 STARTTLS |
| yahoo.com | imap.mail.yahoo.com:993 TLS | smtp.mail.yahoo.com:465 TLS |
| zoho.com / .in / .eu / .com.au / .jp | imap.zoho.*:993 TLS | smtp.zoho.*:465 TLS |
| icloud.com / me.com | imap.mail.me.com:993 TLS | smtp.mail.me.com:587 STARTTLS |
| aol.com | imap.aol.com:993 TLS | smtp.aol.com:465 TLS |
| 其他自定义域 | `imap.<域名>:993` | `smtp.<域名>:465` |

##### 启动示例

```bash
# Gmail（最简，IMAP/SMTP 自动识别）
export EMAIL_ADDRESS=you@gmail.com
export EMAIL_PASSWORD=abcd-efgh-ijkl-mnop   # 16 位 App Password
./start.sh email-mcp-server

# 自建邮箱（163/QQ，无内置识别）
export EMAIL_ADDRESS=you@163.com
export EMAIL_PASSWORD=你的客户端授权码
export IMAP_HOST=imap.163.com
export SMTP_HOST=smtp.163.com
./start.sh email-mcp-server
```

##### App Password 获取链接

| 服务 | 链接 |
|------|------|
| Gmail | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)（需先开 2FA） |
| Outlook/Hotmail | [account.live.com/proofs/AppPassword](https://account.live.com/proofs/AppPassword) |
| Yahoo | [login.yahoo.com/account/security](https://login.yahoo.com/account/security) |
| Zoho | [accounts.zoho.com/home#security/app-passwords](https://accounts.zoho.com/home#security/app-passwords) |

---

##### 两个工具的差异对比

| 维度 | email-mcp-server | email-mcp-diskd |
|------|------------------|-----------------|
| **必填 env 数** | **2 个** | **4 个**（多 IMAP/SMTP host） |
| **Provider 自动识别** | ✅ 内置 12 个 | ❌ 需手填 host |
| **tool 数量** | 18 个 | **47 个** |
| **OAuth2 支持** | ❌ 仅 App Password | ✅ Google/Microsoft/Custom |
| **多账户** | ❌ 单账户 | ✅ TOML 配置可多账户 |
| **IMAP IDLE 实时监听** | ❌ | ✅ `MCP_EMAIL_WATCHER_ENABLED=true` |
| **AI 分类/标签** | ✅ 内置 triage | ✅ 可配 preset + custom_instructions |
| **日历提取** | ❌ | ✅ `MCP_EMAIL_HOOK_AUTO_CALENDAR=true` |
| **Webhook 通知** | ❌ | ✅ Slack/Discord/ntfy |
| **适用场景** | 快速接入，简单收发 | 重度邮件用户，多账户+自动化 |

**推荐**：先用 `email-mcp-server` 跑通（只 2 个 env），有重度自动化需求再换 `email-mcp-diskd`。

---

### ❌ build 失败 / 不可用

#### 11. ui-mcp (@forgespace/ui-mcp) — ❌

**原因**：`@forgespace` 组织已于 2026-04-20 停止维护，npm 警告：
```
npm warn deprecated @forgespace/ui-mcp@0.25.0: Forge-Space organization wound down 2026-04-20. This package is no longer maintained. Source archived at github.com/Forge-Space/.
```

**错误**：`better-sqlite3` 在新 Node 上 prebuild 失败 + 源码编译失败：
```
npm error path /root/.npm/_npx/cf1906c90e4eef8b/node_modules/better-sqlite3
```

**修复方案**：无（作者已停止维护，better-sqlite3 不再发新版）

**替代建议**：magic-mcp、v0-mcp（需 v0 商业 API token）、或直接用 playwright-mcp 配合人工编码

#### 12. uiforge-mcp — ❌

**原因**：同 #11，依赖 `better-sqlite3`，编译失败

**修复方案**：无

**替代建议**：同 #11

#### 13. mcp-run-isolated-python — ❌（已被替代）

**原因**：作者明确写"This package is WIP and does not have any functionality yet - just wanted to reserve pypi name"，是空壳包

**修复方案**：已用 #8 `mcp-run-python` 替代（功能完整，deno+pyodide 真沙箱）

---

### ❌ 需要外部依赖，未装

#### mcp-web-engine

**说明**：纯 Go 编写，单二进制部署，集成 SearXNG 网页搜索 + SSRF 防护

**TODO**：
1. 访问 https://github.com/search?q=mcp-web-engine 找官方仓库
2. 下载 release 二进制或 `go install`
3. 需另起 SearXNG 容器：`docker run -d -p 8080:8080 searxng/searxng`

#### glm4v-vision-mcp

**说明**：基于智谱 GLM-4V-Flash 的图像理解（识图/OCR/图表解析）

**TODO**：
1. 找官方仓库（疑似 github.com/zhipuai/glm4v-vision-mcp，clone 时返回 404，可能仓库名不同）
2. `pip install -r requirements.txt`
3. 配置 `ZHIPU_API_KEY=xxx`

#### vision-foundation-mcp

**说明**：本地视觉理解服务（图片不上传云端）

**TODO**：需手动搜索官方仓库（猜测的 cyanheads 仓库未找到）

#### mbsync-mcp

**说明**：基于 mbsync + Maildir 的本地邮件管理

**TODO**：
1. 系统依赖：`apt install -y isync`（提供 mbsync 命令）
2. 找到仓库后 `pip install -r requirements.txt`
3. 配置 `~/.mbsyncrc`

#### codemode-lite

**说明**：Podman 容器或 Pyodide WASM 沙箱，暴露单个 run_python 工具

**TODO**：PyPI 上未发布 codemode-lite，需手动找 GitHub 仓库

#### container-mcp

**说明**：Podman Ubuntu 容器隔离

**TODO**：需 Podman 环境，找官方仓库

#### OnlyBoxes

**说明**：自托管沙箱平台，多语言，水平扩展

**TODO**：找官方仓库（疑似 e2b-dev/OnlyBoxes，clone 返回 404）

#### AI-Diagram-Prototype-Generator

**说明**：集成智谱AI/OpenAI/Gemini，生成 draw.io 图表和 HTML 交互原型

**TODO**：需配置 LLM API Key 后 `pip install -r requirements.txt`

#### modao-proto-mcp

**说明**：HTML 代码生成、设计描述生成、HTML 导入

**TODO**：需 `MODAO_TOKEN=YOUR_TOKEN`，找官方仓库

#### mcp-skills-registry

**说明**：将 Shell 脚本/Python 片段/REST API 自动注册为 MCP 工具

**TODO**：找官方仓库

---

## 接入 YoAgent 的方式

DataAnalysis_Agent 的 `app/dependencies.py` 中，`build_http_adapters()` 已经把外部服务都通过 HttpXAdapter 调用，对接 MCP 工具只需：

1. 选择适合的 MCP 工具（如 imagesorcery-mcp）作为前端能力
2. 用 `./start.sh <tool> http <port>` 启动为 streamable-http
3. 在 YoAgent 的前端层（不是 Agent 核心层）通过 MCP HTTP client 调用

或者反过来，把 MCP 工具作为 AnalysisAdapter 的下游执行器（如用 `mcp-run-python` 执行高级分析的 Python 算法）：
- 在 `app/adapters/http.py` 的 `HttpAnalysisAdapter.analyze()` 中，把 `analysis_type` 路由到对应 MCP 工具
- AnalysisAdapter 已经实现了契约校验和重试降级，无需改动

## 系统依赖检查

```bash
# 验证 LibreOffice
soffice --version

# 验证 Pandoc（装上后 office-mcp 自动支持 pandoc_convert）
pandoc --version

# 验证 npx（npm 自带）
npx --version

# 验证 pip 路径
ls /root/yyy/mcp-tools/pip/bin/ 2>/dev/null | grep -E "(imagesorcery|mcp-email|mcp-run-python|mcp)"
# 或直接看包
ls /root/yyy/mcp-tools/pip/ | grep -E "(imagesorcery|mcp_run_python|mcp_email)"

# 验证 deno（mcp-run-python 依赖）
deno --version

# 验证 dist 产物
ls /root/yyy/mcp-tools/source/email-mcp-diskd/dist/main.js
ls /root/yyy/mcp-tools/source/onlyoffice-mcp-server/src/onlyoffice_mcp/
```

## 维护与扩展

新增工具的步骤：
1. 在 `manifest/TOOLS.md` 加一行
2. 在 `configs/mcp_servers.json` 加 mcpServers 条目
3. 在 `start.sh` 加 case 分支
4. 用 `./start.sh <tool> http <port>` 启动后，用 `curl http://localhost:<port>/mcp` smoke test

## 已知问题

1. **pip 包冲突**：`imagesorcery-mcp` 拖入了 torch/ultralytics，重写了 websockets/redis/fastapi/starlette 版本，可能与 DataAnalysis_Agent 的依赖冲突。建议给 MCP 工具单独建 venv：
   ```bash
   python -m venv /root/yyy/mcp-tools/venv
   source /root/yyy/mcp-tools/venv/bin/activate
   pip install imagesorcery-mcp mcp-email mcp-run-python fastmcp
   ```
2. **npm node_modules 不在**：当前 `npm/node_modules` 没有持久化（可能 sandbox 清理），但所有 npm 工具都用 `npx -y <pkg>` 即用模式，不需要本地 install。
3. **playwright Chromium 下载**：首次运行 `./start.sh playwright-mcp` 会触发 200MB Chromium 下载，建议预先：`npx playwright install chromium`
4. **mcp-run-python 首启慢**：deno 会下载 pyodide + MCP SDK 等依赖 ~100MB，缓存后秒启。生产环境建议预先跑一次 warm up。
5. **@forgespace 工具不可用**：`ui-mcp` 和 `uiforge-mcp` 已停止维护（2026-04-20），`better-sqlite3` 编译失败。无修复方案，建议找替代品。
