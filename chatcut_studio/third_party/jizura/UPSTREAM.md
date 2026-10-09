# JIZURA（第三方，MIT）

- 上游：https://github.com/852wa/JIZURA
- 版本：v0.10.1，提交 fc16bfe43ea4a6c25a21f1caf04d17326de14f00（2026-10-01）
- 许可：MIT，见同目录 LICENSE；它打包的 mp4-muxer 我们没有带进来（视频编码由 ffmpeg 完成）。

这里只放了 `src/*.js`（渲染与编排引擎），**没有改动任何源码**。我们在 `chatcut_studio/jizura.py` 里：
把这些文件按文件名顺序拼成一个脚本（与上游 build.py 相同），装进无头浏览器里的渲染页，
用本地字体替代 Google Fonts，逐帧调用 `Renderer.frame()` 渲染透明画面，再交给 ffmpeg 叠到背景图上、合上音频。

升级：把新版的 `src/*.js` 覆盖进来，更新本文件里的版本号，跑一遍 `pytest chatcut_studio/tests/test_jizura.py`。
