# Nxtpath Skills

Official skills repository of [Nxtpath](https://nxtpath.ai).

These skills call the Nxtpath gateway directly with your platform key. They cover features that official CLIs hardwire to their own backends (and therefore ignore a custom gateway).

## Skills

| Skill | Description |
| --- | --- |
| [nxtpath-gpt-image](./nxtpath-gpt-image) | Generate or edit an image via the Nxtpath gateway using the GPT image model (gpt-image-2). Use when the user asks to generate an image, draw something, or edit an existing picture; when a CLI's built-in image generation is unusable because it is hardwired to the official backend and ignores custom gateways (Codex/Claude/Grok CLIs); or explicitly invokes /nxtpath-gpt-image. Saves the image as a local file and prints its absolute path. |
| [nxtpath-grok-video](./nxtpath-grok-video) | Generate a video via the Nxtpath gateway using the Grok video model, including image-to-video from reference images. Use when the user asks to generate a video, animate a still, or make a clip from a prompt or reference image; when a CLI's built-in video generation is unusable because it is hardwired to the official backend and ignores custom gateways (Codex/Claude/Grok CLIs); or explicitly invokes /nxtpath-grok-video. Saves the video as a local mp4 and prints its absolute path. |
| [nxtpath-seedance-video](./nxtpath-seedance-video) | Generate a video via the Nxtpath gateway using the Seedance video model on the ark task line, including image-to-video and video-to-video from public reference URLs. Use when the user asks to generate a Seedance video, animate a still from a public image URL, or make a clip from a prompt or reference URL; when a CLI's built-in video generation is unusable because it is hardwired to the official backend and ignores custom gateways (Codex/Claude/Grok CLIs); or explicitly invokes /nxtpath-seedance-video. Saves the video as a local mp4 and prints its absolute path. |

## Install

- **Nxtpath desktop app:** Skills → Discover. This repository is listed there; install from the catalog.
- **Manual:** copy a skill folder into `~/.claude/skills`, `~/.codex/skills`, or `~/.grok/skills`.

Skills are plain `SKILL.md` plus scripts. They work with Claude Code, Codex, and Grok CLI.
