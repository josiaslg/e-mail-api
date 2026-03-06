import hashlib
import hmac
import imaplib
import logging
import os
import secrets
import socket
import smtplib
import ssl
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, EmailStr, Field

load_dotenv()


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).isoformat()
        client_ip = getattr(record, "client_ip", "unknown")
        action = getattr(record, "action", "unknown")
        op_status = getattr(record, "op_status", "unknown")
        return (
            f"timestamp={ts} level={record.levelname} client_ip={client_ip} "
            f"action={action} status={op_status}"
        )


logger = logging.getLogger("mail_gateway")
handler = logging.StreamHandler()
handler.setFormatter(StructuredFormatter())
logger.addHandler(handler)
logger.setLevel(logging.INFO)

app = FastAPI(title="Python-MTA-API-Gateway", version="1.2.0")
security = HTTPBasic(auto_error=False)


class SMTPRequest(BaseModel):
    to: EmailStr
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=100_000)
    user_auth: str | None = None
    pass_auth: str | None = None


class IMAPRequest(BaseModel):
    user_auth: str | None = None
    pass_auth: str | None = None
    limit: int = Field(default=10, ge=1, le=100)


class InboxMessage(BaseModel):
    uid: str
    subject: str
    sender: str
    date: str | None
    body: str


class ActionResponse(BaseModel):
    status: Literal["success", "error"]
    detail: str


class MailSecurityMode:
    AUTODETECT = "AUTODETECT"
    NONE = "NONE"
    TLS = "TLS"
    SSL = "SSL"


class MailAuthMethod:
    AUTODETECT = "AUTODETECT"
    NONE = "NONE"
    PLAIN = "PLAIN"
    MD5 = "MD5"


class ErrorDetail:
    INVALID_CONFIG = "Invalid gateway/mail service configuration"
    MAIL_TIMEOUT = "Mail service timeout"
    MAIL_CONNECT = "Mail service connection failed"
    MAIL_TLS = "Mail service TLS/SSL negotiation failed"


def _get_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def _get_mail_host(service_name: str) -> str:
    specific = os.getenv(f"{service_name}_HOST")
    return specific or _get_env("MAIL_SERVER_HOST")


def _get_mode(name: str, default: str, valid_values: set[str]) -> str:
    mode = os.getenv(name, default).upper()
    if mode not in valid_values:
        raise RuntimeError(f"Invalid value for {name}: {mode}")
    return mode


def _get_port(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer for {name}: {raw}") from exc
    if not 1 <= value <= 65535:
        raise RuntimeError(f"Port out of range for {name}: {value}")
    return value


def _resolve_credentials(
    user_auth: str | None,
    pass_auth: str | None,
    basic_auth: HTTPBasicCredentials | None,
    auth_method: str,
) -> tuple[str, str]:
    if auth_method == MailAuthMethod.NONE:
        return "", ""

    username = user_auth or (basic_auth.username if basic_auth else None)
    password = pass_auth or (basic_auth.password if basic_auth else None)
    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mail credentials are required via body or Basic Auth",
        )
    return username, password


async def require_api_key(request: Request, x_api_key: str = Header(default="")) -> None:
    expected = _get_env("GATEWAY_API_KEY")
    if not secrets.compare_digest(x_api_key, expected):
        logger.warning(
            "request denied",
            extra={
                "client_ip": request.client.host if request.client else "unknown",
                "action": "auth",
                "op_status": "denied",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-API-KEY",
        )


def _smtp_connection(host: str, port: int, security_mode: str) -> smtplib.SMTP | smtplib.SMTP_SSL:
    timeout = int(os.getenv("MAIL_TIMEOUT_SECONDS", "20"))
    ctx = ssl.create_default_context()

    selected_mode = security_mode
    if security_mode == MailSecurityMode.AUTODETECT:
        selected_mode = MailSecurityMode.SSL if port == 465 else MailSecurityMode.TLS

    if selected_mode == MailSecurityMode.SSL:
        return smtplib.SMTP_SSL(host=host, port=port, timeout=timeout, context=ctx)

    smtp = smtplib.SMTP(host=host, port=port, timeout=timeout)
    smtp.ehlo_or_helo_if_needed()
    if selected_mode == MailSecurityMode.TLS:
        smtp.starttls(context=ctx)
        smtp.ehlo_or_helo_if_needed()
    return smtp


def _smtp_authenticate(smtp: smtplib.SMTP | smtplib.SMTP_SSL, username: str, password: str, auth_method: str) -> None:
    selected_method = auth_method
    if auth_method == MailAuthMethod.AUTODETECT:
        selected_method = MailAuthMethod.PLAIN

    if selected_method == MailAuthMethod.NONE:
        return

    if selected_method == MailAuthMethod.MD5:
        smtp.user = username
        smtp.password = password
        smtp.auth("CRAM-MD5", smtp.auth_cram_md5)
        return

    smtp.login(username, password)


def _imap_connection(host: str, port: int, security_mode: str) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
    timeout = int(os.getenv("MAIL_TIMEOUT_SECONDS", "20"))
    selected_mode = security_mode

    if security_mode == MailSecurityMode.AUTODETECT:
        selected_mode = MailSecurityMode.SSL if port == 993 else MailSecurityMode.TLS

    if selected_mode == MailSecurityMode.SSL:
        return imaplib.IMAP4_SSL(host=host, port=port, timeout=timeout)

    conn = imaplib.IMAP4(host=host, port=port, timeout=timeout)
    if selected_mode == MailSecurityMode.TLS:
        conn.starttls(ssl_context=ssl.create_default_context())
    return conn


def _imap_authenticate(mail: imaplib.IMAP4 | imaplib.IMAP4_SSL, username: str, password: str, auth_method: str) -> None:
    selected_method = auth_method
    if auth_method == MailAuthMethod.AUTODETECT:
        selected_method = MailAuthMethod.PLAIN

    if selected_method == MailAuthMethod.NONE:
        return

    if selected_method == MailAuthMethod.MD5:
        def cram_md5_auth(challenge: bytes) -> bytes:
            digest = hmac.new(password.encode("utf-8"), challenge, hashlib.md5).hexdigest()
            return f"{username} {digest}".encode("utf-8")

        mail.authenticate("CRAM-MD5", cram_md5_auth)
        return

    mail.login(username, password)


def _log_request(request: Request, action: str, op_status: str, message: str) -> None:
    logger.log(
        logging.INFO if op_status == "success" else logging.WARNING,
        message,
        extra={
            "client_ip": request.client.host if request.client else "unknown",
            "action": action,
            "op_status": op_status,
        },
    )


def _raise_service_error(request: Request, action: str, exc: Exception) -> None:
    _log_request(request, action, "error", exc.__class__.__name__)
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(status_code=500, detail=ErrorDetail.INVALID_CONFIG) from exc
    if isinstance(exc, TimeoutError | socket.timeout):
        raise HTTPException(status_code=504, detail=ErrorDetail.MAIL_TIMEOUT) from exc
    if isinstance(exc, ssl.SSLError):
        raise HTTPException(status_code=502, detail=ErrorDetail.MAIL_TLS) from exc
    if isinstance(exc, OSError):
        raise HTTPException(status_code=502, detail=ErrorDetail.MAIL_CONNECT) from exc
    raise HTTPException(status_code=502, detail=f"Upstream error: {exc.__class__.__name__}") from exc


@app.get("/healthz")
def healthcheck() -> ActionResponse:
    _get_env("GATEWAY_API_KEY")
    _get_mail_host("SMTP")
    _get_mail_host("IMAP")
    _get_port("SMTP_PORT", 587)
    _get_port("IMAP_PORT", 993)
    _get_mode(
        "SMTP_SECURITY_MODE",
        MailSecurityMode.AUTODETECT,
        {MailSecurityMode.AUTODETECT, MailSecurityMode.NONE, MailSecurityMode.TLS, MailSecurityMode.SSL},
    )
    _get_mode(
        "IMAP_SECURITY_MODE",
        MailSecurityMode.AUTODETECT,
        {MailSecurityMode.AUTODETECT, MailSecurityMode.NONE, MailSecurityMode.TLS, MailSecurityMode.SSL},
    )
    _get_mode(
        "SMTP_AUTH_METHOD",
        MailAuthMethod.AUTODETECT,
        {MailAuthMethod.AUTODETECT, MailAuthMethod.NONE, MailAuthMethod.PLAIN, MailAuthMethod.MD5},
    )
    _get_mode(
        "IMAP_AUTH_METHOD",
        MailAuthMethod.AUTODETECT,
        {MailAuthMethod.AUTODETECT, MailAuthMethod.NONE, MailAuthMethod.PLAIN, MailAuthMethod.MD5},
    )
    return ActionResponse(status="success", detail="ok")


@app.post("/v1/send", response_model=ActionResponse, dependencies=[Depends(require_api_key)])
@app.post("/send", response_model=ActionResponse, dependencies=[Depends(require_api_key)])
def send_email(
    payload: SMTPRequest,
    request: Request,
    basic_auth: HTTPBasicCredentials | None = Depends(security),
) -> ActionResponse:
    host = _get_mail_host("SMTP")
    port = _get_port("SMTP_PORT", 587)
    security_mode = _get_mode(
        "SMTP_SECURITY_MODE",
        MailSecurityMode.AUTODETECT,
        {MailSecurityMode.AUTODETECT, MailSecurityMode.NONE, MailSecurityMode.TLS, MailSecurityMode.SSL},
    )
    auth_method = _get_mode(
        "SMTP_AUTH_METHOD",
        MailAuthMethod.AUTODETECT,
        {MailAuthMethod.AUTODETECT, MailAuthMethod.NONE, MailAuthMethod.PLAIN, MailAuthMethod.MD5},
    )

    username, password = _resolve_credentials(payload.user_auth, payload.pass_auth, basic_auth, auth_method)

    try:
        with _smtp_connection(host, port, security_mode) as smtp:
            _smtp_authenticate(smtp, username, password, auth_method)
            msg = EmailMessage()
            msg["From"] = username if username else _get_env("SMTP_DEFAULT_FROM", "no-reply@localhost")
            msg["To"] = payload.to
            msg["Subject"] = payload.subject
            msg.set_content(payload.body)
            smtp.send_message(msg)

        _log_request(request, "smtp_send", "success", "smtp send")
        return ActionResponse(status="success", detail="Email sent successfully")
    except Exception as exc:
        _raise_service_error(request, "smtp_send", exc)


@app.post("/v1/inbox", dependencies=[Depends(require_api_key)])
@app.post("/fetch", dependencies=[Depends(require_api_key)])
def fetch_inbox(
    payload: IMAPRequest,
    request: Request,
    basic_auth: HTTPBasicCredentials | None = Depends(security),
) -> dict[str, list[InboxMessage]]:
    host = _get_mail_host("IMAP")
    port = _get_port("IMAP_PORT", 993)
    security_mode = _get_mode(
        "IMAP_SECURITY_MODE",
        MailSecurityMode.AUTODETECT,
        {MailSecurityMode.AUTODETECT, MailSecurityMode.NONE, MailSecurityMode.TLS, MailSecurityMode.SSL},
    )
    auth_method = _get_mode(
        "IMAP_AUTH_METHOD",
        MailAuthMethod.AUTODETECT,
        {MailAuthMethod.AUTODETECT, MailAuthMethod.NONE, MailAuthMethod.PLAIN, MailAuthMethod.MD5},
    )

    username, password = _resolve_credentials(payload.user_auth, payload.pass_auth, basic_auth, auth_method)
    messages: list[InboxMessage] = []

    try:
        with _imap_connection(host, port, security_mode) as mail:
            _imap_authenticate(mail, username, password, auth_method)
            mail.select("INBOX")
            status_code, data = mail.uid("search", None, "ALL")
            if status_code != "OK":
                raise imaplib.IMAP4.error("Search failed")

            uids = data[0].split()[-payload.limit :]
            for uid in reversed(uids):
                fetch_status, fetch_data = mail.uid("fetch", uid, "(RFC822)")
                if fetch_status != "OK" or not fetch_data or not fetch_data[0]:
                    continue

                raw = fetch_data[0][1]
                parsed = message_from_bytes(raw)
                body = ""
                if parsed.is_multipart():
                    for part in parsed.walk():
                        content_type = part.get_content_type()
                        disposition = str(part.get("Content-Disposition", ""))
                        if content_type == "text/plain" and "attachment" not in disposition:
                            charset = part.get_content_charset() or "utf-8"
                            part_payload = part.get_payload(decode=True) or b""
                            body = part_payload.decode(charset, errors="replace")
                            break
                else:
                    charset = parsed.get_content_charset() or "utf-8"
                    payload_bytes = parsed.get_payload(decode=True) or b""
                    body = payload_bytes.decode(charset, errors="replace")

                messages.append(
                    InboxMessage(
                        uid=uid.decode("utf-8", errors="replace"),
                        subject=parsed.get("Subject", ""),
                        sender=parsed.get("From", ""),
                        date=parsed.get("Date"),
                        body=body,
                    )
                )

        _log_request(request, "imap_fetch", "success", "imap fetch")
        return {"messages": messages}
    except Exception as exc:
        _raise_service_error(request, "imap_fetch", exc)
