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
