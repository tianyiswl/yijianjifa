from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: str | Path, block_size: int = 1024 * 1024) -> str:
    file_path = Path(path)
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for block in iter(lambda: handle.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()
