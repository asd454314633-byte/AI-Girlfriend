# AI Girlfriend — 源码整理版

本仓库是经过隐私筛选的源码整理与私有备份版本，尚未公开发布，也不代表完整功能已验收。

**数字人前端同步适配层 `runtime/ui/app/avatar-sync.js` 暂未包含，数字人功能不能直接完整运行。** 原实现的再分发许可尚未明确解决；另行开发的替代候选及其失败证据保留在本地隔离区，不在本仓库。本次上传不处理这个功能缺口。

## 包含内容

- `runtime/ui/app/`：语音与聊天界面、阅读界面、管理接口及已确认来源的前端依赖。
- `runtime/core/`、`runtime/s2s_business/`：服务胶水、提供商适配与依赖清单。
- `runtime/character/`、`runtime/mode/`、`runtime/reading/`、`runtime/reading_v2/`：框架源码与通用规则；不含人物内容、读物或用户数据。
- `runtime/avatar/app/`：既有 LiveTalking 源码与已登记修改；原许可证保留。
- `runtime/windows/`、`scripts/`：既有生命周期脚本。
- `third_party_modified/`：已登记的 speech-to-speech 补丁。

## 明确排除

本地读物、小说、文章、导入正文、聊天与记忆数据库、阅读进度、向量/检索索引、人物注册与当前选择、私人配置、参考声音、数字人素材、音视频、模型权重、测试夹具、测试日志、历史备份及验收证据均不包含。旧暂存仓库和旧 Git 历史没有复制。

源码中用于实现阅读或聊天的函数、UI 标签和通用提示规则属于程序代码，不是用户的读物或对话记录。历史测试代码和夹具全部省略，以防内嵌正文进入备份。

## 使用与限制

见 [安装说明](docs/INSTALL.md)。依赖、模型、参考声音及数字人素材需按各自许可自行准备；此版本不是一键运行发行包。配置中的本机路径与声音选择已清空或替换为相对示例，使用前须自行配置。没有针对本次整理树运行 GPU、麦克风、语音或数字人验收。

## 许可与来源

自研部分适用 [LICENSE](LICENSE) 的 MIT 许可；第三方部分保留各自许可，详见 [NOTICE](NOTICE.md) 和 [来源登记](provenance/COMPONENT_SOURCES.csv)。LiveTalking 与 speech-to-speech 派生部分适用 Apache-2.0；OpenAI 前端包适用其 MIT 许可。模型与媒体不随仓库分发。

`provenance/SHA256_MANIFEST.txt` 登记最终文件哈希，`provenance/FILE_ORIGINS.json` 登记从原暂存树复制文件的哈希及整理后哈希。本次提交使用全新 Git 历史；旧库隐私审计的失败结果不能被这次整理抹去。

私有上传完成不等于免费开源发布完成。公开发布仍需另行处理功能缺口及再分发权限。
