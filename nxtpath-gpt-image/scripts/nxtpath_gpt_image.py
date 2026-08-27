#!/usr/bin/env python3
"""Nxtpath GPT image generation / editing via the platform gateway.

Standard-library only (urllib), so it runs anywhere Python 3.8+ exists.

The gateway is always production (https://api.nxtpath.ai) regardless of any
environment configured elsewhere; NXTPATH_BASE_URL exists only as an explicit
debug override.

Credential resolution order (first hit wins):
  1. NXTPATH_API_KEY
  2. ANTHROPIC_AUTH_TOKEN env, only when ANTHROPIC_BASE_URL is an Nxtpath domain
     (the domain check just proves the token is ours, it does NOT pick the gateway)
  3. env block of ~/.claude/settings.json, same domain check
  4. bearer token of an Nxtpath provider section in ~/.codex/config.toml
     (covers Codex-only users deployed by the Nxtpath desktop app)
  5. api_key of an Nxtpath model section in ~/.grok/config.toml
     (covers Grok-only users; Grok discovers this skill via ~/.grok/skills
     or its ~/.claude/skills compat scan)

The API key is never printed or logged.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://api.nxtpath.ai"
DEFAULT_MODEL = "openai/gpt-image-2"
# Image generation is slow; the official docs snippet uses timeout=600.
DEFAULT_TIMEOUT = 600

# Dual-domain: existing configs on the legacy domain nadoutong.org must still be recognized as our gateway.
OWN_DOMAINS = ("nxtpath.ai", "nadoutong.org")


def _is_own_base_url(base_url):
    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in OWN_DOMAINS)


def _normalize_root(base_url):
    root = base_url.strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root


def resolve_credentials():
    """Return (base_root, api_key, source_label). Gateway is always production."""
    root = _normalize_root(
        os.environ.get("NXTPATH_BASE_URL", "").strip() or DEFAULT_BASE_URL
    )

    key = os.environ.get("NXTPATH_API_KEY", "").strip()
    if key:
        return root, key, "NXTPATH_API_KEY env"

    env_base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    env_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    if env_base and env_token and _is_own_base_url(env_base):
        return root, env_token, "ANTHROPIC_* env"

    settings_path = os.path.join(
        os.path.expanduser("~"), ".claude", "settings.json"
    )
    try:
        with open(settings_path, encoding="utf-8-sig") as f:
            env = json.load(f).get("env", {})
        base = str(env.get("ANTHROPIC_BASE_URL", "")).strip()
        token = str(env.get("ANTHROPIC_AUTH_TOKEN", "")).strip()
        if base and token and _is_own_base_url(base):
            return root, token, "~/.claude/settings.json"
    except (OSError, ValueError):
        pass

    token = _codex_config_token()
    if token:
        return root, token, "~/.codex/config.toml"

    token = _grok_config_token()
    if token:
        return root, token, "~/.grok/config.toml"

    sys.exit(
        "error: no Nxtpath API key found.\n"
        "Set one of:\n"
        "  1. NXTPATH_API_KEY env var\n"
        "  2. ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL env vars pointing at the Nxtpath gateway\n"
        "  3. deploy Claude Code / Codex / Grok with the Nxtpath desktop app\n"
        "     (writes ~/.claude/settings.json, ~/.codex/config.toml or ~/.grok/config.toml)"
    )


def _toml_section_token(path, token_key):
    """Token from a TOML section whose base_url is an Nxtpath domain.

    Line-level parse on purpose (no tomllib before Py3.11): base_url and the
    token sit in the same section ([model_providers.*] for Codex,
    [model."*"] for Grok), so pairing within a section is enough.
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except OSError:
        return None
    section_base = section_token = None
    for line in text.splitlines() + ["["]:
        s = line.strip()
        if s.startswith("["):
            if section_base and section_token and _is_own_base_url(section_base):
                return section_token
            section_base = section_token = None
            continue
        m = re.match(r'base_url\s*=\s*"([^"]+)"', s)
        if m:
            section_base = m.group(1)
        m = re.match(token_key + r'\s*=\s*"([^"]+)"', s)
        if m:
            section_token = m.group(1)
    return None


def _codex_config_token():
    return _toml_section_token(
        os.path.join(os.path.expanduser("~"), ".codex", "config.toml"),
        "experimental_bearer_token",
    )


def _grok_config_token():
    return _toml_section_token(
        os.path.join(os.path.expanduser("~"), ".grok", "config.toml"),
        "api_key",
    )


def _multipart(fields, files):
    """Encode multipart/form-data. files: [(name, filename, bytes)]."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += (
            "--{b}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n"
        ).format(b=boundary, n=name, v=value).encode("utf-8")
    for name, filename, data in files:
        # Upstream validates part Content-Type; application/octet-stream is rejected with 400.
        mime = IMAGE_MIME.get(_detect_ext(data), "image/png")
        body += (
            "--{b}\r\nContent-Disposition: form-data; name=\"{n}\"; "
            "filename=\"{f}\"\r\nContent-Type: {m}\r\n\r\n"
        ).format(b=boundary, n=name, f=filename, m=mime).encode("utf-8")
        body += data + b"\r\n"
    body += "--{b}--\r\n".format(b=boundary).encode("utf-8")
    return bytes(body), "multipart/form-data; boundary=" + boundary


def _request(url, api_key, data, content_type, timeout):
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": content_type,
            "User-Agent": "nxtpath-gpt-image-skill/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:800]
        except OSError:
            pass
        sys.exit("error: HTTP {} from gateway\n{}".format(e.code, detail))
    except urllib.error.URLError as e:
        sys.exit("error: cannot reach gateway: {}".format(e.reason))


IMAGE_MAGIC = [
    (b"\x89PNG", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"RIFF", ".webp"),
    (b"GIF8", ".gif"),
]

IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _detect_ext(data):
    for magic, ext in IMAGE_MAGIC:
        if data.startswith(magic):
            return ext
    return ".png"


def _save(data, output):
    if not output:
        output = "nxtpath-gpt-image-{}{}".format(
            time.strftime("%Y%m%d-%H%M%S"), _detect_ext(data)
        )
    with open(output, "wb") as f:
        f.write(data)
    return os.path.abspath(output)


def main():
    parser = argparse.ArgumentParser(
        description="Generate or edit an image via the Nxtpath gateway."
    )
    parser.add_argument("prompt", help="what to draw, or the edit instruction")
    parser.add_argument("--edit", metavar="IMAGE", help="edit this image instead of generating")
    parser.add_argument("-o", "--output", help="output file path (default: auto-named in cwd)")
    parser.add_argument(
        "--model",
        default=os.environ.get("NXTPATH_GPT_IMAGE_MODEL", DEFAULT_MODEL),
        help="image model (default: %(default)s)",
    )
    parser.add_argument("--size", help="e.g. 1024x1024; some lanes decide size upstream and ignore this")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds (default: %(default)s)")
    args = parser.parse_args()

    root, api_key, source = resolve_credentials()
    print("using key from {}, gateway {}".format(source, root))

    if args.edit:
        try:
            with open(args.edit, "rb") as f:
                image_bytes = f.read()
        except OSError as e:
            sys.exit("error: cannot read --edit file: {}".format(e))
        fields = {"model": args.model, "prompt": args.prompt}
        if args.size:
            fields["size"] = args.size
        body, content_type = _multipart(
            fields, [("image", os.path.basename(args.edit), image_bytes)]
        )
        url = root + "/v1/images/edits"
    else:
        payload = {"model": args.model, "prompt": args.prompt}
        if args.size:
            payload["size"] = args.size
        body = json.dumps(payload).encode("utf-8")
        content_type = "application/json"
        url = root + "/v1/images/generations"

    started = time.time()
    result = _request(url, api_key, body, content_type, args.timeout)

    items = result.get("data") or []
    if not items:
        sys.exit("error: gateway returned no image data:\n" + json.dumps(result)[:800])
    item = items[0]

    if item.get("b64_json"):
        image = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=args.timeout) as resp:
            image = resp.read()
    else:
        sys.exit("error: image entry has neither b64_json nor url:\n" + json.dumps(item)[:400])

    path = _save(image, args.output)
    print("saved: {} ({} bytes, {:.0f}s)".format(path, len(image), time.time() - started))
    if item.get("revised_prompt"):
        print("revised prompt: " + item["revised_prompt"])


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()
