"""本地配置：Kindle 收件地址与发件邮箱 SMTP 设置，保存在 ~/.kindle_sender/config.json。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields

from .validator import AMAZON_EMAIL_LIMIT_MB, Limits

CONFIG_PATH = os.environ.get(
    "KINDLE_SENDER_CONFIG",
    os.path.join(os.path.expanduser("~"), ".kindle_sender", "config.json"),
)


@dataclass
class Config:
    kindle_email: str = ""
    sender_email: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: str = "ssl"  # ssl | starttls | none
    smtp_username: str = ""
    smtp_password: str = ""
    max_file_mb: float = AMAZON_EMAIL_LIMIT_MB
    smtp_limit_mb: float = 50

    @property
    def limits(self) -> Limits:
        return Limits(max_file_mb=self.max_file_mb, smtp_limit_mb=self.smtp_limit_mb)

    @property
    def password(self) -> str:
        return os.environ.get("KINDLE_SMTP_PASSWORD") or self.smtp_password

    def public_dict(self) -> dict:
        data = asdict(self)
        data.pop("smtp_password")
        data["has_password"] = bool(self.password)
        return data

    def update(self, data: dict) -> None:
        for f in fields(self):
            if f.name not in data:
                continue
            value = data[f.name]
            # 前端留空密码表示保持原密码不变
            if f.name == "smtp_password" and not value:
                continue
            if f.type in ("int", int):
                value = int(value)
            elif f.type in ("float", float):
                value = float(value)
            else:
                value = str(value).strip()
            setattr(self, f.name, value)

    def errors(self) -> list[str]:
        errs = []
        kindle = self.kindle_email.lower()
        if not kindle:
            errs.append("请填写 Kindle 收件邮箱")
        elif not kindle.endswith(("@kindle.com", "@kindle.cn", "@free.kindle.com")):
            errs.append("Kindle 邮箱应以 @kindle.com 结尾")
        if "@" not in self.sender_email:
            errs.append("请填写发件邮箱")
        if not self.smtp_host:
            errs.append("请填写 SMTP 服务器")
        if self.smtp_security not in ("ssl", "starttls", "none"):
            errs.append("SMTP 加密方式无效")
        return errs


def load_config(path: str = CONFIG_PATH) -> Config:
    cfg = Config()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg: Config, path: str = CONFIG_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, ensure_ascii=False, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
