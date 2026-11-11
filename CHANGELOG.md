
2.1.8: 仓库地址迁移至 suyuexuan-cn 26.11.11

- `metadata.yaml` 的 `repo` 与 README 作者主页链接迁移到新账号 `suyuexuan-cn`
- 同步 `main.py` 注册版本号

2.1.7: 辅助程序改为绿色免安装版 26.10.22

- `assistant/` 目录替换为 `bettergi_assistant/`，随包内置 Python 3.13 运行环境
- 目标电脑无需安装 Python、无需联网装依赖，解压即用
- 启动入口改为 `启动.bat`，调用内置 `python\python.exe`；缺少 python 目录时给出明确提示
- `launcher.py` 新增绿色版检测：跳过 pip 安装，菜单 10 由「安装 / 修复依赖」改为「检查运行环境」
- 状态页新增「运行环境」一行，显示内置 Python 版本
- 新增 `使用说明.txt`：目录结构、首次使用、菜单说明、命令行用法、常见问题
- 新增 `bettergi_assistant/python/` 到 .gitignore，运行环境只在 Releases 附件分发，不进版本库
- README 改为从 Releases 下载 zip 的安装流程

2.1.6: 启动器缺失依赖时可自动安装 26.10.22

- 新增启动前依赖自检：缺少 httpx / psutil / pyyaml 时提示并可直接自动安装
- 新增菜单项 10「安装 / 修复依赖」，以及命令行参数 --deps
- 安装失败自动换清华镜像重试，再失败加 --user；当前 Python 无 pip 时先用 ensurepip 修复
- 安装后刷新导入缓存，同一次运行内即可生效，无需重启窗口
- 没有 pyyaml 时改用内置简易解析读取 config.yaml，不再直接报警无法读配置，菜单与配置向导仍可用
- 状态页与菜单头部增加「运行依赖」显示
- 配置读取失败提示合并为一条，不再每次刷新菜单都重复报错

2.1.5: 辅助程序启动器改为 Python 实现 26.10.22

- 新增 assistant/launcher.py，原 运行功能.bat 的全部功能迁移到 Python
- 修复 bat 用 findstr 解析 YAML 易被缩进、引号、URL 冒号截断的问题，改用 yaml 安全解析
- 修复 schtasks 注册时引号嵌套在含空格路径下易失败的问题，改为参数列表调用，失败自动回退到当前用户注册表 Run 项
- 开机自启支持计划任务与注册表双通道，查看/取消时两处都处理
- 连接测试由 PowerShell 改为 httpx，密钥走 Authorization 头，不再拼进 URL
- 新增后台启动/停止、依赖自动安装、配置向导（保留原注释）、日志查看
- 支持命令行参数：--start / --daemon / --stop / --install / --uninstall / --status / --test / --config / --log
- 运行功能.bat 精简为入口，仅负责调用 launcher.py

2.1.4: 修复运行时无法用指令停止 26.10.22

- 本地模式停止不再只认插件启动的子进程：BetterGI 单实例转发、插件热重载、手动启动时子进程已退出但任务仍在跑，旧逻辑会误报“当前没有正在运行的任务”
- 停止时按可执行文件名兜底查找残留进程，并限定在配置的安装目录内，不误杀其他副本
- 改为结束整棵进程树，避免 BetterGI 退出后游戏与子进程残留
- 新增 `停止 强制`：跳过优雅退出直接强杀；默认仍先 terminate 等待 5 秒再 kill
- cleanup（插件卸载）只结束自己启动的进程，不影响手动打开的 BetterGI
- 远程模式停止：task_id 改用 UUID，15 秒未确认也清空运行标记，修复状态永久卡在“运行中”、反复停止反复超时
- 远程模式命令下发超时由 60 秒降至 20 秒，避免指令长时间无响应
- Webhook 收到结束类事件（dragon.end/group.end/task.cancel 等）自动清空“运行中”标记
- 状态查询显示实际探测到的 BetterGI 进程 PID

2.1.3: 认证统一为 AstrBot API 密钥 26.08.27

- 删除 webhook.token 和 remote.token 配置项，认证统一由 AstrBot 接口层处理
- Webhook 地址需携带 API 密钥：http://<AstrBot地址>:<端口>/api/v1/plugins/extensions/bettergi/webhook?key=API密钥
- API 密钥在 AstrBot 设置→OpenAPI 创建：名称随便填，只勾选 plugin 权限，有效期选永久
- 修复远程模式在新版 AstrBot 下因接口强制认证导致的 401（原 Bearer 头和 ?token= 参数均不被接口层接受）
- 辅助程序配置 token 改为 api_key，SSE 连接和结果上报通过 Authorization: ApiKey 头携带密钥
- 辅助程序对旧版 token 配置和无密钥情况给出明确警告
- bat 脚本连接测试自动携带 API 密钥，补充密钥相关错误提示

2.1.2: 综合检查修复 26.08.27

- 修复权限逻辑与配置说明不一致：better_master 为空时改为仅管理员可用
- stop_event 仅在匹配到命令时触发，不再拦截 better 开头的普通消息
- 权限检查移到前缀匹配之后，避免每条消息都查询管理员配置
- 修复子进程管道无人读取可能导致的卡死（本地与辅助程序均改 DEVNULL）
- 修复 bat 脚本读取 astrbot_url 时被 URL 冒号截断的问题
- SSE 连接增加 30 秒心跳，断开时清空待执行队列，防止重连后执行过期命令
- 辅助程序断线统一 5 秒后重连，连接阶段增加 10 秒超时
- 修复定时任务启动即显示"上次执行"为当天的问题
- 运行/停止失败时向用户返回具体原因（辅助程序未连接、已有任务运行等）
- 结果上报仅通过 Authorization 头携带 token，不再暴露于 URL
- 删除无效配置 command_timeout、debug_log 及死代码

2.1.1: 复用 AstrBot 主端口 26.08.27

- Webhook 接收从独立 aiohttp 服务器（默认 8088 端口）改为 register_web_api 注册路由，复用 AstrBot 主端口，不再单独占用端口
- Webhook 地址变更为 http://<AstrBot地址>:<端口>/api/v1/plugins/extensions/bettergi/webhook
- 远程模式重构：辅助程序从监听端口的 HTTP 服务器改为主动连接 AstrBot 的 SSE 客户端，BetterGI 所在电脑无需开放任何端口
- 插件通过 SSE 下发指令，辅助程序执行后 POST 回传结果
- 辅助程序配置从 host/port 改为 astrbot_url，依赖从 fastapi/uvicorn 精简为 httpx
- 插件移除 aiohttp 依赖

2.1.0: 修复定时任务时间无效 26.08.27

- 修复调度器未读取配置时间、始终按默认 04:00 执行的问题
- 定时时间配置由字符串 run_time("HH:MM") 改为 run_hour/run_minute 两个整数，从输入层面避免非数字
- 旧配置的 run_time 已废弃，需在管理面板重新设置定时时间

2.0.7～2.0.9: 处理了大量的 BUG 26.08.19

2.0.6: 修复截图转发 26.08.18

- base64 图片改为先保存临时文件再用 file_image 发送
- 兼容性更好，避免 base64 格式在各平台适配器中的差异

2.0.5: 转发截图 26.08.18

- Webhook 事件通知支持转发 BetterGI 截图
- 使用 base64_image 发送 base64 编码的 JPEG 图片

2.0.4: 修复命令不执行 26.08.18

- 修复 yield 后代码不执行导致 BetterGI 不启动
- 命令执行改为先执行再返回结果，避免事件传播被中断
- 失败时返回错误信息给用户

2.0.3: 改用 regex 过滤器 26.08.18

- 消息监听从 event_message_type 改为 @filter.regex，priority=1
- 不受 wake_prefix 约束，消息到达更可靠

2.0.2: 多别名+多会话通知 26.08.18

- 命令名称支持多个别名（如 运行/run/启动 均可触发）
- 通知接收支持绑定多个会话（better绑定 可多次执行）
- 帮助文本展示所有别名（用 / 分隔）

2.0.1: 修复热重载问题 26.08.18

- 修复热重载后 Webhook 服务器不启动（on_astrbot_loaded 改为 initialize）
- 默认 Webhook 路径从 /bettergi/webhook 改为 /bettergi

2.0.0: 完全重写插件 26.08.18

- 新增 Webhook 事件接收与转发（aiohttp HTTP 服务器）
- 新增本地/远程双模式（配置切换）
- 新增辅助程序（FastAPI，支持开机自启）
- 新增命令名称自定义（每个命令可配置触发词）
- 新增事件类型勾选配置（22种 BetterGI 事件）
- 新增辅助程序命令校验（仅允许 --startOneDragon/--startGroups）
- 重写定时任务（修复重复触发 BUG）
- 简化命令配置（template_list 模板，只需填配置名）
- 权限检查读取 AstrBot 管理员列表作为默认授权
- 删除消息撤回、截图、队列管理功能
