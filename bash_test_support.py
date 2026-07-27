import os
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence


def _bash_executable() -> str:
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            git_bash = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if git_bash.is_file():
                return str(git_bash)
    return shutil.which("bash") or "bash"


def run_bash(
    arguments: Sequence[str] = (),
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    input_bytes = None if input_text is None else input_text.encode("utf-8")
    result = subprocess.run(
        [_bash_executable(), *arguments],
        cwd=cwd,
        env=env,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    return subprocess.CompletedProcess(
        args=result.args,
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8", errors="replace"),
        stderr=result.stderr.decode("utf-8", errors="replace"),
    )
