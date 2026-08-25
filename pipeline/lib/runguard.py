"""Run guard: a PER-TASK lock plus reliable signal teardown.

Two responsibilities, kept isolated here so the stage scripts stay clean:

1. ``acquire_task_lock`` — an OS-level ``flock`` on a per-task lock file
   (``runs/.locks/<slug>.lock``). At most ONE pipeline process may work on a
   GIVEN task at a time; different tasks run fully in parallel. A second
   launch for the same task refuses to start and prints who holds the lock.
   The lock is tied to the open file descriptor, so the OS releases it the
   moment the holder dies — even on ``kill -9`` or a crash — which means
   there are simply no stale locks to clean up, ever.

   The lock is keyed on the task slug so re-running a stage of the same task
   can't corrupt its workdir/snapshots. It's per-PROCESS otherwise: everything
   inside one pipeline process (the builder + reviewer AgentSessions running
   concurrently in stage 2/3, any subagents) shares that single lock. Only a
   second *process* on the *same task* is blocked.

2. ``install_signal_handlers`` — SIGINT/SIGTERM handler that force-terminates
   the whole descendant process tree (the SDK ``claude`` child, harbor
   subprocesses, their shells) via a ``pgrep -P`` walk + SIGTERM→SIGKILL,
   then exits. Without this, Ctrl-C gets absorbed as an agent "interrupt"
   and leaves half the tree running — very confusing to debug.

Call ``guard(cfg, stage, slug)`` once at the top of each entry point.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .orchestration import slugify

LOCK_DIRNAME = ".locks"
_LOCK_FD: int | None = None          # kept alive for the process lifetime → holds the lock
_TEARING_DOWN = False


# ---------------------------------------------------------------------------
# Per-task lock
# ---------------------------------------------------------------------------
def _lock_path(cfg, slug: str) -> Path:
    """``runs/.locks/<normalized-slug>.lock`` — one lock file per task.

    We normalize the slug with the same ``slugify`` that names ``runs/<slug>/``,
    so every entry point (run_pipeline, stage1/2/3 — whether given a proposal
    path, a slug, or a run dir) maps the same task onto the same lock file.
    """
    return cfg.runs_dir / LOCK_DIRNAME / f"{slugify(slug)}.lock"


def acquire_task_lock(cfg, stage: str, slug: str = "?") -> int:
    """Acquire this task's lock, or exit(3) if another run already holds it.

    Tasks are independent: a different slug means a different lock file, so
    parallel tasks never block each other. Only a second process on the SAME
    task is refused.
    """
    global _LOCK_FD
    path = _lock_path(cfg, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            holder = Path(path).read_text().strip() or "(no holder info)"
        except Exception:
            holder = "(could not read holder info)"
        os.close(fd)
        sys.stderr.write(
            f"\n[runguard] REFUSING TO START — another pipeline process is already working on "
            f"task '{slugify(slug)}':\n"
            f"    {holder}\n"
            f"    lock file: {path}\n"
            "Only one run per task may be active at a time (a different task runs in parallel\n"
            "fine — it uses its own lock). Stop the other run on THIS task first (Ctrl-C it, or\n"
            "kill the PID above), then retry. The lock frees automatically when that process dies.\n\n"
        )
        sys.exit(3)

    # We hold it — record who we are (for the message a blocked launcher will read).
    info = (f"PID={os.getpid()} stage={stage} slug={slugify(slug)} "
            f"since={time.strftime('%Y-%m-%d %H:%M:%S')} cwd={os.getcwd()}")
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, (info + "\n").encode())
        os.fsync(fd)
    except Exception:
        pass
    _LOCK_FD = fd  # keep the fd open → keep the lock held
    return fd


# ---------------------------------------------------------------------------
# Process-tree teardown
# ---------------------------------------------------------------------------
def _alive(pid: int) -> bool:
    """True only if the pid exists AND is not a zombie. A defunct (zombie)
    child is already dead — just not reaped yet — and without this check a
    SIGTERM'd process looks 'alive' until its parent reaps it, which would
    stall teardown."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2)
        if out.stdout.strip().startswith("Z"):
            return False
    except Exception:
        pass
    return True


def _descendants(root: int) -> list[int]:
    """All descendant PIDs of *root*, found by repeated ``pgrep -P`` walks.
    We follow PPID (not the process group) deliberately, so this works even
    when a child called setsid to escape its group."""
    found: list[int] = []
    stack = [root]
    while stack:
        p = stack.pop()
        try:
            out = subprocess.run(["pgrep", "-P", str(p)],
                                 capture_output=True, text=True, timeout=5)
            kids = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
        except Exception:
            kids = []
        for k in kids:
            if k not in found:
                found.append(k)
                stack.append(k)
    return found


def terminate_tree(grace: float = 3.0) -> None:
    """SIGTERM the whole descendant tree, wait ~grace seconds, then SIGKILL
    whatever survived. Leaves die first so parents don't respawn work."""
    kids = _descendants(os.getpid())
    for k in reversed(kids):  # leaves first
        try:
            os.kill(k, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + grace
    while time.time() < deadline:
        if not any(_alive(k) for k in kids):
            break
        time.sleep(0.1)
    for k in kids:
        if _alive(k):
            try:
                os.kill(k, signal.SIGKILL)
            except OSError:
                pass
    # best-effort reap of our own direct children so they don't linger as zombies
    for k in kids:
        try:
            os.waitpid(k, os.WNOHANG)
        except OSError:
            pass


def _on_signal(signum, _frame):
    global _TEARING_DOWN
    if _TEARING_DOWN:
        return
    _TEARING_DOWN = True
    sys.stderr.write(f"\n[runguard] signal {signum} received — terminating run and all "
                     "child processes (SDK agent, harbor, shells)…\n")
    sys.stderr.flush()
    try:
        terminate_tree()
    finally:
        os._exit(130)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)


# ---------------------------------------------------------------------------
# One-call entry point
# ---------------------------------------------------------------------------
def guard(cfg, stage: str, slug: str = "?") -> None:
    """One-call entry point: take this task's lock (exit 3 if busy) and install
    the teardown signal handlers. Call once at the top of each entry point's
    main(), BEFORE any provisioning happens."""
    acquire_task_lock(cfg, stage, slug)
    install_signal_handlers()
