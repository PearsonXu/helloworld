"""Web 服务：提供前端页面，以及检查 / 发送书籍的接口。"""

from __future__ import annotations

import atexit
import os
import shutil
import smtplib
import tempfile
import threading
import uuid

from flask import Flask, jsonify, request, send_from_directory

from . import mailer
from .config import CONFIG_PATH, load_config, save_config
from .validator import AMAZON_EMAIL_LIMIT_MB, SUPPORTED_FORMATS, validate_file

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(config_path: str = CONFIG_PATH) -> Flask:
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
    # 允许上传比 Kindle 上限更大的文件，以便给出明确的"超出大小"提示
    app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024

    staging_dir = tempfile.mkdtemp(prefix="kindle_sender_")
    staged: dict[str, dict] = {}
    lock = threading.Lock()

    def current_config():
        return load_config(config_path)

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/config")
    def get_config():
        cfg = current_config()
        data = cfg.public_dict()
        data["supported_formats"] = sorted(SUPPORTED_FORMATS)
        data["amazon_limit_mb"] = AMAZON_EMAIL_LIMIT_MB
        return jsonify(data)

    @app.post("/api/config")
    def set_config():
        cfg = current_config()
        try:
            cfg.update(request.get_json(force=True) or {})
        except (TypeError, ValueError):
            return jsonify(error="配置格式错误，请检查数字字段"), 400
        errors = cfg.errors()
        if errors:
            return jsonify(error="；".join(errors)), 400
        save_config(cfg, config_path)
        return jsonify(cfg.public_dict())

    @app.post("/api/config/test")
    def test_config():
        cfg = current_config()
        errors = cfg.errors()
        if errors:
            return jsonify(ok=False, error="；".join(errors)), 400
        try:
            server = mailer.connect(cfg)
            server.quit()
        except (smtplib.SMTPException, OSError) as e:
            return jsonify(ok=False, error=f"连接失败：{e}"), 502
        return jsonify(ok=True)

    @app.post("/api/files")
    def upload_files():
        limits = current_config().limits
        results = []
        for f in request.files.getlist("files"):
            filename = os.path.basename(f.filename or "unnamed")
            file_id = uuid.uuid4().hex
            path = os.path.join(staging_dir, file_id)
            f.save(path)
            report = validate_file(path, filename, limits)
            with lock:
                staged[file_id] = {"path": path, "filename": filename}
            results.append({"id": file_id, **report.to_dict()})
        return jsonify(files=results)

    @app.delete("/api/files/<file_id>")
    def delete_file(file_id):
        with lock:
            item = staged.pop(file_id, None)
        if item and os.path.exists(item["path"]):
            os.remove(item["path"])
        return jsonify(ok=True)

    @app.post("/api/send")
    def send():
        body = request.get_json(force=True) or {}
        ids = body.get("ids") or []
        convert = bool(body.get("convert_pdf"))
        cfg = current_config()
        errors = cfg.errors()
        if errors:
            return jsonify(error="；".join(errors)), 400

        results = []
        server = None
        try:
            for file_id in ids:
                with lock:
                    item = staged.get(file_id)
                if not item:
                    results.append({"id": file_id, "ok": False, "error": "文件不存在，请重新添加"})
                    continue
                # 发送前按当前配置再检查一次
                report = validate_file(item["path"], item["filename"], cfg.limits)
                if not report.ok:
                    results.append({"id": file_id, "ok": False, "error": "；".join(report.errors)})
                    continue
                try:
                    if server is None:
                        server = mailer.connect(cfg)
                    mailer.send_book(server, cfg, item["path"], item["filename"], convert)
                except (smtplib.SMTPException, OSError) as e:
                    results.append({"id": file_id, "ok": False, "error": f"发送失败：{e}"})
                    server = _close(server)
                    continue
                results.append({"id": file_id, "ok": True})
                with lock:
                    staged.pop(file_id, None)
                os.remove(item["path"])
        finally:
            _close(server)
        return jsonify(results=results)

    app.staging_dir = staging_dir
    atexit.register(shutil.rmtree, staging_dir, ignore_errors=True)
    return app


def _close(server):
    if server is not None:
        try:
            server.quit()
        except (smtplib.SMTPException, OSError):
            pass
    return None


def main() -> None:
    import argparse
    import webbrowser

    parser = argparse.ArgumentParser(description="把本地书籍发送到 Kindle")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    args = parser.parse_args()

    app = create_app()
    url = f"http://{args.host}:{args.port}/"
    print(f"Kindle Sender 已启动：{url}")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
