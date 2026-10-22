# BetterGI 远程控制插件

AstrBot 插件，用于远程控制 [BetterGI（更好的原神）](https://www.bettergi.com/)。

## 功能特性

- **本地/远程双模式** - 通过配置切换，支持同设备直接执行或跨设备远程控制
- **Webhook 事件通知** - 接收 BetterGI 事件并自动转发到聊天会话（复用 AstrBot 主端口，无需额外端口）
- **任务完成检测** - 基于 BetterGI Webhook 的 `dragon.end`/`group.end` 事件
- **定时任务** - 每天定时自动执行命令
- **命令名称自定义** - 每个命令的触发词均可配置，支持多个别名
- **简化配置** - 命令分为"一条龙"和"调度器"模板，只需填配置名
- **多会话通知** - 支持绑定多个聊天会话接收事件通知
- **零端口远程控制** - 远程模式下，辅助程序主动连接 AstrBot，BetterGI 所在电脑无需开放任何端口

## 应用项目

- [AstrBot](https://docs.astrbot.app/)
- [BetterGI](https://www.bettergi.com/)

## 架构说明

### 本地模式（单插件式）

AstrBot 和 BetterGI 在同一设备，插件直接通过子进程执行 `BetterGI.exe` 命令。
Webhook 事件通过 AstrBot 主端口接收。

```
┌─────────────┐
│   AstrBot   │
│   (插件)     │ ←── Webhook ── BetterGI
└─────────────┘
      │
      └── 子进程 ──→ BetterGI.exe
```

### 远程模式（分离式，零端口）

AstrBot 和 BetterGI 在不同设备。辅助程序运行在 BetterGI 所在电脑上，
**主动连接**到 AstrBot 的 SSE 端点，等待指令。BetterGI 电脑无需开放任何端口。

```
┌─────────────┐
│   AstrBot   │
│   (插件)     │ ←── Webhook ── BetterGI
└──────┬──────┘
       │ SSE (指令下发) / HTTP POST (结果上报)
       │ （复用 AstrBot 主端口）
┌──────▼──────┐
│  辅助程序    │ （客户端模式，主动连接，不监听端口）
└──────┬──────┘
       │ 子进程
┌──────▼──────┐
│   BetterGI   │
└─────────────┘
```

## 配置说明

### 运行模式

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `mode` | 运行模式 (`local`/`remote`) | `local` |
| `bettergi_dir` | BetterGI 安装目录（本地模式必填） | `""` |

### 命令配置

使用模板列表配置命令，分为"一条龙"和"调度器配置组"两种类型：

- **一条龙** - 只需填写一条龙配置名称，插件自动组装 `--startOneDragon <名称>`
- **调度器配置组** - 只需填写配置组名称，插件自动组装 `--startGroups <名称>`

### 命令名称自定义

所有命令均支持配置多个别名（列表形式）：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `command_names.prefix` | 命令前缀 | `better` |
| `command_names.run` | 运行命令名 | `["运行"]` |
| `command_names.status` | 状态命令名 | `["状态"]` |
| `command_names.stop` | 停止命令名 | `["停止"]` |
| `command_names.log` | 日志命令名 | `["日志"]` |
| `command_names.bind` | 绑定命令名 | `["绑定"]` |
| `command_names.help` | 帮助命令名 | `["帮助"]` |

### Webhook 配置

Webhook 复用 AstrBot 主端口，无需额外开端口。请求需携带 AstrBot API 密钥（创建方法见「使用方法 → Webhook 配置」）。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `webhook.enable` | 启用 Webhook 接收 | `true` |
| `webhook.path` | Webhook 路径后缀 | `/webhook` |

完整地址格式：`http://<AstrBot地址>:<端口>/api/v1/plugins/extensions/bettergi/webhook?key=API密钥`

### 定时任务

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `scheduled_task.enable` | 启用定时任务 | `false` |
| `scheduled_task.run_hour` | 执行时间 - 小时 (0-23) | `4` |
| `scheduled_task.run_minute` | 执行时间 - 分钟 (0-59) | `0` |
| `scheduled_task.command_index` | 执行的命令序号 | `1` |

### 远程模式配置

远程模式无需在插件中额外配置，认证统一使用 AstrBot API 密钥：

- **插件端** - 无需配置，路由注册后由 AstrBot 统一认证
- **辅助程序** - 在 `assistant/config.yaml` 中填写 `api_key`

API 密钥创建方法见「使用方法 → Webhook 配置」，同一个密钥可同时用于 Webhook 和远程模式。

### 权限配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `better_master` | 额外授权用户ID列表 | `[]`（仅管理员可用） |

AstrBot 全局管理员（`admins_id`）自动拥有权限，无需在此列表中。

## 使用方法

### 基础命令

假设使用默认命令名称（前缀 `better`）：

| 命令 | 说明 |
|------|------|
| `better运行` | 运行第1个命令 |
| `better运行 选择` | 查看可用命令列表 |
| `better运行 1` | 运行第1个命令 |
| `better状态` | 查看运行状态 |
| `better停止` | 停止当前任务（含残留进程树） |
| `better停止 强制` | 不等待优雅退出，直接强杀进程 |
| `better日志` | 查看最近10条事件 |
| `better日志 20` | 查看最近20条事件 |
| `better日志 清除` | 清除事件记录 |
| `better绑定` | 绑定当前会话为通知接收方（可绑定多个） |
| `better帮助` | 显示帮助信息 |

### Webhook 配置

BetterGI 推送事件到 AstrBot 需通过接口认证，需先创建 API 密钥，再把带密钥的地址填入 BetterGI。

**1. 创建 API 密钥**

- 进入 AstrBot 管理面板 **设置 → OpenAPI**
- 新建密钥：**名称随便填**，权限**只勾选 `plugin`**（其他取消勾选），有效期选择**永久**
- 创建后复制生成的密钥（`abk_` 开头）

**2. 在 BetterGI 中填入 Webhook 地址**

进入 BetterGI 设置 → 通知渠道 → Webhook，填入：

```
http://<AstrBot所在IP>:<端口>/api/v1/plugins/extensions/bettergi/webhook?key=你的API密钥
```

例如：`http://192.168.1.100:6185/api/v1/plugins/extensions/bettergi/webhook?key=abk_xxxxxxxx`

**3. 验证**

在 BetterGI 通知渠道中点击测试，AstrBot 日志出现 `notify.test` 事件即配置成功（转发到聊天会话需在插件配置中勾选「测试通知」事件）。

> 密钥仅用于通过 AstrBot 接口认证，泄露后可在 OpenAPI 页面删除重建。远程模式的辅助程序也使用同一个密钥。

### 事件类型

BetterGI 支持的 Webhook 事件类型（可在配置中勾选需要通知的事件）：

| 事件 | 说明 | 默认启用 |
|------|------|----------|
| `notify.test` | 测试通知 | 否 |
| `dragon.start` / `dragon.end` | 一条龙启动/结束 | 是 |
| `group.start` / `group.end` | 配置组启动/结束 | 是 |
| `task.cancel` / `task.error` | 任务取消/错误 | 是 |
| `domain.start` / `domain.end` / `domain.reward` / `domain.retry` | 自动秘境相关 | 是 |
| `tcg.start` / `tcg.end` | 七圣召唤启动/结束 | 是 |
| `album.start` / `album.end` / `album.error` | 自动音游相关 | 是 |
| `autoeat.start` / `autoeat.end` / `autoeat.info` | 自动吃药 | 是 |
| `daily.reward` | 每日奖励状态 | 是 |
| `js.custom` / `js.error` | JS自定义/错误 | 是 |

完整事件列表请参考 [BetterGI Webhook 文档](https://www.bettergi.com/dev/webhook.html)

## 远程模式使用

### 特点

- **零端口** - BetterGI 所在电脑无需开放任何端口
- **主动连接** - 辅助程序主动连接到 AstrBot，断线自动重连
- **安全** - 通过 AstrBot API 密钥认证，只允许执行指定命令

### 1. 创建 API 密钥

远程模式与 Webhook 共用同一个 API 密钥，创建方法见「使用方法 → Webhook 配置」第 1 步。

### 2. 安装辅助程序依赖

在 BetterGI 所在电脑上：

```bash
cd assistant
pip install -r requirements.txt
```

### 3. 配置 config.yaml

编辑 `assistant/config.yaml`：

```yaml
# AstrBot 的访问地址（必填）
# 例如: http://192.168.1.100:6185
astrbot_url: "http://192.168.1.100:6185"

# BetterGI 安装目录（必填）
bettergi_dir: "D:\\BetterGI"

# AstrBot API 密钥（必填）
# 在 AstrBot 设置→OpenAPI 创建：名称随便填，只勾选 plugin 权限，有效期选永久
api_key: "abk_xxxxxxxxxxxxxxxx"

# 日志级别
log_level: "INFO"
```

### 4. 使用 运行功能.bat

双击 `运行功能.bat`，通过交互式菜单操作：

| 选项 | 说明 |
|------|------|
| 1. 启动辅助程序（前台） | 前台运行，可看到日志输出，Ctrl+C 停止 |
| 2. 注册开机自启 | 创建 Windows 计划任务，登录时自动后台运行 |
| 3. 取消开机自启 | 删除计划任务 |
| 4. 查看自启状态 | 查看计划任务是否已注册 |
| 5. 测试连接 AstrBot | 检查是否能连接到 AstrBot |
| 6. 退出 | 退出菜单 |

### 5. 配置插件

在 AstrBot 插件配置中，将 `mode` 设为 `remote` 即可。认证由 AstrBot API 密钥统一处理，无需在插件中配置密钥。

### 6. Webhook 配置

远程模式下 Webhook 仍然由 AstrBot 插件接收，BetterGI 直接推送到 AstrBot 地址（同样需带 `?key=API密钥`，见「使用方法 → Webhook 配置」）。

### 日志

辅助程序运行日志保存在 `assistant/assistant.log`，包含所有连接状态和命令执行记录。

## 安装和依赖

插件无额外依赖（Webhook 和远程控制均复用 AstrBot 主端口）。

辅助程序依赖（远程模式，手动安装）：
- httpx >= 0.24.0
- psutil >= 5.9.0
- pyyaml >= 6.0

## 作者

- [苏月晅](https://yuexuan6699.dpdns.org/)

## 许可证

跟随 AstrBot 和 BetterGI 项目
