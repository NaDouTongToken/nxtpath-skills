---
name: nxtpath-minimax-video
description: Generate a video via the Nxtpath gateway using the MiniMax H3 video model on the ark task line, including first/last-frame keyframes and reference images (local files or public URLs). Use when the user asks to generate a MiniMax video, when a CLI's built-in video generation is unusable because it is hardwired to the official backend and ignores custom gateways (Codex/Claude/Grok CLIs), or explicitly invokes /nxtpath-minimax-video. Saves the video as a local mp4 and prints its absolute path. Not for Seedance (use nxtpath-seedance-video) and not for Grok video (use nxtpath-grok-video).
---

# Nxtpath MiniMax video

Call the Nxtpath gateway's MiniMax H3 video API (ark task line) with a platform key (default model `minimax/minimax-h3`) and save the result as a local mp4.

## Usage

Generate a video:

```bash
python scripts/nxtpath_minimax_video.py "一只橘猫慢慢转头看向窗外，窗外下着雨" --resolution 720p --duration 4 --ratio 16:9 -o out.mp4
```

Generate with a first-frame keyframe (a local file is uploaded to Nxtpath temporary storage; the request carries the https URL):

```bash
python scripts/nxtpath_minimax_video.py "the subject slowly turns toward the camera" --first-frame frame.png --resolution 720p --duration 4 -o out.mp4
```

Generate with a public reference-image URL (no `role`; mutually exclusive with keyframes):

```bash
python scripts/nxtpath_minimax_video.py "the subject slowly turns toward the camera" --ref-image https://example.com/frame.jpg --resolution 720p --duration 4 -o out.mp4
```

Print the final request body without submitting (`--dry-run`):

```bash
python scripts/nxtpath_minimax_video.py "preview the payload" --first-frame frame.png --dry-run
```

The script path resolves relative to this skill's directory (`scripts/nxtpath_minimax_video.py` sits next to SKILL.md). After success, tell the user the printed absolute video path; if the surface supports video, play or display the file.

## Parameters

| Parameter | Description |
| --- | --- |
| `prompt` (required) | What to generate, or how to animate the keyframe / reference image(s) |
| `--first-frame PATH_OR_URL` | At most one. Local file path or public `http(s)` URL. Sent as `image_url` with `role: first_frame` (a keyframe; does not count as a reference image). A local file is uploaded to Nxtpath temporary storage and the signed https URL is sent; a public URL passes through. `data:` URLs are rejected |
| `--last-frame PATH_OR_URL` | At most one. Same rules as `--first-frame`, with `role: last_frame` |
| `--ref-image PATH_OR_URL` | Repeatable. Local file path or public `http(s)` URL. Sent as `image_url` with no `role` (a reference image). A local file is uploaded the same way; a public URL passes through. Mutually exclusive with `--first-frame` / `--last-frame` |
| `--resolution` | Required by the gateway. Default `720p`. Only `720p` / `960p` / `2k` (either case of P is accepted). Out of range is rejected locally before spend. This is also the billing tier |
| `--duration` | Required by the gateway. Default `4`. Integer seconds, range 4–12. Out of range is rejected locally before spend |
| `--ratio` | Required by the gateway. Default `16:9`. Closed set of six: `21:9` / `16:9` / `4:3` / `1:1` / `3:4` / `9:16`. Any other value is rejected locally before spend |
| `--model` | Default `minimax/minimax-h3`; override via `--model` or the `NXTPATH_MINIMAX_MODEL` env var |
| `-o` / `--output` | Output file path; default `nxtpath-minimax-<timestamp>.mp4` |
| `--timeout` | Default 900 seconds (covers submit + poll + download; video generation is slow; be patient) |
| `--dry-run` | Print the final request-body JSON and exit without uploading or submitting (no spend). A local image is shown as `<oss-upload:filename>` |

Keyframe mode (`--first-frame` and/or `--last-frame`) and reference-image mode (`--ref-image`) are mutually exclusive; the script rejects the mix locally. This skill does not accept `--ref-video` or `--ref-audio` (the MiniMax lane refuses reference audio/video, including when mixed with keyframes or reference images).

## Gateway & credentials

**The gateway is fixed to production `https://api.nxtpath.ai`** and never follows local environment config (`NXTPATH_BASE_URL` exists only as an explicit debug override).

Credential auto-resolution, first hit wins, no manual setup needed:

1. `NXTPATH_API_KEY` env var;
2. `ANTHROPIC_AUTH_TOKEN` env var, only when `ANTHROPIC_BASE_URL` points at the Nxtpath gateway (the domain check only proves the token is Nxtpath's; it never selects the gateway);
3. the env block of `~/.claude/settings.json` (present after the Nxtpath desktop app one-click-deploys Claude Code);
4. the key of the provider section in `~/.codex/config.toml` whose `base_url` points at Nxtpath (present after one-click-deploying Codex; covers Codex-only users);
5. the `api_key` of the model section in `~/.grok/config.toml` whose `base_url` points at Nxtpath (present after one-click-deploying Grok; covers Grok-only users).

If none of these yield a key, the script errors with setup guidance. **The API key is never printed or logged**; never pass it on the command line or commit it.

## Notes

- 本地 `--first-frame` / `--last-frame` / `--ref-image` 文件会上传到 Nxtpath 临时存储（私有桶，需有效的 Nxtpath key），请求里只放返回的 https 链接。链接约 24 小时有效，对象在创建约 1 天后由生命周期规则删除。不内联 `data:`。公网 `http(s)` URL 原样透传。显式传入的 `data:` URL 会被拒绝。文件不超过 20 MiB 时不缩图；超过 20 MiB 才在临时副本上按多级回退缩图（Pillow → System.Drawing → sips → ImageMagick），不修改用户原文件，缩不到 20 MiB 以内则报错。调试时可设 `NXTPATH_UPLOAD_SIGNER_URL` 覆盖签名服务地址。签名服务返回 401 时报 `key invalid`。
- `--dry-run` 不上传，本地图在 JSON 里显示为 `<oss-upload:filename>`。
- Keyframe `image_url` parts use `role: first_frame` or `role: last_frame` (at most one of each). An `image_url` with no `role` is a reference image. The two modes cannot be mixed, and neither can include `video_url` / `audio_url`.
- This is the ark task line (`POST /v1/tasks/submit` + `GET /v1/tasks/status?task_id=…` + `GET /v1/tasks/artifacts/{task_id}/0`, capitalized statuses `Pending` / `Running` / `Success` / `Failure` / `Expired`). It is not the Grok video line (`/v1/videos/generations`); paths and status words differ.
- Billing is seconds × the resolution tier, plus a surcharge for reference images beyond the allowance (the first five reference images are free; keyframes do not count as reference images). See the pricing page — this skill does not quote rates. No delivery means no charge.
- `output.urls[]` are this gateway's authenticated handles. Download with the same bearer token. Valid about 23 hours; the script downloads immediately. Expired handles cannot be refreshed — regenerate.
- Each submit sends `Idempotency-Key: <uuid4>` (16–128 characters). Reusing a key with a different body is a 409.
- There is no official SDK upstream. This script is the client (stdlib urllib submit + poll + bearer download).
- Video generation is slow (minutes). The default timeout is 900 seconds; do not conclude the run is stuck too early.
- One task per run. `--dry-run` does not submit.
- On failure the script prints the gateway's error text; `401/403` means a key problem. `404 MODEL_NOT_AVAILABLE` prints a catalog hint (`GET /v1/models`; override with `--model` or `NXTPATH_MINIMAX_MODEL`).
