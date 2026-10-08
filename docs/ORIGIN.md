# 来源、依赖与发布边界

本项目从独立的原创需求与界面概念实现“镜序”，没有复制旧短剧工作台的源码、人物、剧本、提示词或素材。提交历史保存了需求、设计、代码与测试的演进；这些记录描述开发来源，不构成对第三方资产权利的法律保证。

## 明确纳入源码的三张虚构图片

以下图片来自本项目授权的 ImageGen 原创概念／虚构雨巷静帧，只用于设计参考和明确创建的演示作品。发布校验器按路径和 SHA-256 同时限制它们；任何替换都须重新核对来源与发布决定。

| 路径 | 用途 | SHA-256 |
| --- | --- | --- |
| `docs/design/workbench-concept-v1.png` | 主工作台概念 | `4045099c9f02dd1afc6564b048dce404e56eeb4aad846752cc1fada40970e131` |
| `docs/design/queue-concept-v1.png` | 队列界面概念 | `4c8390de7affc59efdf663beb8c05f251cdc24ee026fa832f919ee07ee885dc1` |
| `web/assets/rain-alley-demo.png` | 虚构“雨夜来客”静帧 | `e0a85449e2fe68a94da4a693b2fca8c3d6dd120ba671b65f2c069cfc01baf4a2` |

这些图片不代表真实演员、实际视频、人工验收或完整作品版权结论。用户实际导入的图片、视频、声音、剧本与生成结果始终属于运行数据，不进入源码发布。

## 技术依赖和接口依据

- Python 3.12+ 标准库：本地 HTTP、SQLite、ZIP、队列与服务端功能；Python 许可随安装来源而定。
- 浏览器原生 HTML／CSS／JavaScript 模块：没有打包第三方前端组件或字体文件。界面按系统字体回退；具体字体许可由运行系统决定。
- 系统提供的 FFmpeg／ffprobe：媒体探测、重新编码、字幕与解码验证。FFmpeg 的实际许可取决于用户安装的构建及启用组件，本仓库不捆绑二进制。接口依据为 [FFmpeg concat](https://ffmpeg.org/ffmpeg-formats.html#concat) 与 [subtitles filter](https://ffmpeg.org/ffmpeg-filters.html#subtitles-1)；本机能力以实际命令检查为准。
- 可选 ComfyUI 视频适配器：仅按操作者配置使用公开的 [服务端接口](https://docs.comfy.org/development/comfyui-server/comms_routes)。工作流、模型及素材需另行取得并核对许可；没有随仓库附送，也没有在发布验证中运行真实 H3。
- 可选离线 ASR `faster-whisper`：只有显式提供已有模型目录和依赖后才能运行。模型权重、运行配置和下载过程均不属于发布物；技术测试不等于真实语音识别验收。
- 可选文本服务：由操作者自行配置兼容接口、模型与凭据；不把当前 Codex 会话或订阅视为 API 授权。

当前没有为整个源码仓库附加面向公众的开源许可。源码公开展示不改变上述各项独立权利，也不授权第三方模型或素材的使用。实际制作或商用前，应逐项核对所选 FFmpeg 构建、模型、字体、素材与服务条款。
