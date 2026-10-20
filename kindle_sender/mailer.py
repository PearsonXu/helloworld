"""通过 SMTP 把书籍作为附件发送到 Kindle 邮箱。"""

from __future__ import annotations

import mimetypes
import smtplib
import ssl
from email.message import EmailMessage

from .config import Config

TIMEOUT = 60


def connect(cfg: Config) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if cfg.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT)
        if cfg.smtp_security == "starttls":
            server.starttls(context=context)
    username = cfg.smtp_username or cfg.sender_email
    if cfg.password:
        server.login(username, cfg.password)
    return server


def build_message(cfg: Config, path: str, filename: str, convert: bool = False) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.sender_email
    msg["To"] = cfg.kindle_email
    # 主题为 "convert" 时 Amazon 会把 PDF 转换为可调整字号的 Kindle 格式
    msg["Subject"] = "convert" if convert and filename.lower().endswith(".pdf") else filename
    msg.set_content("Sent by Kindle Sender.")

    ctype, _ = mimetypes.guess_type(filename)
    maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
    with open(path, "rb") as f:
        msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=filename)
    return msg


def send_book(server: smtplib.SMTP, cfg: Config, path: str, filename: str, convert: bool = False) -> None:
    server.send_message(build_message(cfg, path, filename, convert))
