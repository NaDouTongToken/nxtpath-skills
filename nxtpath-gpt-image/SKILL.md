---
name: nxtpath-gpt-image
description: Generate or edit an image via the Nxtpath gateway using the GPT image model (gpt-image-2). Use when the user asks to generate an image, draw something, or edit an existing picture; when a CLI's built-in image generation is unusable because it is hardwired to the official backend and ignores custom gateways (Codex/Claude/Grok CLIs); or explicitly invokes /nxtpath-gpt-image. Saves the image as a local file and prints its absolute path.
---

# Nxtpath GPT image

Call the Nxtpath gateway's GPT image API with a platform key (default model `openai/gpt-image-2`) and save the result as a local image file.

## Usage

Generate an image:

```bash
python scripts/nxtpath_gpt_image.py "A red bicycle leaning against a plain wall" -o out.png
```

Edit an existing image (put the edit instruction in the prompt):

```bash
python scripts/nxtpath_gpt_image.py "Make the bicycle blue, leave everything else unchanged" --edit out.png -o edited.png
```

The script path resolves relative to this skill's directory (`scripts/nxtpath_gpt_image.py` sits next to SKILL.md). After success, tell the user the printed absolute image path; if the surface supports images, display the file.

## Parameters

| Parameter | Description |
| --- | --- |
| `prompt` (required) | What to draw, or the edit instruction for `--edit` |
| `--edit IMAGE` | Edit that image instead of generating from scratch |
| `-o` / `--output` | Output file path; default auto-named by timestamp, extension detected from the actual format |
| `--model` | Default `openai/gpt-image-2`; override via `--model` or the `NXTPATH_GPT_IMAGE_MODEL` env var |
| `--size` | e.g. `1024x1024`. Some lanes decide size upstream and ignore this; trust the actual output |
| `--timeout` | Default 600 seconds (image generation is slow; be patient; do not conclude the run is stuck too early) |

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

- Image generation can take tens of seconds to several minutes; that is normal. On failure the script prints the gateway's error text; `401/403` means a key problem, `404` usually means the key has no image-model permission.
- One image per run; run again for more.
