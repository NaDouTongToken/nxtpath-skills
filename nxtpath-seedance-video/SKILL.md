---
name: nxtpath-seedance-video
description: Generate a video via the Nxtpath gateway using the Seedance video model on the ark task line, including image-to-video and video-to-video from public reference URLs. Use when the user asks to generate a Seedance video, animate a still from a public image URL, or make a clip from a prompt or reference URL; when a CLI's built-in video generation is unusable because it is hardwired to the official backend and ignores custom gateways (Codex/Claude/Grok CLIs); or explicitly invokes /nxtpath-seedance-video. Saves the video as a local mp4 and prints its absolute path.
---

# Nxtpath Seedance video

Call the Nxtpath gateway's Seedance video API (ark task line) with a platform key (default model `seedance/seedance-2.0`) and save the result as a local mp4.

## Usage

Generate a video:

```bash
python scripts/nxtpath_seedance_video.py "A red bicycle rolling down a quiet street" --resolution 480p --duration 4 -o out.mp4
```

Generate with a reference image URL:

```bash
python scripts/nxtpath_seedance_video.py "the subject slowly turns toward the camera" --ref-image https://example.com/frame.jpg --resolution 480p --duration 4 -o out.mp4
```

The script path resolves relative to this skill's directory (`scripts/nxtpath_seedance_video.py` sits next to SKILL.md). After success, tell the user the printed absolute video path; if the surface supports video, play or display the file.

## Parameters

| Parameter | Description |
| --- | --- |
| `prompt` (required) | What to generate, or how to animate the reference URL(s) |
| `--ref-image URL` | Repeatable. Public `http(s)` URL only. Local file paths are rejected — a public URL is required. Same price as text-to-video |
| `--ref-video URL` | Repeatable. Public `http(s)` URL only. Local file paths are rejected — a public URL is required. Different billing tier than text/image |
| `--resolution` | Default `480p`. seedance-2.0: `480p` / `720p` only (no `1080p`). seedance-2.5: `480p` / `720p` / `1080p`. Out of range is rejected locally before spend |
| `--duration` | Integer seconds. Omit for the gateway default. seedance-2.0: 4–15 (no 1–3s). seedance-2.5: 4–30, or `-1` (smart duration). Out of range is rejected locally before spend |
| `--ratio` | Optional string, e.g. `16:9`. Passed through; omit for the upstream default |
| `--model` | Default `seedance/seedance-2.0`; override via `--model` or the `NXTPATH_SEEDANCE_MODEL` env var. The other listed model is `seedance/seedance-2.5` |
| `-o` / `--output` | Output file path; default `nxtpath-seedance-<timestamp>.mp4` |
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

- This is the ark task line (`POST /v1/tasks/submit` + `GET /v1/tasks/status?task_id=…`, capitalized statuses `Pending` / `Running` / `Success` / `Failure` / `Expired`). It is not the Grok video line (`/v1/videos/generations`); paths and status words differ.
- Billing is the token lane (`usage.total_tokens` × per-Mtok rate), tiered by whether the request includes video input. A reference image is the same price as text-to-video; a reference video is a different tier. See the pricing page — this skill does not quote rates.
- `output.urls[]` are this gateway's authenticated handles. Download with the same bearer token. Valid 24 hours; the script downloads immediately. Expired handles cannot be refreshed — regenerate.
- There is no official SDK upstream. This script is the client (stdlib urllib submit + poll + bearer download).
- Video generation is slow (minutes). The default timeout is 900 seconds; do not conclude the run is stuck too early.
- One task per run.
- On failure the script prints the gateway's error text; `401/403` means a key problem.
