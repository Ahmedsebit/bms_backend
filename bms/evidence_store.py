import hashlib
import hmac
import io
import secrets
import time
from datetime import UTC, datetime
from uuid import uuid5

from PIL import Image, UnidentifiedImageError

from bms.config import settings
from bms.schemas import Command
from bms.services import ApiError, audit


def ticket(actor, evidence, method, expires):
    message = (
        f"evidence:{actor['tenant_id']}:{actor['id']}:{evidence['id']}:{method}:{expires}"
    ).encode()
    return hmac.new(
        settings().secret_key.get_secret_value().encode(), message, "sha256"
    ).hexdigest()


def check_ticket(actor, evidence, method, expires, signature):
    now = int(time.time())
    if expires < now or expires > now + settings().evidence_ticket_seconds:
        raise ApiError(403, "EVIDENCE_TICKET_EXPIRED")
    if not hmac.compare_digest(ticket(actor, evidence, method, expires), signature):
        raise ApiError(403, "FORBIDDEN")


def file_path(evidence):
    # Storage keys are server-generated and never accepted from a client.
    return settings().evidence_directory.joinpath(*evidence["storage_key"].split("/"))


def save_photo(conn, actor, evidence, content):
    cfg = settings()
    if len(content) != evidence["byte_size"] or len(content) > cfg.evidence_max_bytes:
        raise ApiError(400, "INVALID_EVIDENCE_SIZE")
    if hashlib.sha256(content).hexdigest() != evidence["sha256"]:
        raise ApiError(400, "EVIDENCE_HASH_MISMATCH")
    try:
        with Image.open(io.BytesIO(content)) as image:
            if (
                image.format != "JPEG"
                or max(image.size) > cfg.evidence_max_dimension
                or image.getexif()
            ):
                raise ApiError(400, "INVALID_EVIDENCE_IMAGE")
            image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ApiError(400, "INVALID_EVIDENCE_IMAGE") from None
    path = file_path(evidence)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix("." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    conn.execute(
        "UPDATE bms.evidence SET upload_state='uploaded',received_at=coalesce(received_at,%s),"
        "updated_at=now(),revision=revision+1 WHERE tenant_id=%s AND id=%s "
        "AND upload_state<>'uploaded'",
        (datetime.now(UTC), actor["tenant_id"], evidence["id"]),
    )
    if evidence["upload_state"] != "uploaded":
        audit(
            conn,
            actor,
            "evidence.uploaded",
            evidence["id"],
            Command(
                client_operation_id=uuid5(evidence["id"], "evidence.uploaded"),
                device_occurred_at=evidence["captured_at"],
            ),
            after={"uploadState": "uploaded", "ownerId": str(evidence["owner_id"])},
        )
