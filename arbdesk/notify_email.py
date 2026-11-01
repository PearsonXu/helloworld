"""Email alerts for black-swan events (SMTP).

After every scan, new alerts that match the configured categories and level are
sent as ONE digest email; at most `max_per_hour` emails go out per hour, so a
volatile hour can't flood your inbox. The SMTP password is read from the
encrypted vault only (`vault set SMTP_PASSWORD`); for Gmail use an App Password.

Only market information is sent (titles and explanations of the alerts) —
never keys, balances or trade records.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

log = logging.getLogger(__name__)

LEVELS = {"info": 0, "warning": 1, "critical": 2}
CATEGORY_LABELS = {
    "move1h": "1 小时急涨急跌", "move24h": "24 小时大幅波动", "depeg": "稳定币脱锚", "ivjump": "波动率飙升",
    "ivhigh": "波动率极高", "funding": "资金费率极端", "tail_cheap": "便宜的黑天鹅彩票", "dip_fill": "抄底挂单成交",
    "outage": "数据源中断", "sim_end": "模拟结束", "dip_rearm": "抄底挂单需要重新布置",
}


def category(alert_key: str) -> str:
    return alert_key.split(":", 1)[0]


def smtp_send(cfg, password: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr or cfg.username
    msg["To"] = cfg.to_addr
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if cfg.security == "ssl":
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=20) as s:
            s.login(cfg.username, password)
            s.send_message(msg)
    else:  # STARTTLS (port 587); plain-text SMTP is not offered
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(cfg.username, password)
            s.send_message(msg)


class EmailNotifier:
    def __init__(self, settings, store, sender=smtp_send):
        self.settings = settings
        self.store = store
        self.sender = sender

    @property
    def cfg(self):
        return self.settings.email

    def problems(self) -> list[str]:
        c, out = self.cfg, []
        if not c.enabled:
            out.append("email.enabled = false")
        for f in ("smtp_host", "username", "to_addr"):
            if not getattr(c, f):
                out.append(f"email.{f} is empty")
        if not self.settings.secrets.smtp_password:
            out.append("SMTP_PASSWORD is not in the vault (python -m arbdesk vault set SMTP_PASSWORD)")
        return out

    def _wanted(self, a: dict) -> bool:
        return (LEVELS.get(a["level"], 0) >= LEVELS.get(self.cfg.min_level, 1)
                and category(a["key"]) in self.cfg.categories)

    def run(self) -> dict:
        """Send one digest of new matching alerts (if any). Safe to call every scan."""
        if not self.cfg.enabled:
            return {"skipped": "disabled"}
        if (p := self.problems()):
            return {"skipped": "; ".join(p)}
        # Only recent alerts: enabling email later must not mail out old history.
        since = (datetime.now(timezone.utc) - timedelta(hours=self.cfg.lookback_hours)).isoformat(timespec="seconds")
        new = self.store.alerts_to_email(since)
        wanted = [a for a in new if self._wanted(a)]
        skipped = [a["id"] for a in new if not self._wanted(a)]
        if skipped:
            self.store.mark_alerts_emailed(skipped, 2)  # 2 = not an email category
        if not wanted:
            return {"sent": 0}
        if self.store.emails_sent_since(datetime.now(timezone.utc) - timedelta(hours=1)) >= self.cfg.max_per_hour:
            return {"deferred": len(wanted), "reason": "hourly email cap reached"}  # retried next scan
        worst = max(wanted, key=lambda a: LEVELS.get(a["level"], 0))
        subject = f"{self.cfg.subject_prefix} {'【严重】' if worst['level'] == 'critical' else ''}{worst['title']}"
        if len(wanted) > 1:
            subject += f"（另有 {len(wanted) - 1} 条）"
        body = self.body(wanted)
        try:
            self.sender(self.cfg, self.settings.secrets.smtp_password, subject, body)
        except Exception as e:
            self.store.log_email(subject, len(wanted), ok=False, error=f"{type(e).__name__}: {e}")
            log.warning("email alert failed: %s", e)
            return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        self.store.log_email(subject, len(wanted), ok=True)
        self.store.mark_alerts_emailed([a["id"] for a in wanted], 1)
        return {"sent": 1, "alerts": len(wanted)}

    @staticmethod
    def body(alerts: list[dict]) -> str:
        lines = ["ArbDesk 行情提醒", ""]
        for a in sorted(alerts, key=lambda a: (-LEVELS.get(a["level"], 0), a["id"])):
            tag = {"critical": "严重", "warning": "注意"}.get(a["level"], "提示")
            lines += [f"[{tag}] {a['title']}",
                      f"  类型：{CATEGORY_LABELS.get(category(a['key']), category(a['key']))} · 时间（UTC）：{a['ts'][:16]}",
                      f"  {a['detail']}", ""]
        lines += ["这是模拟/提醒系统发出的通知，不会自动下任何真实订单。",
                  "详情请在电脑上打开 ArbDesk 面板（只能在本机访问）。"]
        return "\n".join(lines)

    def send_test(self) -> dict:
        if (p := [x for x in self.problems() if x != "email.enabled = false"]):
            return {"ok": False, "problems": p}
        subject = f"{self.cfg.subject_prefix} 测试邮件"
        try:
            self.sender(self.cfg, self.settings.secrets.smtp_password, subject,
                        "这是一封测试邮件：ArbDesk 的邮件提醒配置正确。\n\n" + self.body([{
                            "id": 0, "key": "move1h:BTC:down", "level": "warning", "ts": datetime.now(timezone.utc).isoformat(),
                            "title": "（示例）BTC 1 小时暴跌 -6.0%", "detail": "这只是格式示例，不是真实行情。"}]))
        except Exception as e:
            self.store.log_email(subject, 0, ok=False, error=f"{type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self.store.log_email(subject, 0, ok=True)
        return {"ok": True, "to": self.cfg.to_addr}
