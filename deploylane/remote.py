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


def _parse_ssh_error(dest: str, stderr: str, returncode: int) -> str:
    """Convert raw SSH stderr into an actionable error message."""
    s = (stderr or "").lower()
    host = dest.split("@")[-1] if "@" in dest else dest

    if "connection refused" in s:
        return (
            f"Connection refused to {host}\n"
            f"  Is the server running and SSH on port 22?\n"
            f"  Try: ssh {dest}"
        )
    if "no route to host" in s or "network is unreachable" in s:
        return (
            f"Cannot reach {host}\n"
            f"  Check the host IP in deploy.yml and your network connection.\n"
            f"  Try: ping {host}"
        )
    if "could not resolve hostname" in s or "name or service not known" in s:
        return (
            f"Cannot resolve hostname '{host}'\n"
            f"  Check the host in deploy.yml — use an IP address if DNS fails."
        )
    if "permission denied" in s or "publickey" in s:
        return (
            f"SSH authentication failed for {dest}\n"
            f"  Check your SSH key is in the server's ~/.ssh/authorized_keys.\n"
            f"  Try: ssh {dest}"
        )
    if "host key verification failed" in s:
        return (
            f"SSH host key verification failed for {host}\n"
            f"  If the server was reinstalled, run: ssh-keygen -R {host}\n"
            f"  Then reconnect: ssh {dest}"
        )
    if stderr.strip():
        return f"SSH error connecting to {host}: {stderr.strip()}"
    return f"SSH command failed (exit {returncode}) on {host}"


def run_cmd(argv: List[str], dry_run: bool = True) -> None:
    print("+ " + " ".join(argv))
    if dry_run:
        return
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        dest = next((a for a in argv if "@" in a), "")
        if dest and argv[0] in ("ssh", "scp"):
            raise RemoteError(_parse_ssh_error(dest, result.stderr, result.returncode))
        raise RemoteError(result.stderr.strip() or f"Command failed (exit {result.returncode})")


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

def copy_file(local_file: Path, dest: str, remote_path: str, dry_run: bool = True) -> None:
    """Copy a single local file to a remote path via scp."""
    require_tools("scp")
    remote_dir = remote_path.rsplit("/", 1)[0] if "/" in remote_path else "."
    ssh_mkdir(dest, remote_dir, dry_run=dry_run)
    run_cmd(["scp", str(local_file.resolve()), f"{dest}:{remote_path}"], dry_run=dry_run)


def ssh_run(dest: str, remote_cmd: str, dry_run: bool = True) -> None:
    """
    Run a single remote shell command via ssh.
    remote_cmd is passed as one argument to remote shell.
    """
    run_cmd(["ssh", dest, remote_cmd], dry_run=dry_run)


def ssh_run_interactive(dest: str, remote_cmd: str, dry_run: bool = True) -> None:
    """
    Run a remote command with a TTY (-t) so sudo password prompts work interactively.
    """
    argv = ["ssh", "-t", dest, remote_cmd]
    print("+ " + " ".join(argv))
    if dry_run:
        return
    require_tools("ssh")
    result = subprocess.run(argv)
    if result.returncode != 0:
        raise RemoteError(f"Remote command exited with code {result.returncode}")


def ssh_symlink(dest: str, target_path: str, link_path: str, dry_run: bool = True) -> None:
    """
    ln -sfn target_path link_path
    """
    cmd = f"ln -sfn {target_path} {link_path}"
    ssh_run(dest, cmd, dry_run=dry_run)


def ssh_capture(dest: str, remote_cmd: str) -> str:
    """Run a remote command and return stdout. Raises RemoteError on failure."""
    require_tools("ssh")
    result = subprocess.run(
        ["ssh", dest, remote_cmd],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RemoteError(result.stderr.strip() or f"Command failed: {remote_cmd}")
    return result.stdout


def remote_file_exists(dest: str, remote_path: str) -> bool:
    """Check if a remote file exists via SSH."""
    require_tools("ssh")
    result = subprocess.run(
        ["ssh", dest, f"test -f {remote_path}"],
        capture_output=True,
    )
    return result.returncode == 0


def read_remote_file(dest: str, remote_path: str) -> str:
    """Read a remote file via SSH. Raises RemoteError if not found or unreadable."""
    require_tools("ssh")
    result = subprocess.run(
        ["ssh", dest, f"cat {remote_path}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "no such file" in stderr.lower() or result.returncode == 1:
            raise RemoteError(f"File not found on server: {remote_path}")
        raise RemoteError(_parse_ssh_error(dest, stderr, result.returncode))
    return result.stdout