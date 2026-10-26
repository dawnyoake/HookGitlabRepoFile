"""OS-specific locks, console windows and per-user schedulers."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys


@contextmanager
def process_lock(path):
    with path.open("a+b") as handle:
        acquired = False
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        try:
            yield acquired
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def console_python():
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    return str(executable)


def windows_notify(script, summary):
    # Execute Python directly, bypassing cmd expansion of %, &, quotes or remote text.
    return subprocess.Popen([console_python(), str(script), "display", str(summary), "--pause"],
                            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))


def windows_task_command(config_path, data_dir):
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.is_file():
        raise RuntimeError("Windows 定时任务需要标准 Python 安装中的 pythonw.exe；请使用官方 Python 安装包")
    script = Path(__file__).with_name("monitor.py").resolve()
    args = [str(python), str(script), "--config", str(config_path),
            "--log-file", str(Path(data_dir) / "logs/monitor.log"), "check"]
    return subprocess.list2cmdline(args)


def install_windows(config_path, config):
    seconds = int(config.get("interval_seconds", 300))
    if seconds < 60 or seconds % 60 or seconds > 86340:
        raise ValueError("Windows 检查间隔须为 60 秒的整数倍，范围 60 到 86340 秒")
    Path(config["data_dir"], "logs").mkdir(parents=True, exist_ok=True)
    command = windows_task_command(config_path, config["data_dir"])
    subprocess.run(["schtasks.exe", "/Create", "/TN", "LML-monitor", "/SC", "MINUTE", "/MO",
                    str(seconds // 60), "/TR", command, "/IT", "/F"], check=True)
    print("已安装当前用户交互式任务 LML-monitor；登录期间运行。")


def uninstall_windows():
    subprocess.run(["schtasks.exe", "/Delete", "/TN", "LML-monitor", "/F"], check=True)
