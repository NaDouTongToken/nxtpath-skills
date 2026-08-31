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

Generate several images in one request (`--n` 1..10; default model only):

```bash
python scripts/nxtpath_gpt_image.py "A red apple on a white background" --n 2 -o apple.png
```

That writes `apple-1.png` and `apple-2.png` (suffix `-1`..`-N` before the extension). Without `-o`, each file is timestamp-named.

Edit an existing image (put the edit instruction in the prompt):

```bash
python scripts/nxtpath_gpt_image.py "Make the bicycle blue, leave everything else unchanged" --edit out.png -o edited.png
```

Edit with reference images — repeat `--edit` any number of times; the first
image is the one being edited, all later ones are references the prompt can
point at by position ("the second image is the style reference, the third is
the color palette"):

```bash
python scripts/nxtpath_gpt_image.py "Redraw the first image in the brushwork style of the second image, using the palette of the third" --edit target.png --edit style_ref.png --edit palette.png -o styled.png
```

The gpt-image upstream accepts up to 16 input images per request; the wire
format is one `image[]` part per file, in the order given.

The script path resolves relative to this skill's directory (`scripts/nxtpath_gpt_image.py` sits next to SKILL.md). After success, tell the user the printed absolute image path; if the surface supports images, display the file.

## Parameters

| Parameter | Description |
| --- | --- |
| `prompt` (required) | What to draw, or the edit instruction for `--edit` |
| `--edit IMAGE` | Edit that image instead of generating from scratch. Repeatable any number of times (upstream cap: 16): the first image is edited, every later one is a reference (style/content/palette), addressed in the prompt by position. Multi-image is sent as OpenAI `image[]` parts; not every lane accepts it — on a 400 about duplicate/unsupported image parameters, retry with `--model codex/gpt-image-2` or fall back to a single `--edit` and describe the references in the prompt. `codex/gpt-image-2` does **not** support generation `--n>1` (see `--n`) |
| `-o` / `--output` | Output file path; default auto-named by timestamp, extension detected from the actual format. With `--n>1`, suffix `-1`..`-N` is inserted before the extension |
| `--model` | Default `openai/gpt-image-2`; override via `--model` or the `NXTPATH_GPT_IMAGE_MODEL` env var. `codex/gpt-image-2` is the edit-lane fallback and does not support `n>1` |
| `--n` | Generation only (`POST /v1/images/generations` field `n`). Integer 1..10. Default `openai/gpt-image-2` supports `n=1..10`; `codex/gpt-image-2` does **not** — `--n>1` when the model starts with `codex/` is rejected locally (upstream would 400, with two distinct messages depending on where it is rejected). Not valid with `--edit` |
| `--size` | e.g. `1024x1024`. Some lanes decide size upstream and ignore this; trust the actual output |
| `--quality` | e.g. `high`. The gateway may not honour it; the script prints the tier the response actually reports |
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
- Default is one image. Use `--n` (1..10) on `openai/gpt-image-2` to generate several in one request; every saved path is printed. `--n>1` is not available on `codex/gpt-image-2`.
- 本地 `--edit` 参考图若超过约 600KB 或长边超过 1280px，会先在临时副本上缩到长边 1024px（仍大于 800KB 则再缩到 768px）并转 JPEG 再上传，不修改用户原文件。无 Pillow 时回退系统自带缩图工具；都不可用则打印提示并仍发送原图。
