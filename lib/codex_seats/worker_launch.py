from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path


WORKER_LAUNCH_DIRNAME = "worker-launches"


def create_worker_launch_directory(root: Path, job_id: str) -> Path:
    """Create an owner-controlled launch directory outside worker scratch.

    The native worker policies deny the stable parent. A fresh child per job
    prevents one provider run from reusing or replacing another run's files.
    """
    parent = root.expanduser().resolve() / WORKER_LAUNCH_DIRNAME
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    before = parent.lstat()
    if (
        not stat.S_ISDIR(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError(
            "worker launch root must be an owner-controlled non-symlink directory"
        )
    os.chmod(parent, 0o700)
    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "-", job_id).strip("-") or "job"
    directory = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=parent))
    os.chmod(directory, 0o700)
    return directory
