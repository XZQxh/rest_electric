"""测试辅助：在工作区内创建临时目录。

不用标准库 tempfile：当前 Windows 沙盒对 mkdtemp 创建的目录拒绝写入。
测试仍然是自包含的，跑完自动清理。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / ".test-tmp"


class WorkspaceTmpDir:
    def __init__(self, name: str = "case") -> None:
        self.path = BASE / f"{name}-{uuid.uuid4().hex[:10]}"
        self.path.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
