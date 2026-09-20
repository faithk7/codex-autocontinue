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
uninstall    停止并移除服务与 PATH 链接（见下文说明）
start        启动（或重启）看守进程
stop         停止（仍保持安装状态，下次登录时自动启动）
status       查看运行状态：pid、自动续聊次数
logs         查看看守日志
```

守护进程参数（一般无需直接使用）：

```sh
./codex-autocontinue.py --dry-run        # 只记录日志，不实际注入
./codex-autocontinue.py --no-dry-run     # 实际注入（覆盖配置文件）
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
- 只向事件所属的会话/窗口发送输入——绝不会误入无关窗口（但请注意下文 ydotool / Windows 的例外）。
- 不处理看守进程启动之前的历史事件。
- 单会话冷却 + 全局每小时上限，绝不刷屏。
- 队列感知：会话已有排队消息时保持静默。
- `dry_run` 模式可以先观察它"打算做什么"，确认可信后再开启注入。

## 健壮性

错误的配置或缺失的 Codex 安装都不会让看守进程崩溃循环：

- `config.json` 损坏（非法 JSON、结构错误）→ 回退到内置默认值并记录 `WARNING`。如果 `config.json` 完全缺失，则使用默认值，其中 `dry_run: true`（故障保护：仅检测，直到你完成配置）。
- 缺失 `~/.codex/logs_2.sqlite`（全新机器，从未运行过 Codex）→ 看守进程记录 `waiting for …` 并重试，而不是崩溃；`--simulate` 会打印一行提示并以状态码 1 退出。
- 非法的 `poll_interval_seconds`（零、负数、非数字）→ 钳制到默认值（0.25 秒）并记录 `WARNING`。零值会导致 100% CPU 空转，负数会导致崩溃。
- 缺失 `desktop_app_name` → 默认使用 `"CodexManager"`。

## 已知限制

测试中发现的问题，在此记录以免意外：

- **会话路由只有"CLI / 其他"两种。** 任何非 CLI 会话（VSCode 插件、`exec`、subagent）都会被当作桌面应用会话，向 `desktop_app_name` 发送按键。如果你只想覆盖 CLI，请设置 `"inject_app": false`。
- **Linux/Wayland（ydotool）向当前聚焦的窗口输入**，而非指定的 Codex 窗口——请保持 Codex 终端处于聚焦状态，或优先使用 tmux。
- **Windows 上会向任意一个能激活的 `codex.exe` 窗口发送**，多会话时可能进错窗口。自定义 `reply` 若包含 `'` 或 SendKeys 元字符（`+ ^ % ~ [ ] { }`）不会被转义——请只用纯单词。
- **Linux/X11（xdotool）通常匹配不到窗口**：它按 `codex` 子进程 pid 查找窗口，但窗口属于终端模拟器。在 X11 上可靠的方式是 tmux，否则只能停留在仅检测模式。
- **`--simulate THREAD_ID` 会显示该线程的最新日志行，即使它不是容量事件**——请核对它报告的行 id。不带参数的 `--simulate` 会按短语过滤。
- **`--once` 只能看到它那一轮轮询期间写入的行**（启动时即打水位），只适合检查链路是否通畅，不能用来补处理事件。
- **交互式运行只打印到控制台，不写入 `watcher.log`**；只有服务托管运行时才会追加日志。反过来，以服务运行时 `DRY-RUN` 行可能在日志里出现两次（一次直接写文件，一次经由捕获的 stdout）。
- **`uninstall` 只移除服务和 PATH 链接，shell 启动文件中的 `~/.local/bin` 行和 `watcher.log` 会保留**——如需彻底清除请手动删除。
- `status` 显示 pid 和自动续聊次数，不显示运行时长。

## 依赖要求

- Python 3（仅标准库，无任何第三方包）。
- 按平台可选的辅助工具：`tmux`、`xdotool`（X11）、`ydotool`（Wayland）、PowerShell（Windows）。全部为可选项；缺失时自动降级为仅检测模式。

## 日志

看守进程的所有动作都记录在仓库下的 `watcher.log`：

```sh
./codex-autocontinue logs
```
