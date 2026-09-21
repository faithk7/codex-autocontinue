"""CLI language for install/permissions screens (stdlib only).

English is the default. Simplified Chinese is used when the OS UI language
is Chinese (`zh*`). `CODEX_UI_LANG=zh|en` overrides detection.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unicodedata
from typing import Any

_lang: str | None = None

_APPLE_LANG_RE = re.compile(r'"([^"]+)"')
_APPLE_LANG_BARE_RE = re.compile(r"\(\s*([A-Za-z0-9_-]+)")


def ui_lang() -> str:
    """Cached UI language: 'zh' or 'en'."""
    global _lang
    if _lang is None:
        _lang = _detect()
    return _lang


def t(key: str, **kwargs: Any) -> str:
    """Look up `key` in the active catalog; kwargs are str.format fields."""
    catalog = ZH if ui_lang() == "zh" else EN
    template = catalog.get(key, EN[key])
    return template.format(**kwargs) if kwargs else template


def t_detail(detail: str) -> str:
    """Translate a known permission-probe detail; pass unknown text through."""
    if not detail:
        return detail
    mapped = {
        "ok": t("detail.ok"),
        "not running; macOS will ask on first real injection": t("detail.not_running"),
        "prompt unanswered (timed out)": t("detail.timeout"),
        "denied — enable in System Settings > Privacy & Security > Automation":
            t("detail.denied_automation"),
        "denied — enable in System Settings > Privacy & Security > Accessibility":
            t("detail.denied_accessibility"),
        "inject_app is off": t("detail.inject_app_off"),
        "not granted yet — flip the switch in System Settings":
            t("detail.ax_pending"),
        "unexpected reply": t("detail.unexpected"),
    }
    if detail in mapped:
        return mapped[detail]
    prefix = "consent recorded (probe reply: "
    if detail.startswith(prefix) and detail.endswith(")"):
        return t("detail.consent_recorded", reply=detail[len(prefix):-1])
    return detail


def display_width(text: str) -> int:
    """Visible column count: fullwidth CJK is 2, combining marks are 0."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
    return width


def pad(text: str, width: int) -> str:
    """Pad `text` with spaces to `width` display columns."""
    extra = width - display_width(text)
    return text + (" " * extra if extra > 0 else "")


def _detect() -> str:
    """Resolve UI language: override, then OS, then env, then English."""
    override = os.environ.get("CODEX_UI_LANG", "").strip().lower()
    if override in ("zh", "en"):
        return override
    if sys.platform == "darwin":
        tag = _apple_language()
        if tag:
            return "zh" if _is_zh(tag) else "en"
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(key, "").strip()
        if val and val not in ("C", "POSIX"):
            return "zh" if _is_zh(val) else "en"
    if sys.platform == "win32":
        tag = _win_ui_language()
        if tag:
            return "zh" if _is_zh(tag) else "en"
    return "en"


def _is_zh(tag: str) -> bool:
    """True for zh, zh-Hans, zh_CN.UTF-8, and other zh* locale tags."""
    t = tag.strip().strip("\"'").replace("_", "-").lower()
    t = t.split(".", 1)[0].split("@", 1)[0]
    return t == "zh" or t.startswith("zh-")


def _apple_language() -> str | None:
    """First AppleLanguages entry, or None when defaults is unavailable."""
    try:
        proc = subprocess.run(
            ["defaults", "read", "-g", "AppleLanguages"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    out = proc.stdout
    m = _APPLE_LANG_RE.search(out)
    if m:
        return m.group(1)
    m = _APPLE_LANG_BARE_RE.search(out)
    return m.group(1) if m else None


def _win_ui_language() -> str | None:
    """'zh' or 'en' from the Windows UI language; None on failure."""
    try:
        import ctypes
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        # LANG_CHINESE = 0x04 (primary language id in the low 10 bits).
        return "zh" if (langid & 0x3FF) == 0x04 else "en"
    except Exception:
        return None


# ---- catalogs (identical keys; English matches the original CLI copy) -----

EN: dict[str, str] = {
    "install.header": "Installing codex-autocontinue",
    "install.service_registered": "Service registered",
    "install.service_failed": "service registration failed",
    "install.watcher_started": "Watcher started",
    "install.watcher_pid": "pid {pid}",
    "install.watcher_not_running": "not running yet; check: codex-autocontinue status",
    "install.command_on_path": "Command on PATH",
    "install.codex_db": "Codex log database",
    "install.db_missing": "not found yet; run codex once (the watcher waits for it)",
    "install.injectors": "Injectors",
    "install.injectors_none": "none available; detection-only",
    "install.done": "Installed.",
    "install.mode": "mode",
    "install.mode_dry": " — logs what it would do, injects nothing",
    "install.mode_live": " — replies {reply!r} automatically",
    "install.service": "service",
    "install.starts_at_login": " (starts at login)",
    "install.config": "config",
    "install.logs": "logs",
    "install.logs_cmd": "codex-autocontinue logs",
    "install.alias": "alias",
    "install.alias_note": " — same commands, shorter to type",
    "install.next_steps": "Next steps",
    "install.next_dry_run":
        'set "dry_run": false in config.json, then: codex-autocontinue start',
    "install.next_new_shell": "open a new shell so PATH picks up ~/.local/bin",
    "install.next_new_shell_win": "open a new shell so the PATH change takes effect",
    "install.next_verify_perms": "verify macOS permissions: codex-autocontinue doctor",
    "install.next_finish_perms":
        "finish macOS permissions: codex-autocontinue doctor --fix",
    "install.next_linux_injectors":
        "install tmux (best), xdotool (X11) or ydotool (Wayland) to enable injection",
    "prime.header": "Permissions",
    "prime.intro":
        "  Priming from the running watcher (the identity Apple will ask about)...",
    "prime.expect": "  Expect one macOS dialog per line — click Allow on each:",
    "prime.automation": 'Automation: "{who}" may control "{app}"',
    "prime.not_running": " (not running — macOS will ask on first real injection)",
    "prime.accessibility": 'Accessibility: turn on "{who}"',
    "prime.runs_as": "(watcher runs as {who_path})",
    "prime.restarting": "Restarting watcher...",
    "prime.opening_settings": "Opening System Settings...",
    "prime.done": "done",
    "prime.failed": "failed",
    "prime.restart_failed": "could not restart the watcher",
    "prime.settings_skipped": "skipped (open Privacy & Security manually)",
    "prime.nontty":
        "non-interactive shell: allow the macOS dialogs, then verify with:",
    "prime.nontty_cmd": "    codex-autocontinue doctor",
    "prime.enter_verify":
        "  Click Allow in the macOS dialogs, then press Enter "
        "to verify (q quits instantly)... ",
    "prime.line_verify": "  Type q to quit, or press Enter to verify... ",
    "prime.verifying": "  Verifying...",
    "prime.verify_waiting": "Verifying... waiting on: {items}",
    "report.header": "Permissions",
    "report.not_primed": "not primed yet — run: codex-autocontinue doctor --fix",
    "report.checked": "checked",
    "report.by_watcher": " · by the running watcher",
    "report.accessibility": "Accessibility",
    "report.granted_partial": "{granted} of {total} granted",
    "report.allow_remaining":
        "allow the remaining dialogs (or enable in System Settings), then:",
    "report.fix_cmd": "    codex-autocontinue doctor --fix",
    "report.all_granted": "All {total} applicable permissions granted.",
    "report.still_running":
        "priming may still be running — re-check with: codex-autocontinue doctor",
    "detail.ok": "ok",
    "detail.not_running": "not running; macOS will ask on first real injection",
    "detail.timeout": "prompt unanswered (timed out)",
    "detail.denied_automation":
        "denied — enable in System Settings > Privacy & Security > Automation",
    "detail.denied_accessibility":
        "denied — enable in System Settings > Privacy & Security > Accessibility",
    "detail.inject_app_off": "inject_app is off",
    "detail.ax_pending":
        "not granted yet — flip the switch in System Settings",
    "detail.consent_recorded": "consent recorded (probe reply: {reply})",
    "detail.unexpected": "unexpected reply",
}

ZH: dict[str, str] = {
    "install.header": "正在安装 codex-autocontinue",
    "install.service_registered": "服务已注册",
    "install.service_failed": "服务注册失败",
    "install.watcher_started": "看守已启动",
    "install.watcher_pid": "pid {pid}",
    "install.watcher_not_running": "尚未运行；请检查：codex-autocontinue status",
    "install.command_on_path": "命令已加入 PATH",
    "install.codex_db": "Codex 日志数据库",
    "install.db_missing": "尚未找到；先运行一次 Codex（看守会等待）",
    "install.injectors": "注入器",
    "install.injectors_none": "暂无可用；仅检测",
    "install.done": "安装完成。",
    "install.mode": "模式",
    "install.mode_dry": " — 只记录将要做的事，不注入",
    "install.mode_live": " — 自动回复 {reply!r}",
    "install.service": "服务",
    "install.starts_at_login": "（开机启动）",
    "install.config": "配置",
    "install.logs": "日志",
    "install.logs_cmd": "codex-autocontinue logs",
    "install.alias": "别名",
    "install.alias_note": " — 同样的命令，更短的写法",
    "install.next_steps": "后续步骤",
    "install.next_dry_run":
        '在 config.json 中设置 "dry_run": false，然后执行：codex-autocontinue start',
    "install.next_new_shell": "新开一个终端，以便 PATH 生效（~/.local/bin）",
    "install.next_new_shell_win": "新开一个终端，以便 PATH 更改生效",
    "install.next_verify_perms": "验证 macOS 权限：codex-autocontinue doctor",
    "install.next_finish_perms":
        "完成 macOS 权限：codex-autocontinue doctor --fix",
    "install.next_linux_injectors":
        "安装 tmux（首选）、xdotool（X11）或 ydotool（Wayland）以启用注入",
    "prime.header": "权限",
    "prime.intro": "  正在通过运行中的看守预授权（macOS 会询问该身份）...",
    "prime.expect": "  下面每一行对应一个系统对话框，请逐个点「允许」：",
    "prime.automation": "自动化：「{who}」想要控制「{app}」",
    "prime.not_running": "（未在运行 — 首次真正注入时 macOS 才会询问）",
    "prime.accessibility": "辅助功能：打开「{who}」的开关",
    "prime.runs_as": "（看守运行为 {who_path}）",
    "prime.restarting": "正在重启看守...",
    "prime.opening_settings": "正在打开系统设置...",
    "prime.done": "完成",
    "prime.failed": "失败",
    "prime.restart_failed": "无法重启看守",
    "prime.settings_skipped": "已跳过（请手动打开“隐私与安全性”）",
    "prime.nontty": "非交互式终端：请允许 macOS 对话框，然后用以下命令验证：",
    "prime.nontty_cmd": "    codex-autocontinue doctor",
    "prime.enter_verify":
        "  请在系统对话框中点「允许」，然后按 Enter 验证（按 q 立即退出）... ",
    "prime.line_verify": "  输入 q 退出，或按 Enter 验证... ",
    "prime.verifying": "  正在验证...",
    "prime.verify_waiting": "正在验证... 等待：{items}",
    "report.header": "权限",
    "report.not_primed": "尚未预授权 — 请运行：codex-autocontinue doctor --fix",
    "report.checked": "检查时间",
    "report.by_watcher": " · 由运行中的看守",
    "report.accessibility": "辅助功能",
    "report.granted_partial": "已授予 {granted} / {total} 项",
    "report.allow_remaining": "请允许剩余对话框（或在系统设置中开启），然后：",
    "report.fix_cmd": "    codex-autocontinue doctor --fix",
    "report.all_granted": "全部 {total} 项适用权限已授予。",
    "report.still_running":
        "预授权可能仍在进行 — 请重新检查：codex-autocontinue doctor",
    "detail.ok": "正常",
    "detail.not_running": "未在运行；首次真正注入时 macOS 才会询问",
    "detail.timeout": "未响应提示（超时）",
    "detail.denied_automation":
        "已拒绝 — 请在系统设置 > 隐私与安全性 > 自动化 中开启",
    "detail.denied_accessibility":
        "已拒绝 — 请在系统设置 > 隐私与安全性 > 辅助功能 中开启",
    "detail.inject_app_off": "inject_app 已关闭",
    "detail.ax_pending": "尚未授予 — 请在系统设置中打开开关",
    "detail.consent_recorded": "已记录授权（探测回复：{reply}）",
    "detail.unexpected": "意外回复",
}

assert set(EN) == set(ZH), set(EN) ^ set(ZH)
