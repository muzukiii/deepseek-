"""跨平台桌面通知：通过系统原生通知渠道提醒用户"""

from __future__ import annotations

import os
import platform
import subprocess

from astrabot.logmanager.logger import logger


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _flat(text: str) -> str:
    """通知内容压成单行，避免换行破坏 AppleScript / Windows 环境变量。"""
    return " ".join((text or "").split())


def send_desktop_notification(title: str, message: str) -> None:
    system = platform.system()
    try:
        if system == "Linux":
            _notify_linux(title, message)
        elif system == "Darwin":
            _notify_macos(title, message)
        elif system == "Windows":
            _notify_windows(title, message)
        else:
            logger.warning(f"Unsupported platform for desktop notification: {system}")
    except Exception as e:
        logger.error(f"Failed to send desktop notification: {e}")


def _notify_linux(title: str, message: str) -> None:
    subprocess.run(
        ["notify-send", title, message, "--urgency=critical", "--expire-time=10000"],
        check=False,
    )


def _notify_macos(title: str, message: str) -> None:
    # 经 argv 传参，不把 title/message 拼进 AppleScript 源码，避免脚本注入
    script = (
        "on run argv\n"
        "display notification (item 1 of argv) with title (item 2 of argv)\n"
        "end run"
    )
    subprocess.run(
        ["osascript", "-e", script, _flat(message), _flat(title)],
        check=False,
    )


def _notify_windows(title: str, message: str) -> None:
    # 经环境变量传入、对 XML 文本转义，杜绝把消息插进 PowerShell/XML 源码
    env = {
        **os.environ,
        "ASTRA_NOTIF_TITLE": _xml_escape(_flat(title)),
        "ASTRA_NOTIF_MSG": _xml_escape(_flat(message)),
    }
    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] > $null;"
        "$title = $env:ASTRA_NOTIF_TITLE; $msg = $env:ASTRA_NOTIF_MSG;"
        "$template = \"<toast><visual><binding template='ToastText02'>"
        "<text id='1'>$title</text><text id='2'>$msg</text>"
        "</binding></visual></toast>\";"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; $xml.LoadXml($template);"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AstraBot').Show($toast)"
    )
    subprocess.run(
        ["powershell", "-Command", ps_script],
        check=False,
        creationflags=0x08000000,
        env=env,
    )
