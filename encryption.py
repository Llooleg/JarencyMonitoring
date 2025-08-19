import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


class DatabaseEncryption:
    def __init__(self, password: str = None, direct_key: str = None):
        """Initialize encryption with either password or direct key"""
        if direct_key:
            self.cipher = Fernet(direct_key.encode() if isinstance(direct_key, str) else direct_key)
        elif password:
            salt = os.environ.get('DB_SALT', 'default_salt_change_this').encode()
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
            self.cipher = Fernet(key)
        else:
            raise ValueError("Either password or direct_key must be provided")

    def encrypt_text(self, text: str) -> str:
        """Encrypt text and return base64 encoded string"""
        if not text or text == "":
            return text
        try:
            encrypted = self.cipher.encrypt(text.encode())
            return base64.urlsafe_b64encode(encrypted).decode()
        except Exception as e:
            print(f"Encryption error for text: {text[:50]}... - {e}")
            return text

    def decrypt_text(self, encrypted_text: str) -> str:
        """Decrypt base64 encoded encrypted text"""
        if not encrypted_text or encrypted_text == "":
            return encrypted_text

        try:
            encrypted_bytes = base64.urlsafe_b64decode(encrypted_text.encode())
            decrypted = self.cipher.decrypt(encrypted_bytes)
            return decrypted.decode()
        except Exception:
            print(f"Decryption failed for: {encrypted_text[:50]}... - assuming plaintext")
            return encrypted_text

