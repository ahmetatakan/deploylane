from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


class RemoteError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteTarget:
    host: str
    user: str
    deploy_dir: str

    @property
    def ssh_dest(self) -> str:
        return f"{self.user}@{self.host}"


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def require_tools(*tools: str) -> None:
    missing = [t for t in tools if not _which(t)]
    if missing:
        raise RemoteError(f"Missing required tools: {', '.join(missing)}")


def run_cmd(argv: List[str], dry_run: bool = True) -> None:
    # deterministic print
    print("+ " + " ".join(argv))
    if dry_run:
        return
    try:
        subprocess.run(argv, check=True)
    except subprocess.CalledProcessError as e:
        raise RemoteError(f"Command failed: {argv}\n{e}") from e


def ssh_mkdir(dest: str, remote_path: str, dry_run: bool = True) -> None:
    # mkdir -p
    run_cmd(["ssh", dest, "mkdir", "-p", remote_path], dry_run=dry_run)


def rsync_dir(local_dir: Path, dest: str, remote_dir: str, dry_run: bool = True) -> None:
    # rsync local_dir/ -> remote_dir/
    # -a: archive, -v: verbose, -z: compress, --mkpath: create destination path (newer rsync)
    # Not all rsync have --mkpath, so we mkdir via ssh first.
    local = str(local_dir.resolve()).rstrip("/") + "/"
    remote = f"{dest}:{remote_dir.rstrip('/')}/"
    run_cmd(["rsync", "-avz", local, remote], dry_run=dry_run)


def scp_dir(local_dir: Path, dest: str, remote_dir: str, dry_run: bool = True) -> None:
    # fallback if rsync not installed
    # copies local_dir/* into remote_dir
    local = str(local_dir.resolve()).rstrip("/") + "/."
    remote = f"{dest}:{remote_dir.rstrip('/')}/"
    run_cmd(["scp", "-r", local, remote], dry_run=dry_run)


def copy_dir(local_dir: Path, dest: str, remote_dir: str, dry_run: bool = True) -> str:
    """
    Prefer rsync, fallback to scp.
    Returns: method used ("rsync" or "scp")
    """
    require_tools("ssh")
    if not local_dir.exists() or not local_dir.is_dir():
        raise RemoteError(f"Local directory not found: {local_dir}")

    ssh_mkdir(dest, remote_dir, dry_run=dry_run)

    if _which("rsync"):
        rsync_dir(local_dir, dest, remote_dir, dry_run=dry_run)
        return "rsync"

    require_tools("scp")
    scp_dir(local_dir, dest, remote_dir, dry_run=dry_run)
    return "scp"

def ssh_run(dest: str, remote_cmd: str, dry_run: bool = True) -> None:
    """
    Run a single remote shell command via ssh.
    remote_cmd is passed as one argument to remote shell.
    """
    run_cmd(["ssh", dest, remote_cmd], dry_run=dry_run)


def ssh_symlink(dest: str, target_path: str, link_path: str, dry_run: bool = True) -> None:
    """
    ln -sfn target_path link_path
    """
    cmd = f"ln -sfn {target_path} {link_path}"
    ssh_run(dest, cmd, dry_run=dry_run)