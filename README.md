# Kindle Sender

一个带网页界面的本地小工具：把电脑上的书籍通过邮件发送到 Kindle，发送前自动检查格式和文件大小。

## 功能

- 拖拽或选择多个文件，逐个显示检查结果，只有检查通过的文件才会发送
- **格式检查**
  - 扩展名必须是 Amazon 邮件推送支持的格式：EPUB、PDF、DOC、DOCX、TXT、RTF、HTM/HTML、JPG/JPEG、PNG、GIF、BMP
  - 读取文件内容（魔数）识别真实格式，发现扩展名与内容不符（例如把 MOBI 改名为 .epub）会拦截
  - MOBI / AZW3 / FB2 / 压缩包等不支持的格式给出转换建议
  - EPUB：检查 `container.xml`、`.opf`、ZIP 完整性、`mimetype` 规范和 DRM 加密
  - PDF：检查是否完整、是否加密
  - TXT：识别 GBK / Big5 等非 UTF-8 编码并提醒（Kindle 上容易乱码）
- **大小检查**
  - 单个文件不超过 Amazon 的 50 MB 邮件上限（可设置更小的值）
  - 按 Base64 编码后（约增大 33%）的邮件大小，与发件邮箱的上限比较（例如 Gmail 为 25 MB）
- 内置 QQ、163、126、Gmail、iCloud 邮箱的 SMTP 预设，可测试连接
- PDF 可选「convert」转换为可调字号的 Kindle 格式
- 每本书单独发一封邮件，避免多本书合计超限

## 使用

```bash
pip install -r requirements.txt
python -m kindle_sender            # 启动后自动打开 http://127.0.0.1:5000/
python -m kindle_sender --port 8080 --no-browser
```

首次使用：

1. 在页面「邮箱设置」中填写 Kindle 收件邮箱（`xxx@kindle.com`，在 Amazon「管理内容和设备 → 偏好设置 → 个人文档设置」中查看）、发件邮箱和 SMTP 信息。QQ / 163 邮箱需要在邮箱设置中开启 SMTP 并使用**授权码**，而不是登录密码。
2. 在 Amazon 同一页面把发件邮箱加入「已认可的发件人电子邮箱列表」，否则 Kindle 收不到文件。
3. 拖入书籍，确认检查通过后点击「发送到 Kindle」。

配置保存在 `~/.kindle_sender/config.json`（权限 600），可用环境变量 `KINDLE_SENDER_CONFIG` 指定其他路径；也可以不保存密码，改用环境变量 `KINDLE_SMTP_PASSWORD` 提供。服务默认只监听 `127.0.0.1`。

## 开发

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

代码结构：

- `kindle_sender/validator.py` — 格式与大小检查
- `kindle_sender/mailer.py` — 构造邮件并通过 SMTP 发送
- `kindle_sender/config.py` — 本地配置读写
- `kindle_sender/app.py` — Flask 接口（`/api/config`、`/api/files`、`/api/send`）
- `kindle_sender/static/` — 前端页面
