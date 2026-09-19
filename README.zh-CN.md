# codex-autocontinue

[English](README.md) | **简体中文**

一个静默的后台看守工具：每当 Codex 因以下提示停顿时，自动回复 `continue`：

> Selected model is at capacity. Please try a different model.

你再也不用手动输入 `continue` 了。

## 工作原理

1. 轮询 `~/.codex/logs_2.sqlite`，只处理新产生的 "model is at capacity" 日志（不会处理启动前的历史记录）。
2. 根据会话的 rollout 文件判断它是 Codex CLI 会话（tmux / iTerm2 / Terminal.app）还是 CodexManager 桌面应用。
3. 如果会话里已有排队消息，则保持静默——排队的消息自然会驱动会话继续。
4. 通过 `injectors.py` 把 `continue` 精确输入到对应的会话/窗口（按平台使用 tmux send-keys、AppleScript、xdotool、ydotool 或 PowerShell SendKeys）。
5. 每次动作只向 `watcher.log` 写一行日志。无通知、无界面、不抢焦点。

## 平台支持

| 平台 | 支持程度 | 注入方式 |
|------|----------|----------|
| macOS | 完整支持 | tmux、AppleScript（iTerm2 / Terminal.app）、CodexManager 应用 |
| Linux | 有辅助工具时完整支持，否则仅检测 | tmux、xdotool（X11）、ydotool（Wayland） |
| Windows | 尽力而为 | PowerShell SendKeys |

当平台上没有可用的注入工具时，程序仍会检测事件并在日志中提示"请手动输入 continue"，而不会报错退出。

## 安装

macOS / Linux：

```sh
./codex-autocontinue install
```

Windows（PowerShell）：

```powershell
.\codex-autocontinue.ps1 install
```

`install` 只需执行一次：它会将看守进程注册到系统服务管理器（macOS 用 launchd，Linux 用 `systemd --user`，Windows 用任务计划程序），立即启动，把命令加入 PATH，并提示所需的授权步骤（例如 macOS 的辅助功能/自动化权限）。程序开机自启，崩溃后自动重启。无需 sudo、brew 或 pip。

## 使用方法

各平台命令完全一致（`./codex-autocontinue <命令>` 或 `.\codex-autocontinue.ps1 <命令>`）：

```
install      一次性：注册到系统服务管理器、启动、打印授权步骤
uninstall    停止并完全移除（不留任何残留）
start        启动（或重启）看守进程
stop         停止（仍保持安装状态，下次登录时自动启动）
status       查看运行状态：pid、运行时长、自动续聊次数
logs         查看看守日志
```

守护进程参数（一般无需直接使用）：

```sh
./codex-autocontinue.py --dry-run        # 只记录日志，不实际注入
./codex-autocontinue.py --once           # 只轮询一次然后退出
./codex-autocontinue.py --simulate [ID]  # 打印某个线程的注入计划
```

## 配置

编辑仓库中的 `config.json`：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `phrase` | `"model is at capacity"` | 触发短语（不区分大小写） |
| `reply` | `"continue"` | 触发后注入的文本 |
| `poll_interval_seconds` | `0.25` | 轮询日志数据库的间隔 |
| `response_delay_seconds` | `1.0` | 注入前的随机延迟（±25%） |
| `skip_when_queued` | `true` | 会话有排队消息时保持静默 |
| `per_thread_cooldown_seconds` | `60` | 同一会话两次回复之间的最小间隔 |
| `max_continues_per_hour` | `20` | 所有会话合计的每小时上限 |
| `dry_run` | `false` | 只记录"将要做什么"，不实际注入 |
| `desktop_app_name` | `"CodexManager"` | 目标 Codex 桌面应用名称 |
| `inject_cli` | `true` | 允许向 CLI 会话注入 |
| `inject_app` | `true` | 允许向桌面应用注入 |
| `use_tmux` | `true` | 有 tmux 时使用 tmux send-keys |
| `use_applescript` | `true` | macOS 上使用 AppleScript（iTerm2 / Terminal.app / 应用） |
| `use_xdotool` | `true` | Linux/X11 上优先使用 xdotool |
| `use_ydotool` | `true` | Linux/Wayland 上优先使用 ydotool |

## 安全机制

- 只匹配精确的（可配置的）容量提示短语。
- 只向事件所属的会话/窗口发送输入——绝不会误入无关窗口。
- 不处理看守进程启动之前的历史事件。
- 单会话冷却 + 全局每小时上限，绝不刷屏。
- 队列感知：会话已有排队消息时保持静默。
- `dry_run` 模式可以先观察它"打算做什么"，确认可信后再开启注入。

## 依赖要求

- Python 3（仅标准库，无任何第三方包）。
- 按平台可选的辅助工具：`tmux`、`xdotool`（X11）、`ydotool`（Wayland）、PowerShell（Windows）。全部为可选项；缺失时自动降级为仅检测模式。

## 日志

看守进程的所有动作都记录在仓库下的 `watcher.log`：

```sh
./codex-autocontinue logs
```
