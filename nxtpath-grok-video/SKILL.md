---
name: nxtpath-grok-video
description: Generate a video via the Nxtpath gateway using the Grok video model, including image-to-video from reference images. Use when the user asks to generate a video, animate a still, or make a clip from a prompt or reference image; when a CLI's built-in video generation is unusable because it is hardwired to the official backend and ignores custom gateways (Codex/Claude/Grok CLIs); or explicitly invokes /nxtpath-grok-video. Saves the video as a local mp4 and prints its absolute path.
---

# Nxtpath Grok video

Call the Nxtpath gateway's Grok video API with a platform key (default model `xai/grok-imagine-video`) and save the result as a local mp4.

## Usage

Generate a video:

```bash
python scripts/nxtpath_grok_video.py "A red bicycle rolling down a quiet street" --resolution 480p -o out.mp4
```

Generate from a reference image:

```bash
python scripts/nxtpath_grok_video.py "the subject slowly turns toward the camera" --ref scene.png --resolution 480p -o out.mp4
```

The script path resolves relative to this skill's directory (`scripts/nxtpath_grok_video.py` sits next to SKILL.md). After success, tell the user the printed absolute video path; if the surface supports video, play or display the file.

## Parameters

| Parameter | Description |
| --- | --- |
| `prompt` (required) | What to generate, or how to animate the reference image(s) |
| `--ref IMAGE` | Repeatable, up to 14. Local file path or http(s)/data URL. Local files are sent as inline base64; URLs are passed through |
| `--resolution` | `480p` or `720p`; default `480p`. The gateway requires this field |
| `--duration` | Integer 1..15 seconds. Omit to use the upstream default |
| `--model` | Default `xai/grok-imagine-video`; override via `--model` or the `NXTPATH_GROK_VIDEO_MODEL` env var |
| `-o` / `--output` | Output file path; default `nxtpath-grok-video-<timestamp>.mp4` |
| `--timeout` | Default 900 seconds (covers submit + poll + download; video generation is slow; be patient) |

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

- Video generation is slow (minutes). The default timeout is 900 seconds; do not conclude the run is stuck too early.
- Resolution is only `480p` or `720p`. The gateway requires the field; the script defaults to `480p`.
- Duration is 1..15 seconds. Omitting `--duration` uses the upstream default.
- Cost is delivered seconds × the per-second rate of the resolution tier, plus a flat per-image charge for each reference image. Rates differ per model — see the pricing page.
- Up to 14 reference images is a limit, not a promise that multiple images compose. Measured behavior for 2+ images is not established.
- The mp4 URL is temporary. The script downloads it immediately to the output path.
- One in-flight job per account per resolution tier. A `429 TOO_MANY_TASKS_IN_FLIGHT` means wait for the running job to finish, then retry.
- On failure the script prints the gateway's error text; `401/403` means a key problem.
