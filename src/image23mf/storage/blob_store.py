import hashlib
import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

CHUNK_SIZE = 1024 * 1024
ALLOWED_NAMESPACES = frozenset({"assets", "artifacts", "cache"})
EXTENSION = re.compile(r"^\.[a-z0-9]{1,16}$")


class BlobStoreError(RuntimeError):
    """Base class for content-addressed storage failures."""


class BlobCollisionError(BlobStoreError):
    """The expected hash path already contains different or corrupted bytes."""


class InvalidBlobPathError(BlobStoreError):
    """A path attempted to escape the workspace or use an invalid namespace."""


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    relative_path: str
    byte_size: int
    media_type: str
    extension: str


class ContentAddressedStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.temp_root = self.root / "temp"

    def put_bytes(
        self,
        payload: bytes,
        *,
        namespace: str,
        extension: str,
        media_type: str,
    ) -> StoredBlob:
        return self.put_stream(
            io.BytesIO(payload),
            namespace=namespace,
            extension=extension,
            media_type=media_type,
        )

    def put_stream(
        self,
        source: BinaryIO,
        *,
        namespace: str,
        extension: str,
        media_type: str,
    ) -> StoredBlob:
        extension = self._validate_extension(extension)
        self._validate_namespace(namespace)
        if not media_type or "/" not in media_type:
            raise ValueError("media_type must be a non-empty MIME type")

        self.temp_root.mkdir(parents=True, exist_ok=True)
        temporary_path: Optional[Path] = None
        digest = hashlib.sha256()
        byte_size = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b", prefix="incoming-", dir=self.temp_root, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                while True:
                    chunk = source.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise TypeError("blob source must return bytes")
                    temporary.write(chunk)
                    digest.update(chunk)
                    byte_size += len(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())

            sha256 = digest.hexdigest()
            relative = Path(namespace) / sha256[:2] / f"{sha256}{extension}"
            destination = self._resolve_relative(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)

            if destination.exists():
                self._verify_existing(destination, sha256, byte_size)
                temporary_path.unlink()
            else:
                os.replace(temporary_path, destination)
                temporary_path = None
                self._sync_directory(destination.parent)

            return StoredBlob(
                sha256=sha256,
                relative_path=relative.as_posix(),
                byte_size=byte_size,
                media_type=media_type,
                extension=extension,
            )
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def path_for(self, relative_path: str) -> Path:
        path = self._resolve_relative(Path(relative_path))
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        return path

    def verify(self, blob: StoredBlob) -> bool:
        path = self.path_for(blob.relative_path)
        try:
            self._verify_existing(path, blob.sha256, blob.byte_size)
        except BlobCollisionError:
            return False
        return True

    def _resolve_relative(self, relative: Path) -> Path:
        if relative.is_absolute():
            raise InvalidBlobPathError("blob paths must be relative to the workspace")
        if ".." in relative.parts:
            raise InvalidBlobPathError("blob paths cannot contain parent traversal")
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise InvalidBlobPathError("blob path escapes the workspace") from error
        return candidate

    @staticmethod
    def _validate_namespace(namespace: str) -> None:
        if namespace not in ALLOWED_NAMESPACES:
            raise InvalidBlobPathError(f"unsupported blob namespace: {namespace}")

    @staticmethod
    def _validate_extension(extension: str) -> str:
        normalized = extension.lower()
        if not EXTENSION.fullmatch(normalized):
            raise ValueError("extension must look like .png, .svg, .stl, or .3mf")
        return normalized

    @staticmethod
    def _verify_existing(path: Path, expected_hash: str, expected_size: int) -> None:
        if path.stat().st_size != expected_size:
            raise BlobCollisionError(f"existing blob has unexpected size: {path}")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise BlobCollisionError(f"existing blob failed hash verification: {path}")

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
