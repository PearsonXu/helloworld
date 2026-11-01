from datetime import datetime, timedelta, timezone

import pytest

from arbdesk.config import load_settings
from arbdesk.notify_email import EmailNotifier
from arbdesk.storage import Store


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "config.toml").write_text(
        f'data_dir = "{(tmp_path / "data").as_posix()}"\n[email]\nenabled = true\n'
        'username = "me@example.com"\nto_addr = "me@example.com"\nmax_per_hour = 2\n')
    s = load_settings(tmp_path / "config.toml", tmp_path / ".env", vault_data={"SMTP_PASSWORD": "app-pass-123"})
    store = Store(s.data_dir / "t.db")
    sent = []
    n = EmailNotifier(s, store, sender=lambda cfg, pw, subj, body: sent.append((pw, subj, body)))
    return s, store, n, sent


def test_digest_of_matching_alerts_only(setup):
    s, store, n, sent = setup
    store.add_alert("move1h:BTC:down", "critical", "BTC 1 小时暴跌 -13.0%", "detail A")
    store.add_alert("tail_cheap:kalshi:X:YES", "warning", "发现便宜的黑天鹅彩票", "detail B")
    store.add_alert("sim_end:1", "info", "模拟结束", "not an email category")
    assert n.run() == {"sent": 1, "alerts": 2}
    (pw, subj, body) = sent[0]
    assert pw == "app-pass-123" and "【严重】BTC 1 小时暴跌" in subj and "另有 1 条" in subj
    assert "detail A" in body and "detail B" in body and "模拟结束" not in body
    assert n.run() == {"sent": 0}                                        # nothing new: no second email


def test_hourly_cap_defers_then_sends(setup):
    s, store, n, sent = setup
    for i in range(3):
        store.add_alert(f"depeg:USDC{i}", "warning", f"脱锚 {i}", "d")
        n.run()
    assert len(sent) == 2                                               # max_per_hour = 2
    assert store.alerts_to_email("2000-01-01")                          # third alert kept for later
    store.conn.execute("UPDATE email_log SET ts = ?", ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
    n.run()
    assert len(sent) == 3


def test_failure_is_logged_and_retried(setup):
    s, store, n, sent = setup
    def boom(*a):
        raise OSError("connection refused")
    n.sender = boom
    store.add_alert("move1h:ETH:up", "warning", "ETH 暴涨", "d")
    assert "error" in n.run()
    assert store.email_log(1)[0]["ok"] == 0 and store.alerts_to_email("2000-01-01")
    n.sender = lambda *a: sent.append(a)
    assert n.run()["sent"] == 1


def test_old_alerts_and_low_levels_not_mailed(setup):
    s, store, n, sent = setup
    old = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(timespec="seconds")
    store.conn.execute("INSERT INTO alerts (ts, key, level, title, detail) VALUES (?, 'move1h:BTC:up', 'warning', 'old', 'd')", (old,))
    s.email.min_level = "critical"
    store.add_alert("move24h:BTC:down", "warning", "warning only", "d")
    assert n.run() == {"sent": 0} and sent == []


def test_password_vault_only_and_problems(tmp_path):
    (tmp_path / "config.toml").write_text(f'data_dir = "{(tmp_path / "data").as_posix()}"\n[email]\nenabled = true\n')
    (tmp_path / ".env").write_text("SMTP_PASSWORD=plaintext\n")
    s = load_settings(tmp_path / "config.toml", tmp_path / ".env")
    assert s.secrets.smtp_password == ""
    n = EmailNotifier(s, Store(s.data_dir / "t.db"), sender=lambda *a: None)
    probs = n.problems()
    assert any("SMTP_PASSWORD" in p for p in probs) and any("to_addr" in p for p in probs)
    assert "skipped" in n.run() and n.send_test()["ok"] is False
