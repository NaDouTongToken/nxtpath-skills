---
name: nxtpath-seedance-video
description: Generate a video via the Nxtpath gateway using the Seedance video model on the ark task line, including image-to-video and video-to-video from local files or public reference URLs. Use when the user asks to generate a Seedance video, animate a still, or make a clip from a prompt or reference; when a CLI's built-in video generation is unusable because it is hardwired to the official backend and ignores custom gateways (Codex/Claude/Grok CLIs); or explicitly invokes /nxtpath-seedance-video. Saves the video as a local mp4 and prints its absolute path. Not for MiniMax (use nxtpath-minimax-video) and not for Grok video (use nxtpath-grok-video).
---

# Nxtpath Seedance video

Call the Nxtpath gateway's Seedance video API (ark task line) with a platform key (default model `doubao/seedance-2.0`) and save the result as a local mp4.

## Usage

Generate a video:

```bash
python scripts/nxtpath_seedance_video.py "A red bicycle rolling down a quiet street" --resolution 480p --duration 4 -o out.mp4
```

Generate with a local reference image and a local reference video (the image is inlined; the video is uploaded to Nxtpath temporary storage and the request carries the https URL):

```bash
python scripts/nxtpath_seedance_video.py "the subject slowly turns toward the camera" --ref-image frame.jpg --ref-video clip.mp4 --resolution 480p --duration 4 -o out.mp4
```

Print the final request body without submitting (`--dry-run`):

```bash
python scripts/nxtpath_seedance_video.py "preview the payload" --ref-image frame.jpg --ref-video clip.mp4 --dry-run
```

The script path resolves relative to this skill's directory (`scripts/nxtpath_seedance_video.py` sits next to SKILL.md). After success, tell the user the printed absolute video path; if the surface supports video, play or display the file.

### Tool timeout — read before running

Video generation takes several minutes and the default `--timeout` is 900 seconds, longer than most agent shells allow for one command (Claude Code's Bash tool stops a command after **2 minutes** by default and at most 10 minutes). **Run the script in the background and wait for it to finish** (Claude Code: `run_in_background: true`, then wait for the completion notice), or pass a tool timeout of at least `--timeout` where the shell allows it. A killed run is not free: the task has already been submitted, so it may still run to completion and be billed, but the video is never downloaded.

## Parameters

| Parameter | Description |
| --- | --- |
| `prompt` (required) | What to generate, or how to animate the reference image / video |
| `--ref-image PATH_OR_URL` | Repeatable. Local file path, public `http(s)` URL, or an explicit `data:` image URL. A local file is sniffed (jpeg/png/webp/gif) and inlined as `data:<mime>;base64`. A public URL passes through. Same price as text-to-video |
| `--ref-video PATH_OR_URL` | Repeatable. Local `.mp4` / `.mov` file, or a public `http(s)` URL. A local file is uploaded to Nxtpath temporary storage and the signed https URL is sent as `video_url`. A public URL passes through. Inline `data:` video is rejected. Different billing tier than text/image |
| `--resolution` | Default `480p`. seedance-2.0 (including mini): `480p` / `720p` only (no `1080p`). seedance-2.5: `480p` / `720p` / `1080p`. Out of range is rejected locally before spend |
| `--duration` | Integer seconds. Omit for the gateway default. seedance-2.0 (including mini): 4–15 (no 1–3s). seedance-2.5: 4–30, or `-1` (smart duration). Out of range is rejected locally before spend |
| `--ratio` | Optional string, e.g. `16:9`. Passed through; omit for the upstream default |
| `--seed` | Optional int. seedance-2.5 only; mapped to `parameters.seed`. Rejected locally on non-2.5 models |
| `--generate-audio` | Optional bool (`true`/`false`). seedance-2.5 only; mapped to `parameters.generate_audio`. Rejected locally on non-2.5 models |
| `--return-last-frame` | Optional bool (`true`/`false`). seedance-2.5 only; mapped to `parameters.return_last_frame`. Rejected locally on non-2.5 models |
| `--model` | Default `doubao/seedance-2.0`; override via `--model` or the `NXTPATH_SEEDANCE_MODEL` env var. Also listed: `doubao/seedance-2.5`, `doubao/seedance-2.0-mini`. On `404 MODEL_NOT_AVAILABLE` the script swaps the `doubao/` ↔ `seedance/` prefix once and retries |
| `-o` / `--output` | Output file path; default `nxtpath-seedance-<timestamp>.mp4` |
| `--timeout` | Default 900 seconds (covers submit + poll + download; video generation is slow; be patient) |
| `--dry-run` | Print the final request-body JSON and exit without uploading or submitting (no spend). A local video is shown as `<oss-upload:filename>`; a local image as `<inline-image:filename,N bytes>` |

`doubao/seedance-2.0-mini` exists and is bucketed with the 2.0 duration/resolution family (the `"2.5"` substring check does not match). Exact mini bounds are whatever the gateway enforces — this skill does not invent numbers.

V2 parameters (`--seed`, `--generate-audio`, `--return-last-frame`) are seedance-2.5 only (router #2203). `omni_reference_task_type` is not exposed yet (reference-task semantics unverified).

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

- A local `--ref-image` is inlined as a `data:` URL after the file is sniffed (jpeg, png, webp, or gif). The gateway submit body is capped at 10 MiB, so if the encoded images would exceed about 9 MiB the script downscales a temporary copy (Pillow → System.Drawing → sips → ImageMagick) and does not modify the original. If they still do not fit, it exits and asks for a public https URL. An explicit `data:` image URL is accepted. A public `http(s)` URL passes through unchanged.
- A local `--ref-video` (`.mp4` or `.mov`, with an `ftyp` box at offset 4, at most 50 MiB) is uploaded to Nxtpath temporary storage. The upload needs a valid Nxtpath key. The request then carries the signed https URL, which is valid for 24 hours; the object is deleted about 1 day after it was created. Inline `data:` video is rejected: the gateway accepts `data:video` but the task never leaves Pending. A public `http(s)` URL passes through unchanged. Set `NXTPATH_UPLOAD_SIGNER_URL` to override the signer address. A signer HTTP 401 is reported as `key invalid`.
- `--dry-run` does not upload or submit. A local video shows as `<oss-upload:filename>`; a local image shows as `<inline-image:filename,N bytes>`.
- Status polling retries transient errors (socket timeout, URL error, HTTP 5xx, HTTP 429) with backoff until `--timeout`, and prints a one-line warning each time. Other HTTP 4xx stops immediately. The final timeout prints `task_id` so the task can be checked later. The artifact download retries a transient failure a few times the same way.
- This is the ark task line (`POST /v1/tasks/submit` + `GET /v1/tasks/status?task_id=…`, capitalized statuses `Pending` / `Running` / `Success` / `Failure` / `Expired`). It is not the Grok video line (`/v1/videos/generations`); paths and status words differ.
- Billing is the token lane (`usage.total_tokens` × per-Mtok rate), tiered by whether the request includes video input. A reference image is the same price as text-to-video; a reference video is a different tier. See the pricing page — this skill does not quote rates.
- `output.urls[]` are this gateway's authenticated handles. Download with the same bearer token. Valid 24 hours; the script downloads immediately. Expired handles cannot be refreshed — regenerate.
- There is no official SDK upstream. This script is the client (stdlib urllib submit + poll + bearer download).
- Video generation is slow (minutes). The default timeout is 900 seconds; do not conclude the run is stuck too early.
- One task per run. `--dry-run` does not submit.
- The public namespace is `doubao/seedance-*`. Old `seedance/seedance-*` names are retried once as `doubao/` (and vice versa) on `404 MODEL_NOT_AVAILABLE`.
- On failure the script prints the gateway's error text; `401/403` means a key problem.
