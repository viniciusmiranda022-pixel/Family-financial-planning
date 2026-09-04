from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class EncryptedDocumentStore:
    def __init__(self) -> None:
        settings = get_settings()
        self.base_dir = settings.documents_dir
        self.base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cipher = Fernet(settings.file_encryption_key.encode())

    def save(self, document_id: str, payload: bytes) -> str:
        path = self.base_dir / f"{document_id}.bin"
        path.write_bytes(self.cipher.encrypt(payload))
        path.chmod(0o600)
        return str(path)

    def read(self, encrypted_path: str) -> bytes:
        try:
            return self.cipher.decrypt(Path(encrypted_path).read_bytes())
        except InvalidToken as exc:
            raise ValueError("Não foi possível descriptografar o documento") from exc

    def delete(self, encrypted_path: str) -> None:
        """Remove one encrypted artifact, idempotently.

        Used only to undo this store's own `save()` when the `Document` row
        that was meant to own the artifact fails to commit (see
        `app.api._import_one_document`) -- never as a general-purpose or
        bulk delete, and never for an artifact whose owning row already
        committed. A missing file (already removed, or `save()` never
        reached) is not an error: the caller's intent ("this path must not
        exist") is already satisfied.
        """

        Path(encrypted_path).unlink(missing_ok=True)
