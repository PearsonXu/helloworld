import io
import zipfile
from unittest import mock

import pytest

from kindle_sender.app import create_app
from kindle_sender.config import Config
from kindle_sender.mailer import build_message
from kindle_sender.validator import MB, Limits, validate_file


def make_epub(path, mimetype_first=True):
    with zipfile.ZipFile(path, "w") as zf:
        if mimetype_first:
            zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", "<container/>", compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", "<package/>", compress_type=zipfile.ZIP_DEFLATED)
        if not mimetype_first:
            zf.writestr("mimetype", "application/epub+zip")


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_valid_epub(tmp_path):
    p = tmp_path / "book.epub"
    make_epub(p)
    r = validate_file(str(p), "book.epub")
    assert r.ok and r.detected == "epub" and not r.warnings


def test_epub_mimetype_not_first_warns(tmp_path):
    p = tmp_path / "book.epub"
    make_epub(p, mimetype_first=False)
    r = validate_file(str(p), "book.epub")
    assert r.ok and any("mimetype" in w for w in r.warnings)


def test_valid_pdf(tmp_path):
    p = write(tmp_path, "a.pdf", b"%PDF-1.4\n...\n%%EOF\n")
    assert validate_file(p, "a.pdf").ok


def test_mobi_rejected_with_hint(tmp_path):
    p = write(tmp_path, "a.mobi", b"\0" * 60 + b"BOOKMOBI" + b"\0" * 100)
    r = validate_file(p, "a.mobi")
    assert not r.ok and "EPUB" in r.errors[0]


def test_mislabeled_mobi_as_epub(tmp_path):
    p = write(tmp_path, "a.epub", b"\0" * 60 + b"BOOKMOBI" + b"\0" * 100)
    r = validate_file(p, "a.epub")
    assert not r.ok and r.detected == "mobi"


def test_pdf_named_txt_rejected(tmp_path):
    p = write(tmp_path, "a.txt", b"%PDF-1.4\n%%EOF")
    r = validate_file(p, "a.txt")
    assert not r.ok and "PDF" in r.errors[0]


def test_gbk_txt_warns(tmp_path):
    p = write(tmp_path, "a.txt", "第一章 天下大势，分久必合，合久必分。".encode("gbk") * 20)
    r = validate_file(p, "a.txt")
    assert r.ok and any("GB18030" in w for w in r.warnings)


def test_utf8_txt_ok(tmp_path):
    p = write(tmp_path, "a.txt", "第一章 你好".encode("utf-8") * 20)
    r = validate_file(p, "a.txt")
    assert r.ok and not r.warnings


def test_empty_file(tmp_path):
    p = write(tmp_path, "a.pdf", b"")
    assert not validate_file(p, "a.pdf").ok


def test_too_large(tmp_path):
    p = write(tmp_path, "a.pdf", b"%PDF-1.4\n" + b"0" * (2 * MB))
    r = validate_file(p, "a.pdf", Limits(max_file_mb=1, smtp_limit_mb=50))
    assert not r.ok and "超过上限" in r.errors[0]


def test_smtp_limit_accounts_for_base64(tmp_path):
    # 1.8MB 文件编码后约 2.4MB，超过 2MB 的邮箱上限
    p = write(tmp_path, "a.pdf", b"%PDF-1.4\n" + b"0" * int(1.8 * MB))
    r = validate_file(p, "a.pdf", Limits(max_file_mb=50, smtp_limit_mb=2))
    assert not r.ok and "编码后" in r.errors[0]


def test_unknown_extension(tmp_path):
    p = write(tmp_path, "a.xyz", b"hello")
    assert not validate_file(p, "a.xyz").ok


def test_build_message_attaches_with_unicode_name(tmp_path):
    p = write(tmp_path, "x", b"%PDF-1.4\n%%EOF")
    cfg = Config(kindle_email="me@kindle.com", sender_email="me@qq.com", smtp_host="smtp.qq.com")
    msg = build_message(cfg, p, "三国演义.pdf", convert=True)
    att = next(msg.iter_attachments())
    assert att.get_filename() == "三国演义.pdf"
    assert att.get_content_type() == "application/pdf"
    assert msg["Subject"] == "convert"


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "config.json"))
    return app.test_client()


def configure(client):
    res = client.post("/api/config", json={
        "kindle_email": "me@kindle.com",
        "sender_email": "me@qq.com",
        "smtp_host": "smtp.qq.com",
        "smtp_port": "465",
        "smtp_security": "ssl",
        "smtp_password": "secret",
    })
    assert res.status_code == 200
    assert "smtp_password" not in res.get_json()


def test_config_rejects_non_kindle_address(client):
    res = client.post("/api/config", json={"kindle_email": "a@b.com", "sender_email": "x@y.com", "smtp_host": "h"})
    assert res.status_code == 400


def test_upload_check_and_send(client):
    configure(client)
    res = client.post("/api/files", data={"files": [
        (io.BytesIO(b"%PDF-1.4\n%%EOF"), "good.pdf"),
        (io.BytesIO(b"\0" * 60 + b"BOOKMOBI" + b"\0" * 10), "bad.mobi"),
    ]}, content_type="multipart/form-data")
    files = res.get_json()["files"]
    good, bad = files
    assert good["ok"] and not bad["ok"]

    with mock.patch("kindle_sender.mailer.connect") as connect:
        server = connect.return_value
        res = client.post("/api/send", json={"ids": [good["id"], bad["id"]]})
    results = {r["id"]: r for r in res.get_json()["results"]}
    assert results[good["id"]]["ok"]
    assert not results[bad["id"]]["ok"]
    assert server.send_message.call_count == 1
    msg = server.send_message.call_args[0][0]
    assert msg["To"] == "me@kindle.com"


def test_large_utf8_txt_truncated_mid_char(tmp_path):
    # 文件超过检测读取长度，截断点落在多字节字符中间也不应误判
    p = write(tmp_path, "a.txt", b"a" + "中文".encode("utf-8") * 40000)
    r = validate_file(p, "a.txt")
    assert r.ok and r.detected == "txt" and not r.warnings
