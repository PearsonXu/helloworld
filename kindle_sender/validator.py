"""发送前的书籍检查：格式（扩展名 + 文件内容）与文件大小。"""

from __future__ import annotations

import codecs
import math
import os
import zipfile
from dataclasses import dataclass, field

MB = 1024 * 1024

# Amazon「通过电子邮件发送到 Kindle」支持的格式（扩展名 -> 内容类型）。
SUPPORTED_FORMATS = {
    "epub": "epub",
    "pdf": "pdf",
    "doc": "doc",
    "docx": "docx",
    "txt": "txt",
    "rtf": "rtf",
    "htm": "html",
    "html": "html",
    "png": "png",
    "gif": "gif",
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "bmp": "bmp",
}

# 常见但无法通过邮件发送的格式，给出处理建议。
UNSUPPORTED_HINTS = {
    "mobi": "Amazon 已停止通过邮件接收 MOBI，请用 Calibre 转换为 EPUB 后再发送",
    "azw": "AZW 不再支持通过邮件发送，请转换为 EPUB",
    "azw3": "AZW3 不支持通过邮件发送，请用 Calibre 转换为 EPUB",
    "prc": "PRC 不再支持通过邮件发送，请转换为 EPUB",
    "kfx": "KFX 不支持通过邮件发送，请转换为 EPUB",
    "fb2": "FB2 不受支持，请用 Calibre 转换为 EPUB",
    "djvu": "DJVU 不受支持，请转换为 PDF",
    "cbz": "CBZ 漫画包不受支持，请转换为 PDF 或 EPUB",
    "cbr": "CBR 漫画包不受支持，请转换为 PDF 或 EPUB",
    "zip": "压缩包不受支持，请解压后发送其中的书籍文件",
    "rar": "压缩包不受支持，请解压后发送其中的书籍文件",
    "7z": "压缩包不受支持，请解压后发送其中的书籍文件",
}

# Amazon 对单封邮件的大小上限。
AMAZON_EMAIL_LIMIT_MB = 50

TEXT_ENCODINGS = ("utf-8", "gb18030", "big5")
HEAD_SIZE = 64 * 1024


@dataclass
class Limits:
    # 单个文件的大小上限（不能超过 Amazon 的 50MB）
    max_file_mb: float = AMAZON_EMAIL_LIMIT_MB
    # 发件邮箱服务商允许的整封邮件大小（附件 Base64 编码后约增大 1/3）
    smtp_limit_mb: float = 50


@dataclass
class Report:
    filename: str
    size: int
    ext: str = ""
    detected: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "size": self.size,
            "ext": self.ext,
            "detected": self.detected,
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def encoded_size(size: int) -> int:
    """附件经 Base64 编码（每 76 字符换行）后的大致字节数，外加邮件头的余量。"""
    b64 = 4 * math.ceil(size / 3)
    return b64 + b64 // 76 * 2 + 4096


def format_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _detect_zip(path: str, report: Report) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if "mimetype" in names:
                mimetype = zf.read("mimetype").strip()
                if mimetype == b"application/epub+zip":
                    _check_epub(zf, names, report)
                    return "epub"
            if "META-INF/container.xml" in names:
                _check_epub(zf, names, report)
                return "epub"
            if "word/document.xml" in names:
                return "docx"
            if any(n.lower().endswith((".jpg", ".jpeg", ".png")) for n in names):
                return "cbz"
            return "zip"
    except zipfile.BadZipFile:
        report.errors.append("文件已损坏：ZIP 结构无法读取")
        return "zip"


def _check_epub(zf: zipfile.ZipFile, names: list[str], report: Report) -> None:
    if "META-INF/container.xml" not in names:
        report.errors.append("EPUB 缺少 META-INF/container.xml，Kindle 会拒收")
    if not any(n.lower().endswith(".opf") for n in names):
        report.errors.append("EPUB 缺少 .opf 内容清单文件")
    first = zf.infolist()[0]
    if first.filename != "mimetype" or first.compress_type != zipfile.ZIP_STORED:
        report.warnings.append("EPUB 的 mimetype 不是第一个未压缩条目，不符合规范，可能被 Amazon 拒收；可用 Calibre 重新导出")
    if "META-INF/encryption.xml" in names:
        report.warnings.append("EPUB 含加密信息（可能有 DRM），Amazon 可能无法转换")
    bad = zf.testzip()
    if bad:
        report.errors.append(f"EPUB 内部文件损坏：{bad}")


def _detect_text(head: bytes, truncated: bool, report: Report) -> str | None:
    if b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    text = None
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = head.decode("utf-16", errors="ignore")
        report.warnings.append("文本为 UTF-16 编码，建议转换为 UTF-8 以免出现乱码")
    else:
        for enc in TEXT_ENCODINGS:
            try:
                # 只读了文件开头时，末尾可能截断在多字节字符中间，用增量解码容忍这种情况
                text = codecs.getincrementaldecoder(enc)().decode(head, final=not truncated)
            except UnicodeDecodeError:
                continue
            if enc != "utf-8":
                report.warnings.append(
                    f"文本疑似 {enc.upper()} 编码，Kindle 可能显示乱码，建议另存为 UTF-8"
                )
            break
    if text is None:
        return None
    lowered = text.lstrip("﻿ \r\n\t").lower()
    if lowered.startswith(("<!doctype html", "<html")) or "<html" in lowered[:2048]:
        return "html"
    return "txt"


def detect_type(path: str, report: Report) -> str | None:
    """根据文件内容（魔数）判断真实格式。"""
    with open(path, "rb") as f:
        head = f.read(HEAD_SIZE)
        truncated = bool(f.read(1))

    if head.startswith(b"PK\x03\x04"):
        return _detect_zip(path, report)
    if b"%PDF" in head[:1024]:
        return "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "doc"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"BM") and len(head) > 26:
        return "bmp"
    if head[60:68] == b"BOOKMOBI":
        return "mobi"
    if head[60:68] == b"TEXtREAd":
        return "prc"
    if head.startswith(b"Rar!"):
        return "rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith((b"AT&TFORM", b"FORM")):
        return "djvu"
    return _detect_text(head, truncated, report)


def _check_pdf(path: str, report: Report) -> None:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 4096))
        tail = f.read()
        f.seek(0)
        head = f.read(64 * 1024)
    if b"%%EOF" not in tail:
        report.warnings.append("PDF 末尾缺少 %%EOF 标记，文件可能不完整")
    if b"/Encrypt" in head or b"/Encrypt" in tail:
        report.warnings.append("PDF 已加密，Kindle 可能无法打开")


def validate_file(path: str, filename: str, limits: Limits | None = None) -> Report:
    limits = limits or Limits()
    size = os.path.getsize(path)
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    report = Report(filename=filename, size=size, ext=ext)

    # 1. 扩展名
    if not ext:
        report.errors.append("文件没有扩展名，无法确定格式")
    elif ext in UNSUPPORTED_HINTS:
        report.errors.append(f".{ext} 格式不受支持：{UNSUPPORTED_HINTS[ext]}")
    elif ext not in SUPPORTED_FORMATS:
        report.errors.append(f".{ext} 格式不受 Kindle 支持")

    # 2. 大小
    max_file = min(limits.max_file_mb, AMAZON_EMAIL_LIMIT_MB) * MB
    if size == 0:
        report.errors.append("文件为空（0 字节）")
        return report
    if size > max_file:
        report.errors.append(
            f"文件大小 {format_size(size)} 超过上限 {format_size(max_file)}"
        )
    elif encoded_size(size) > limits.smtp_limit_mb * MB:
        report.errors.append(
            f"编码后邮件约 {format_size(encoded_size(size))}，超过发件邮箱上限 "
            f"{format_size(limits.smtp_limit_mb * MB)}（附件编码后约增大 33%）"
        )
    elif size > max_file * 0.9:
        report.warnings.append("文件接近大小上限，发送可能较慢")

    # 3. 内容与扩展名是否一致
    detected = detect_type(path, report)
    report.detected = detected
    expected = SUPPORTED_FORMATS.get(ext)
    if expected and report.ok:
        if detected is None:
            report.errors.append("无法识别文件内容，文件可能已损坏或不是有效的书籍文件")
        elif detected != expected and not (expected == "txt" and detected == "html"):
            if detected in UNSUPPORTED_HINTS:
                report.errors.append(
                    f"扩展名为 .{ext}，但实际内容是 {detected.upper()}：{UNSUPPORTED_HINTS[detected]}"
                )
            else:
                report.errors.append(
                    f"扩展名为 .{ext}，但实际内容是 {detected.upper()}，请改为正确的扩展名"
                )
        elif detected == "pdf":
            _check_pdf(path, report)

    return report
