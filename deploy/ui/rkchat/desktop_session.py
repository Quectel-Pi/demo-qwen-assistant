#!/usr/bin/env python3
"""Run rkchat as the active desktop user, using that desktop's native input path."""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def session_properties(session_id: str) -> dict[str, str]:
    text = subprocess.check_output(
        ["loginctl", "show-session", session_id, "--no-pager"], text=True)
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def active_desktop() -> dict[str, str]:
    text = subprocess.check_output(
        ["loginctl", "list-sessions", "--no-legend", "--no-pager"], text=True)
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        props = session_properties(fields[0])
        if (props.get("Active") == "yes" and props.get("Class") == "user"
                and props.get("Type") in ("wayland", "x11")):
            if os.getuid() not in (0, int(props["User"])):
                continue
            props["Id"] = fields[0]
            return props
    raise RuntimeError("未找到当前用户的已登录图形桌面；请先在设备屏幕上登录")


def is_socket(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except FileNotFoundError:
        return False


def desktop_environment(props: dict[str, str]) -> dict[str, str]:
    uid = int(props["User"])
    user = pwd.getpwuid(uid)
    runtime = Path("/run/user") / str(uid)
    if not runtime.is_dir() or runtime.stat().st_uid != uid:
        raise RuntimeError("桌面 runtime 目录无效: %s" % runtime)
    if not is_socket(runtime / "bus"):
        raise RuntimeError("桌面 D-Bus 尚未就绪")

    env = os.environ.copy()
    for key in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "GTK_IM_MODULE", "GDK_BACKEND"):
        env.pop(key, None)
    env.update(HOME=user.pw_dir, USER=user.pw_name, LOGNAME=user.pw_name,
               XDG_RUNTIME_DIR=str(runtime), XDG_SESSION_TYPE=props["Type"],
               DBUS_SESSION_BUS_ADDRESS="unix:path=%s/bus" % runtime)
    if props["Type"] == "wayland":
        # GTK_IM_MODULE=ibus bypasses the compositor input-panel protocol on this
        # GNOME Wayland desktop. GNOME itself passes composition through IBus.
        sockets = sorted(p for p in runtime.glob("wayland-*") if is_socket(p))
        if not sockets:
            raise RuntimeError("Wayland 会话尚未就绪")
        env.update(WAYLAND_DISPLAY=sockets[0].name,
                   GDK_BACKEND="wayland", GTK_IM_MODULE="wayland")
    else:
        env.update(DISPLAY=props.get("Display", ":0"),
                   GDK_BACKEND="x11", GTK_IM_MODULE="ibus")
        auth_files = list(runtime.glob(".mutter-Xwaylandauth.*"))
        auth_files += [Path(user.pw_dir) / ".Xauthority"]
        auth = next((p for p in auth_files if p.is_file()), None)
        if auth is not None:
            env["XAUTHORITY"] = str(auth)
    return env


def configure_system_keyboard() -> None:
    # Execute only after switching to the desktop user, never root's dconf.
    from gi.repository import Gio
    settings = Gio.Settings.new("org.gnome.desktop.a11y.applications")
    if not settings.get_boolean("screen-keyboard-enabled"):
        if not settings.set_boolean("screen-keyboard-enabled", True):
            raise RuntimeError("系统屏幕键盘设置不可写")
    sources = Gio.Settings.new("org.gnome.desktop.input-sources")
    configured = list(sources.get_value("sources").unpack())
    required = [("xkb", "us")]
    if Path("/usr/share/ibus/component/libpinyin.xml").exists():
        required.append(("ibus", "libpinyin"))
    missing = [item for item in required if item not in configured]
    if missing:
        from gi.repository import GLib
        if not sources.set_value("sources", GLib.Variant("a(ss)", configured + missing)):
            raise RuntimeError("系统输入源设置不可写")
    Gio.Settings.sync()


def keyboard_mode(args: list[str]) -> str:
    mode = "system"
    for index, arg in enumerate(args):
        if arg == "--keyboard":
            if index + 1 >= len(args) or args[index + 1] not in ("system", "builtin"):
                raise RuntimeError("--keyboard 必须为 system 或 builtin")
            mode = args[index + 1]
    return mode


def launch(args: list[str]) -> int:
    try:
        mode = keyboard_mode(args)
        props = active_desktop()
        env = desktop_environment(props)
        if mode == "builtin":
            # Explicit fallback bypasses Wayland OSK requests so two keyboards
            # cannot open together, without changing desktop-wide settings.
            env["GTK_IM_MODULE"] = "ibus"
        user = pwd.getpwuid(int(props["User"]))
        if os.getuid() == 0:
            # Re-enter as the real desktop user before accessing GSettings/IBus.
            env.pop("RKCHAT_DESKTOP_SESSION", None)
            os.execvpe("runuser", ["runuser", "-u", user.pw_name, "--",
                      sys.executable, str(HERE / "desktop_session.py"), *args], env)
        if mode == "system":
            configure_system_keyboard()
        env["RKCHAT_DESKTOP_SESSION"] = "1"
        print("[run] desktop=%s uid=%s backend=%s im=%s" % (
            user.pw_name, props["User"], env["GDK_BACKEND"], env["GTK_IM_MODULE"]), flush=True)
        os.execve(sys.executable, [sys.executable, "-u", str(HERE / "rkchat.py"), *args], env)
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print("[run] 无法启动桌面 UI: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(launch(sys.argv[1:]))
