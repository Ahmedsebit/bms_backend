import hashlib
import hmac
import json
import unicodedata
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from bms.config import settings

hasher = PasswordHasher()
dummy_hash = hasher.hash("not-a-real-password")

COMMON_PASSWORDS = {
    "passwordpassword",
    "password123456789",
    "123456789012345",
    "12345678901234567890",
    "qwertyuiopasdfghjkl",
    "letmeinletmeinletmein",
}

CAPABILITIES = {
    "officer": {"inspection.execute", "finding.create", "action.complete_owned"},
    "supervisor": {
        "inspection.execute",
        "finding.create",
        "finding.assign",
        "action.complete_owned",
        "action.verify",
        "action.reject",
        "overview.read",
    },
    "administrator": {
        "setup.manage",
        "users.manage",
        "finding.create",
        "finding.assign",
        "action.complete_owned",
        "action.verify",
        "action.reject",
        "overview.read",
        "audit.read",
    },
    "leadership": {"overview.read", "audit.read"},
}


def normalize_password(password: str) -> str:
    return unicodedata.normalize("NFC", password)


def validate_new_password(password: str) -> str:
    password = normalize_password(password)
    if not settings().password_min_length <= len(password) <= 128:
        raise ValueError("invalid_password_length")
    if not password.strip() or len(set(password)) == 1 or password.casefold() in COMMON_PASSWORDS:
        raise ValueError("common_password")
    return password


def hash_password(password: str) -> str:
    return hasher.hash(normalize_password(password))


def verify_password(password: str, encoded: str | None) -> bool:
    try:
        valid = hasher.verify(encoded or dummy_hash, normalize_password(password))
        return valid and encoded is not None
    except VerificationError:
        return False


def token_for(session_id: UUID) -> str:
    signature = hmac.new(
        settings().secret_key.get_secret_value().encode(),
        f"session:{session_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{session_id}.{signature}"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def request_digest(command: str, payload: dict) -> str:
    data = json.dumps([command, payload], sort_keys=True, default=str).encode()
    return hmac.new(
        settings().secret_key.get_secret_value().encode(), data, hashlib.sha256
    ).hexdigest()
