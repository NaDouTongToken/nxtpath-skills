---
name: nxtpath-grok-image
description: Generate an image via the Nxtpath gateway using the Grok image model (grok-imagine-image). Use when the user asks to generate a Grok image, draw something with Grok Imagine, or explicitly invokes /nxtpath-grok-image. Saves the image as a local file and prints its absolute path. Not for GPT image (use nxtpath-gpt-image) and not for Grok video (use nxtpath-grok-video).
---

# Nxtpath Grok 生图

通过 Nxtpath 网关调用 Grok 图片生成接口（默认模型 `xai/grok-imagine-image`），把结果存成本地图片文件。走官方图片线 `POST /v1/images/generations`。

## Usage

生成一张图：

```bash
python scripts/nxtpath_grok_image.py "A lighthouse on a cliff at dusk, oil painting style" -o out.png
```

脚本路径相对本 skill 目录（`scripts/nxtpath_grok_image.py` 与 SKILL.md 同级）。成功后把打印的绝对路径告诉用户；若界面支持图片，直接展示该文件。

## Parameters

| Parameter | Description |
| --- | --- |
| `prompt`（必填） | 要画的内容 |
| `-o` / `--output` | 输出路径；默认按时间戳自动命名，扩展名按实际格式检测 |
| `--model` | 默认 `xai/grok-imagine-image`；可用 `--model` 或环境变量 `NXTPATH_GROK_IMAGE_MODEL` 覆盖 |
| `--timeout` | 默认 600 秒（生图较慢，请耐心等待，不要过早判定卡住） |

v1 不提供 `--edit`、也不提供 `--n`（该线路尚未核实）。

## Gateway & credentials

**网关固定为生产 `https://api.nxtpath.ai`**，不跟随本地环境配置（`NXTPATH_BASE_URL` 仅作显式调试覆盖）。

密钥自动解析，先命中先用，无需手工配置：

1. `NXTPATH_API_KEY` 环境变量；
2. `ANTHROPIC_AUTH_TOKEN` 环境变量，且仅当 `ANTHROPIC_BASE_URL` 指向 Nxtpath 网关（域名检查只证明 token 属于 Nxtpath，不用于选择网关）；
3. `~/.claude/settings.json` 的 env 块（哪都通桌面端一键部署 Claude Code 后存在）；
4. `~/.codex/config.toml` 中 `base_url` 指向 Nxtpath 的 provider 段的密钥（一键部署 Codex 后存在；覆盖仅用 Codex 的用户）；
5. `~/.grok/config.toml` 中 `base_url` 指向 Nxtpath 的 model 段的 `api_key`（一键部署 Grok 后存在；覆盖仅用 Grok 的用户）。

都拿不到密钥时脚本会报错并给出配置指引。**API 密钥绝不打印或写入日志**；不要在命令行传入或提交到仓库。

## Notes

- 生图可能要数十秒到数分钟，属正常。失败时打印网关错误原文；`401/403` 表示密钥问题，`404` 通常表示该密钥没有该图像模型权限。
- 请求固定带 `response_format=b64_json`。上游默认返回 `imgen.x.ai` 临时 URL，部分网络会连接失败；内联字节才能保证本地落盘。
- 每次运行一张图。
