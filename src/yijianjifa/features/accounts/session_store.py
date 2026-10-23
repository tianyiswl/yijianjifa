from __future__ import annotations

import hashlib
import os
import tempfile
import re
import secrets
from pathlib import Path
import shutil


class SessionStore:
    """Account-isolated session payload storage outside SQLite.

    V1 browser/session bytes are never stored in the normal application database.
    The filesystem representation is deliberately account-isolated and permission-
    restricted; the live Playwright adapter can later use the same account profile
    directory for its browser state.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._private_directory(self.root)

    def put(self, account_id: str, secret_blob: bytes, *, versioned: bool = False) -> str:
        path = self._payload_path(account_id)
        if versioned:
            path = path.with_name("session-" + secrets.token_hex(24) + ".bin")
        self.ensure_profile_dir(account_id)
        fd, temporary = tempfile.mkstemp(prefix='.session-', suffix='.tmp', dir=path.parent)
        temporary = Path(temporary)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(bytes(secret_blob))
                stream.flush()
                os.fsync(stream.fileno())
            # Same-directory replacement preserves the old file on write failure.
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        relative = path.relative_to(self.root).as_posix()
        return f"profile:{relative}"

    def get(self, account_id: str, *, ref: str | None = None) -> bytes | None:
        path = self._payload_path(account_id)
        if ref is not None:
            prefix = 'profile:' + path.parent.relative_to(self.root).as_posix() + '/'
            if not isinstance(ref, str) or not ref.startswith(prefix):
                raise ValueError('invalid account session reference')
            name = ref[len(prefix):]
            if not re.fullmatch(r'session(?:-[a-f0-9]{48})?\.bin', name):
                raise ValueError('invalid account session reference')
            path = path.with_name(name)
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError('session reference must not be a symbolic link')
        return path.read_bytes() if path.is_file() else None

    def delete(self, account_id: str) -> None:
        directory = self._payload_path(account_id).parent
        if directory.exists():
            shutil.rmtree(directory)

    def profile_dir(self, account_id: str) -> Path:
        return self._payload_path(account_id).parent

    def ensure_profile_dir(self, account_id: str) -> Path:
        directory = self.profile_dir(account_id)
        self._private_directory(directory)
        return directory

    @staticmethod
    def _private_directory(directory: Path) -> None:
        if directory.is_symlink():
            raise ValueError('session directory must not be a symbolic link')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt':
            # Fail before writing secrets when privacy cannot be established.
            directory.chmod(0o700)

    def _payload_path(self, account_id: str) -> Path:
        digest = hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()[:24]
        return self.root / digest / "session.bin"
