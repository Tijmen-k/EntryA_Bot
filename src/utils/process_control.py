"""
Process control for the trading engine (main.py) — find/start/stop it as an OS
process on the local machine.

This is a Streamlit-free extraction of the same pattern app.py already uses
(psutil find-by-cmdline, subprocess.Popen relaunch). app.py's own copy is left
untouched — this is a new, additive module so the Discord bot (and anything
else) can reuse the same logic without a Streamlit dependency.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

import psutil
from loguru import logger


def _is_project_bot_process(proc: "psutil.Process", project_dir: str) -> bool:
    try:
        cmdline = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    if not any(part.endswith("main.py") for part in cmdline):
        return False
    try:
        return os.path.normcase(proc.cwd()) == os.path.normcase(project_dir)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True  # cmdline matched but cwd unreadable — err on the side of flagging it


def find_bot_pid(project_dir: str, exclude_pids: frozenset[int] = frozenset()) -> Optional[int]:
    """Return the PID of a running `python main.py` process for this project, if any."""
    own_pid = os.getpid()
    for proc in psutil.process_iter(["pid"]):
        pid = proc.info["pid"]
        if pid == own_pid or pid in exclude_pids:
            continue
        if _is_project_bot_process(proc, project_dir):
            return pid
    return None


def is_running(project_dir: str) -> bool:
    return find_bot_pid(project_dir) is not None


def start(project_dir: str, dry_run: bool = False) -> subprocess.Popen:
    """Launch `main.py` as a detached process. Caller should check is_running() first."""
    args = [sys.executable, "main.py"]
    if dry_run:
        args.append("--dry-run")
    kwargs: dict = {"cwd": project_dir}
    if sys.platform == "win32":
        # Survives independently of this process's own lifetime (important for
        # /restart, where the launching process may exit before main.py does).
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(args, **kwargs)
    logger.info(f"Launched main.py (pid={proc.pid}, dry_run={dry_run})")
    return proc


def stop(project_dir: str, timeout: int = 5) -> bool:
    """Terminate the running main.py process, if any. Returns True if a process was found and stopped."""
    pid = find_bot_pid(project_dir)
    if pid is None:
        return False
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            proc.kill()
        logger.info(f"Stopped main.py (pid={pid})")
        return True
    except psutil.NoSuchProcess:
        return False
