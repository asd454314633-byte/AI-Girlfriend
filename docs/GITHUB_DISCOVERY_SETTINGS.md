# GitHub 仓库发现与展示设置（待仓库所有者应用）

> 本文件提供**可直接复制**的 GitHub 后台信息。仓库 About、Topics、Social Preview 和个人主页 Pin 属于仓库/账号界面设置，不等于提交此文件后自动生效。

## 仓库名称

保持现有仓库：`AI-Girlfriend`；不要重新建仓库或丢弃 Git 历史。

## About / Description（英文，适合 GitHub 搜索）

```text
Open-source local AI companion: real-time voice, WebRTC digital avatar, intelligent reading, and pluggable LLM/TTS. Development preview; see test scope.
```

## Topics（逐项添加）

```text
ai-girlfriend
ai-companion
local-llm
digital-human
webrtc
text-to-speech
speech-to-speech
qwen3
```

以上标签准确描述本仓库的项目方向；它们只是用于搜索发现，不能当作全新安装或生产稳定性保证。

## Homepage

可暂时留空。**不要**把本机 `localhost:7860`、`127.0.0.1:8010` 当作公网演示网站填写。

## Social Preview（分享封面）

本仓库提供的 [品牌 SVG](assets/ai-girlfriend-brand-banner.svg) 是**抽象品牌插画，不是软件实际运行截图**。可从同一设计导出 1280×640 PNG，作为仓库 Settings → General → Social preview 图片。真正的实战录屏完成后再决定是否补充真实截图。

## GitHub 个人主页

打开自己的 GitHub Profile → Customize your pins，将 `AI-Girlfriend` 固定到主页。个人简介可以写：

```text
Building local AI companions, real-time voice systems, WebRTC digital humans & intelligent reading. Open-source: AI-Girlfriend.
```

## Release 发布时机

在公开仓库源码、许可证说明、隐私清理和基本验证事实已明确的情况下，可以将其作为 **开发预览版**发布；建议候选标签 `v0.1.0-preview`。目前尚未完成全新环境安装，禁止写“一键运行”或“正式稳定版”。本次 V1 不自动创建 Release。

## 内容推广原则

优先展示真实界面和声音，清楚标注实际录制/概念示意；未经公开授权的原始人物、声音、聊天和私有测试证据不放入公开仓库。不刷量、不制造虚假 Star。
