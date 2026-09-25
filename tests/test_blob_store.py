import errno
import io
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from image23mf.storage import ContentAddressedStore
from image23mf.storage.blob_store import BlobCollisionError, InvalidBlobPathError


class ExplodingStream(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.calls = 0

    def read(self, size: int = -1) -> bytes:
        self.calls += 1
        if self.calls > 1:
            raise OSError("simulated interrupted source")
        return super().read(4)


def test_blob_is_hashed_sharded_and_deduplicated(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path)
    first = store.put_bytes(
        b"same image", namespace="assets", extension=".PNG", media_type="image/png"
    )
    second = store.put_bytes(
        b"same image", namespace="assets", extension=".png", media_type="image/png"
    )

    assert first == second
    assert first.relative_path == f"assets/{first.sha256[:2]}/{first.sha256}.png"
    assert store.path_for(first.relative_path).read_bytes() == b"same image"
    assert store.verify(first)
    assert len(list((tmp_path / "assets").rglob("*.png"))) == 1
    assert not list((tmp_path / "temp").iterdir())


def test_corrupted_existing_hash_path_is_never_overwritten(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path)
    blob = store.put_bytes(
        b"original", namespace="artifacts", extension=".3mf", media_type="model/3mf"
    )
    destination = store.path_for(blob.relative_path)
    destination.write_bytes(b"corrupt!")

    with pytest.raises(BlobCollisionError, match="hash verification"):
        store.put_bytes(
            b"original", namespace="artifacts", extension=".3mf", media_type="model/3mf"
        )

    assert destination.read_bytes() == b"corrupt!"
    assert not store.verify(blob)


def test_interrupted_source_cleans_temporary_file(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path)

    with pytest.raises(OSError, match="interrupted source"):
        store.put_stream(
            ExplodingStream(b"more than four bytes"),
            namespace="assets",
            extension=".png",
            media_type="image/png",
        )

    assert not list((tmp_path / "temp").iterdir())


@pytest.mark.parametrize("error", [PermissionError("read only"), OSError(errno.ENOSPC, "full")])
def test_publication_failure_cleans_temp_and_preserves_no_partial_target(
    tmp_path, error: OSError
) -> None:
    store = ContentAddressedStore(tmp_path)

    with patch.object(os, "replace", side_effect=error), pytest.raises(type(error)):
        store.put_bytes(b"payload", namespace="artifacts", extension=".stl", media_type="model/stl")

    assert not list((tmp_path / "temp").iterdir())
    assert not list((tmp_path / "artifacts").rglob("*.stl"))


@pytest.mark.parametrize(
    "relative", ["../secret", "/tmp/secret", "assets/../../secret", "assets/link/../.."]
)
def test_path_traversal_is_rejected(tmp_path, relative: str) -> None:
    store = ContentAddressedStore(tmp_path)
    with pytest.raises(InvalidBlobPathError):
        store.path_for(relative)


@pytest.mark.parametrize("namespace", ["", "asset", "../assets", "Assets"])
def test_unknown_namespace_is_rejected(tmp_path, namespace: str) -> None:
    store = ContentAddressedStore(tmp_path)
    with pytest.raises(InvalidBlobPathError):
        store.put_bytes(b"x", namespace=namespace, extension=".png", media_type="image/png")


def test_non_binary_stream_and_invalid_metadata_are_rejected(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path)
    with pytest.raises(TypeError, match="must return bytes"):
        store.put_stream(
            io.StringIO("text"),
            namespace="assets",
            extension=".png",
            media_type="image/png",
        )
    with pytest.raises(ValueError, match="extension"):
        store.put_bytes(b"x", namespace="assets", extension="../png", media_type="image/png")
    with pytest.raises(ValueError, match="media_type"):
        store.put_bytes(b"x", namespace="assets", extension=".png", media_type="png")


def test_returned_paths_are_relative_to_workspace(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path)
    blob = store.put_bytes(
        b"x", namespace="cache", extension=".bin", media_type="application/octet-stream"
    )
    assert not Path(blob.relative_path).is_absolute()
