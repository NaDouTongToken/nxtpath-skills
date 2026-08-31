#!/usr/bin/env python3
"""Nxtpath Grok video generation via the platform gateway.

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
import atexit
import base64
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://api.nxtpath.ai"
DEFAULT_MODEL = "xai/grok-imagine-video"
# Video generation is slow; timeout covers submit + poll + download.
DEFAULT_TIMEOUT = 900
POLL_INTERVAL = 5
MAX_REFS = 14
MAX_INLINE_ONE = 5 * 1024 * 1024
MAX_INLINE_TOTAL = 8 * 1024 * 1024

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


# Empirical thresholds (2026-08-29): a 1.8MB / 1280x1600 PNG was rejected by
# both grok-video and seedance i2v; the same picture at 768px long-edge / 79KB
# JPEG passed immediately. Root cause is image size/pixels, not content.
_DS_TRIGGER_BYTES = 600 * 1024
_DS_TRIGGER_EDGE = 1280
_DS_EDGE = 1024
_DS_EDGE_2 = 768
_DS_KEEP_UNDER = 800 * 1024
_DS_JPEG_Q = 85
_DS_TEMPS = []


def _ds_cleanup():
    for path in _DS_TEMPS:
        try:
            os.unlink(path)
        except OSError:
            pass


atexit.register(_ds_cleanup)


def _ds_probe_dims(path):
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head.startswith(b"\x89PNG") and len(head) >= 24:
                return struct.unpack(">II", head[16:24])
            if head.startswith(b"GIF8") and len(head) >= 10:
                w, h = struct.unpack("<HH", head[6:10])
                return w, h
            if not head.startswith(b"\xff\xd8"):
                return None
            f.seek(2)
            while True:
                hdr = f.read(4)
                if len(hdr) < 4 or hdr[0] != 0xFF:
                    return None
                code = hdr[1]
                seglen = struct.unpack(">H", hdr[2:4])[0]
                if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
                    sof = f.read(max(0, seglen - 2))
                    if len(sof) >= 5:
                        h, w = struct.unpack(">HH", sof[1:5])
                        return w, h
                    return None
                f.seek(seglen - 2, os.SEEK_CUR)
    except OSError:
        return None


def _ds_temp_jpeg():
    fd, dest = tempfile.mkstemp(prefix="nxtpath-ds-", suffix=".jpg")
    os.close(fd)
    try:
        os.unlink(dest)
    except OSError:
        pass
    _DS_TEMPS.append(dest)
    return dest


def _ds_try_pil(src, dest, edge, quality):
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(src)
        if im.mode != "RGB":
            im = im.convert("RGB")
        w, h = im.size
        long_edge = max(w, h)
        if long_edge > edge:
            scale = float(edge) / long_edge
            im = im.resize(
                (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                Image.LANCZOS,
            )
        im.save(dest, "JPEG", quality=quality, optimize=True)
        return im.size
    except Exception:
        return None


def _ds_try_dotnet(src, dest, edge, quality):
    env = os.environ.copy()
    env["NXTPATH_DS_SRC"] = os.path.abspath(src)
    env["NXTPATH_DS_DEST"] = os.path.abspath(dest)
    env["NXTPATH_DS_EDGE"] = str(int(edge))
    env["NXTPATH_DS_Q"] = str(int(quality))
    ps = (
        "Add-Type -AssemblyName System.Drawing; "
        "$src = $env:NXTPATH_DS_SRC; $dest = $env:NXTPATH_DS_DEST; "
        "$edge = [int]$env:NXTPATH_DS_EDGE; $quality = [long]$env:NXTPATH_DS_Q; "
        "$img = [System.Drawing.Image]::FromFile($src); "
        "try { "
        "$m = [Math]::Max($img.Width, $img.Height); $nw = $img.Width; $nh = $img.Height; "
        "if ($m -gt $edge) { $s = $edge / [double]$m; "
        "$nw = [Math]::Max(1, [int][Math]::Round($img.Width * $s)); "
        "$nh = [Math]::Max(1, [int][Math]::Round($img.Height * $s)); } "
        "$bmp = New-Object System.Drawing.Bitmap $nw, $nh; "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        "$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic; "
        "$g.DrawImage($img, 0, 0, $nw, $nh); $g.Dispose(); "
        "$codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | "
        "Where-Object { $_.MimeType -eq 'image/jpeg' }; "
        "$ep = New-Object System.Drawing.Imaging.EncoderParameters 1; "
        "$ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter "
        "([System.Drawing.Imaging.Encoder]::Quality, $quality); "
        "$bmp.Save($dest, $codec, $ep); $bmp.Dispose(); "
        "Write-Output ('{0} {1}' -f $nw, $nh); "
        "} finally { $img.Dispose() }"
    )
    try:
        out = subprocess.check_output(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps,
            ],
            stderr=subprocess.DEVNULL,
            env=env,
        )
        parts = out.decode("utf-8", "replace").strip().split()
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
        return _ds_probe_dims(dest)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def _ds_try_sips(src, dest, edge, quality):
    try:
        subprocess.check_call(
            [
                "sips",
                "-Z",
                str(edge),
                "-s",
                "format",
                "jpeg",
                "-s",
                "formatOptions",
                str(quality),
                src,
                "--out",
                dest,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return _ds_probe_dims(dest)
    except (OSError, subprocess.CalledProcessError):
        return None


def _ds_try_magick(src, dest, edge, quality):
    geom = "{}x{}>".format(edge, edge)
    try:
        subprocess.check_call(
            ["convert", src, "-resize", geom, "-quality", str(quality), dest],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return _ds_probe_dims(dest)
    except (OSError, subprocess.CalledProcessError):
        return None


def _ds_resize(src, dest, edge, quality):
    try:
        os.unlink(dest)
    except OSError:
        pass
    got = _ds_try_pil(src, dest, edge, quality)
    if got:
        return got
    if sys.platform == "win32":
        return _ds_try_dotnet(src, dest, edge, quality)
    if sys.platform == "darwin":
        return _ds_try_sips(src, dest, edge, quality)
    return _ds_try_magick(src, dest, edge, quality)


def _maybe_downscale_image(path):
    """Downscale an oversized local image onto a temp JPEG; never overwrite path."""
    try:
        nbytes = os.path.getsize(path)
    except OSError:
        return path
    dims = _ds_probe_dims(path)
    long_edge = max(dims) if dims else 0
    if nbytes <= _DS_TRIGGER_BYTES and long_edge <= _DS_TRIGGER_EDGE:
        return path
    dest = _ds_temp_jpeg()
    got = _ds_resize(path, dest, _DS_EDGE, _DS_JPEG_Q)
    if not got or not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        print(
            "warning: oversized reference image not downscaled "
            "(no PIL / System.Drawing / sips / ImageMagick); "
            "upstream may reject it. Resize long edge to <=1024px and "
            "re-encode as JPEG first: {}".format(path)
        )
        sys.stdout.flush()
        return path
    if os.path.getsize(dest) > _DS_KEEP_UNDER:
        got2 = _ds_resize(path, dest, _DS_EDGE_2, _DS_JPEG_Q)
        if got2:
            got = got2
    new_bytes = os.path.getsize(dest)
    ow, oh = dims if dims else ("?", "?")
    nw, nh = got
    print(
        "auto-downscaled: {} {}x{} {} bytes -> {}x{} {} bytes JPEG".format(
            path, ow, oh, nbytes, nw, nh, new_bytes
        )
    )
    sys.stdout.flush()
    return dest


def _collect_error_tokens(detail):
    """Flatten code/type/message fields from a gateway error envelope."""
    parts = [detail or ""]
    try:
        doc = json.loads(detail)
    except (ValueError, TypeError):
        return parts
    stack = [doc]
    depth = 0
    while stack and depth < 8:
        obj = stack.pop()
        depth += 1
        if isinstance(obj, dict):
            for key in ("code", "type", "error", "message"):
                value = obj.get(key)
                if isinstance(value, (str, int)):
                    parts.append(str(value))
                elif isinstance(value, dict):
                    stack.append(value)
            data = obj.get("data")
            if isinstance(data, dict):
                stack.append(data)
        elif isinstance(obj, str):
            parts.append(obj)
    return parts


def _is_quota_429(detail):
    """True when a 429 is upstream quota, not in-flight concurrency.

    Since router #2188, upstream QUOTA exhaustion also surfaces as 429 with
    error envelope UPSTREAM_PASSTHROUGH (previously 503).
    """
    hay = " ".join(_collect_error_tokens(detail)).upper()
    if "UPSTREAM_PASSTHROUGH" in hay:
        return True
    if "QUOTA" in hay:
        return True
    return False


def _http_error_exit(exc):
    detail = ""
    try:
        detail = exc.read().decode("utf-8", "replace")[:800]
    except OSError:
        pass
    hint = ""
    if exc.code == 429:
        if _is_quota_429(detail):
            hint = "\nhint: 上游配额/余额受限，请稍后再试或联系平台"
        else:
            hint = (
                "\nhint: one in-flight video job per account per resolution tier; "
                "wait for the running job to finish, then retry"
            )
    sys.exit("error: HTTP {} from gateway\n{}{}".format(exc.code, detail, hint))


def _request(url, api_key, timeout, data=None, extra_headers=None):
    headers = {
        "Authorization": "Bearer " + api_key,
        "User-Agent": "nxtpath-grok-video-skill/1.0",
    }
    if extra_headers:
        headers.update(extra_headers)
    method = "POST" if data is not None else "GET"
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _http_error_exit(e)
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


def _detect_media_type(data):
    for magic, ext in IMAGE_MAGIC:
        if data.startswith(magic):
            return IMAGE_MIME[ext]
    return "image/png"


def _is_remote_ref(value):
    lower = value.strip().lower()
    return (
        lower.startswith("http://")
        or lower.startswith("https://")
        or lower.startswith("data:")
    )


def _load_refs(values):
    if len(values) > MAX_REFS:
        sys.exit(
            "error: at most {} --ref images (got {})".format(MAX_REFS, len(values))
        )
    refs = []
    inline_total = 0
    for value in values:
        item = value.strip()
        if not item:
            sys.exit("error: empty --ref value")
        if _is_remote_ref(item):
            refs.append({"url": item})
            continue
        if not os.path.isfile(item):
            if not os.path.exists(item):
                sys.exit("error: --ref file does not exist: {}".format(item))
            sys.exit("error: --ref is not a file: {}".format(item))
        item = _maybe_downscale_image(item)
        try:
            with open(item, "rb") as f:
                raw = f.read()
        except OSError as e:
            sys.exit("error: cannot read --ref file: {}".format(e))
        n = len(raw)
        if n > MAX_INLINE_ONE:
            sys.exit(
                "error: --ref inline image exceeds 5 MiB decoded: {} ({} bytes)".format(
                    item, n
                )
            )
        inline_total += n
        if inline_total > MAX_INLINE_TOTAL:
            sys.exit(
                "error: --ref inline images exceed 8 MiB total ({} bytes)".format(
                    inline_total
                )
            )
        refs.append(
            {
                "data": base64.b64encode(raw).decode("ascii"),
                "media_type": _detect_media_type(raw),
            }
        )
    return refs


def _remaining(started, timeout):
    left = timeout - (time.time() - started)
    if left <= 0:
        sys.exit("error: timed out after {}s".format(timeout))
    return left


def _save(data, output):
    if not output:
        output = "nxtpath-grok-video-{}.mp4".format(time.strftime("%Y%m%d-%H%M%S"))
    with open(output, "wb") as f:
        f.write(data)
    return os.path.abspath(output)


def main():
    parser = argparse.ArgumentParser(
        description="Generate a video via the Nxtpath gateway."
    )
    parser.add_argument("prompt", help="what to generate, or how to animate --ref")
    parser.add_argument(
        "--ref",
        metavar="IMAGE",
        action="append",
        default=[],
        help="reference image (repeatable, up to 14); local path or http(s)/data URL",
    )
    parser.add_argument(
        "--resolution",
        choices=("480p", "720p"),
        default="480p",
        help="required by the gateway (default: %(default)s)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="seconds 1..15; omit for the upstream default",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("NXTPATH_GROK_VIDEO_MODEL", DEFAULT_MODEL),
        help="video model (default: %(default)s)",
    )
    parser.add_argument("-o", "--output", help="output mp4 path (default: auto-named in cwd)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="seconds for submit + poll + download (default: %(default)s)",
    )
    args = parser.parse_args()

    if args.duration is not None and not (1 <= args.duration <= 15):
        sys.exit("error: --duration must be 1..15 (got {})".format(args.duration))
    if args.timeout <= 0:
        sys.exit("error: --timeout must be positive (got {})".format(args.timeout))

    refs = _load_refs(args.ref)

    root, api_key, source = resolve_credentials()
    print("using key from {}, gateway {}".format(source, root))

    payload = {
        "model": args.model,
        "prompt": args.prompt,
        "resolution": args.resolution,
    }
    if args.duration is not None:
        payload["duration"] = args.duration
    if refs:
        payload["reference_images"] = refs

    started = time.time()
    submit_timeout = max(1, min(60, args.timeout))
    result = _request(
        root + "/v1/videos/generations",
        api_key,
        submit_timeout,
        data=json.dumps(payload).encode("utf-8"),
        extra_headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    request_id = result.get("request_id")
    if not request_id:
        sys.exit(
            "error: gateway returned no request_id:\n" + json.dumps(result)[:800]
        )
    print("request_id: " + request_id)
    sys.stdout.flush()

    poll_url = root + "/v1/videos/" + request_id
    while True:
        left = _remaining(started, args.timeout)
        status_doc = _request(poll_url, api_key, max(1, min(30, left)))
        status = (status_doc.get("status") or "").lower()
        if status == "done":
            break
        if status in ("failed", "expired"):
            sys.exit(
                "error: video {}:\n{}".format(
                    status, json.dumps(status_doc, ensure_ascii=False)[:2000]
                )
            )
        print(
            "status: {} (waited {:.0f}s)".format(
                status or "unknown", time.time() - started
            )
        )
        sys.stdout.flush()
        left = _remaining(started, args.timeout)
        time.sleep(min(float(POLL_INTERVAL), left))

    video = status_doc.get("video") or {}
    video_url = video.get("url")
    if not video_url:
        sys.exit(
            "error: done but no video.url:\n"
            + json.dumps(status_doc, ensure_ascii=False)[:800]
        )

    left = _remaining(started, args.timeout)
    try:
        with urllib.request.urlopen(video_url, timeout=max(1, left)) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:800]
        except OSError:
            pass
        sys.exit("error: HTTP {} downloading video\n{}".format(e.code, detail))
    except urllib.error.URLError as e:
        sys.exit("error: cannot download video: {}".format(e.reason))

    if not data:
        sys.exit("error: downloaded empty video from temporary URL")

    path = _save(data, args.output)
    delivered = video.get("duration")
    print(
        "saved: {} ({} bytes, delivered {}s, {:.0f}s total)".format(
            path, len(data), delivered, time.time() - started
        )
    )


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()
