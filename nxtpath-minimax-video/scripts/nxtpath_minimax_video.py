#!/usr/bin/env python3
"""Nxtpath MiniMax H3 video generation via the platform gateway ark task line.

Standard-library only (urllib), so it runs anywhere Python 3.8+ exists.
Pillow / System.Drawing / sips / ImageMagick are optional downscale helpers.

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
import urllib.parse
import urllib.request
import uuid
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://api.nxtpath.ai"
DEFAULT_MODEL = "minimax/minimax-h3"
# Video generation is slow; timeout covers submit + poll + download.
DEFAULT_TIMEOUT = 900
POLL_INTERVAL = 5
USER_AGENT = "nxtpath-minimax-video-skill/1.0"

# Gateway hard-cap is 10 MiB for the whole submit body; leave headroom for
# JSON wrapping and base64 expansion (~33%).
MAX_BODY_BYTES = 7 * 1024 * 1024

ALLOWED_RESOLUTIONS = ("720p", "960p", "2k")
ALLOWED_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
DURATION_MIN = 4
DURATION_MAX = 12

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


# Empirical thresholds (same pipeline as nxtpath-grok-video): a 1.8MB /
# 1280x1600 PNG was rejected by both grok-video and seedance i2v; the same
# picture at 768px long-edge / 79KB JPEG passed immediately.
_DS_TRIGGER_BYTES = 600 * 1024
_DS_TRIGGER_EDGE = 1280
_DS_EDGE = 1024
_DS_EDGE_2 = 768
_DS_KEEP_UNDER = 800 * 1024
_DS_JPEG_Q = 85
_DS_TEMPS = []
_DS_EXTRA_EDGES = (768, 512, 384, 256)


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
            "will retry smaller sizes or refuse if the body exceeds ~7 MiB: {}".format(
                path
            )
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


def _encode_data_uri(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        sys.exit("error: cannot read image file: {}".format(e))
    if not raw:
        sys.exit("error: empty image file: {}".format(path))
    mime = _detect_media_type(raw)
    return "data:{};base64,{}".format(mime, base64.b64encode(raw).decode("ascii"))


def _force_downscale(src, edge, quality):
    dest = _ds_temp_jpeg()
    got = _ds_resize(src, dest, edge, quality)
    if not got or not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        return None
    return dest


def _is_public_url(value):
    lower = value.strip().lower()
    return lower.startswith("http://") or lower.startswith("https://")


def _is_data_url(value):
    return value.strip().lower().startswith("data:")


def _read_http_error(exc):
    try:
        return exc.read().decode("utf-8", "replace")[:800]
    except OSError:
        return ""


def _is_model_not_available(detail):
    text = detail or ""
    if "MODEL_NOT_AVAILABLE" in text:
        return True
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return False
    codes = []
    if isinstance(doc, dict):
        err = doc.get("error")
        if isinstance(err, dict):
            codes.extend([err.get("code"), err.get("type")])
        elif isinstance(err, str):
            codes.append(err)
        codes.extend([doc.get("code"), doc.get("type")])
    return any(str(c) == "MODEL_NOT_AVAILABLE" for c in codes if c)


def _error_hint(code, detail):
    if code in (401, 403):
        return "\nhint: 401/403 means a key problem; check NXTPATH_API_KEY / deployed credentials"
    if code == 404 and _is_model_not_available(detail):
        return (
            "\nhint: MODEL_NOT_AVAILABLE — list models from GET /v1/models and pass "
            "the MiniMax SKU with --model or NXTPATH_MINIMAX_MODEL "
            "(currently minimax/minimax-h3)"
        )
    return ""


def _http_error_exit(exc, detail=None):
    if detail is None:
        detail = _read_http_error(exc)
    sys.exit(
        "error: HTTP {} from gateway\n{}{}".format(
            exc.code, detail, _error_hint(exc.code, detail)
        )
    )


def _request(url, api_key, timeout, data=None, extra_headers=None):
    headers = {
        "Authorization": "Bearer " + api_key,
        "User-Agent": USER_AGENT,
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
        _http_error_exit(e, _read_http_error(e))
    except urllib.error.URLError as e:
        sys.exit("error: cannot reach gateway: {}".format(e.reason))


def _download(url, api_key, timeout):
    headers = {
        "Authorization": "Bearer " + api_key,
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:800]
        except OSError:
            pass
        sys.exit("error: HTTP {} downloading video\n{}".format(e.code, detail))
    except urllib.error.URLError as e:
        sys.exit("error: cannot download video: {}".format(e.reason))


_LOCAL_TOO_LARGE = "error: 本地图过大，请缩图或改用公网 URL"


def _load_image(flag, value):
    """Return (url, original_local_path_or_None)."""
    item = (value or "").strip()
    if not item:
        sys.exit("error: empty {} value".format(flag))
    if _is_public_url(item) or _is_data_url(item):
        return item, None
    if not os.path.isfile(item):
        if not os.path.exists(item):
            sys.exit("error: {} file does not exist: {}".format(flag, item))
        sys.exit("error: {} is not a file: {}".format(flag, item))
    src = os.path.abspath(item)
    encoded = _encode_data_uri(_maybe_downscale_image(src))
    return encoded, src


def _body_size(payload):
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _set_content_url(payload, index, url):
    payload["input"]["content"][index]["image_url"]["url"] = url


def _ensure_body_budget(payload, local_slots):
    """Keep the JSON body under MAX_BODY_BYTES by shrinking local inlines."""
    size = _body_size(payload)
    if size <= MAX_BODY_BYTES:
        return
    if not local_slots:
        sys.exit(_LOCAL_TOO_LARGE)
    for edge in _DS_EXTRA_EDGES:
        progressed = False
        for index, src in local_slots:
            dest = _force_downscale(src, edge, _DS_JPEG_Q)
            if dest is None:
                continue
            _set_content_url(payload, index, _encode_data_uri(dest))
            progressed = True
        size = _body_size(payload)
        if size <= MAX_BODY_BYTES:
            print(
                "auto-downscaled: request body now {} bytes (long-edge {})".format(
                    size, edge
                )
            )
            sys.stdout.flush()
            return
        if not progressed:
            break
    sys.exit(_LOCAL_TOO_LARGE)


def _validate_resolution(value):
    key = (value or "").strip().lower()
    if key not in ALLOWED_RESOLUTIONS:
        sys.exit(
            "error: --resolution {} is not supported (must be {})".format(
                value, ", ".join(ALLOWED_RESOLUTIONS)
            )
        )
    return key


def _validate_duration(value):
    if not (DURATION_MIN <= value <= DURATION_MAX):
        sys.exit(
            "error: --duration {} is out of range (must be integer {}–{})".format(
                value, DURATION_MIN, DURATION_MAX
            )
        )
    return value


def _validate_ratio(value):
    if value not in ALLOWED_RATIOS:
        sys.exit(
            "error: --ratio {} is not supported (must be {})".format(
                value, ", ".join(ALLOWED_RATIOS)
            )
        )
    return value


_MUTEX_ERROR = (
    "error: keyframe mode (--first-frame/--last-frame) and reference-image "
    "mode (--ref-image) are mutually exclusive per the MiniMax docs; "
    "keyframes and reference images cannot be mixed, and neither mode can "
    "include reference audio or video"
)


def _build_payload(
    model, prompt, resolution, duration, ratio, first_frame, last_frame, ref_images
):
    content = [{"type": "text", "text": prompt}]
    local_slots = []

    def add_image(url, src, role=None):
        part = {"type": "image_url", "image_url": {"url": url}}
        if role:
            part["role"] = role
        content.append(part)
        if src:
            local_slots.append((len(content) - 1, src))

    if first_frame is not None:
        url, src = first_frame
        add_image(url, src, "first_frame")
    if last_frame is not None:
        url, src = last_frame
        add_image(url, src, "last_frame")
    for url, src in ref_images:
        add_image(url, src)

    payload = {
        "model": model,
        "input": {"content": content},
        "parameters": {
            "resolution": resolution,
            "duration": duration,
            "ratio": ratio,
        },
    }
    _ensure_body_budget(payload, local_slots)
    return payload


def _submit(root, api_key, payload, timeout):
    url = root + "/v1/tasks/submit"
    headers = {
        "Authorization": "Bearer " + api_key,
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Idempotency-Key": str(uuid.uuid4()),
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _http_error_exit(e, _read_http_error(e))
    except urllib.error.URLError as e:
        sys.exit("error: cannot reach gateway: {}".format(e.reason))


def _timed_out(timeout, last_doc):
    payload = ""
    if last_doc is not None:
        payload = "\n" + json.dumps(last_doc, ensure_ascii=False)[:2000]
    sys.exit("error: timed out after {}s{}".format(timeout, payload))


def _artifact_url(root, url):
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return root + url
    return url


def _output_paths(output, count):
    if not output:
        output = "nxtpath-minimax-{}.mp4".format(time.strftime("%Y%m%d-%H%M%S"))
    if count == 1:
        return [output]
    root, ext = os.path.splitext(output)
    if not ext:
        ext = ".mp4"
    return ["{}-{}{}".format(root, index, ext) for index in range(1, count + 1)]


def _save(data, output):
    with open(output, "wb") as f:
        f.write(data)
    return os.path.abspath(output)


def _usage_from_status(status_doc, poll_output):
    usage = status_doc.get("usage") if isinstance(status_doc, dict) else None
    if isinstance(usage, dict):
        return usage
    usage = poll_output.get("usage") if isinstance(poll_output, dict) else None
    if isinstance(usage, dict):
        return usage
    return {}


def main():
    parser = argparse.ArgumentParser(
        description="Generate a MiniMax H3 video via the Nxtpath gateway ark task line."
    )
    parser.add_argument("prompt", help="what to generate, or how to animate a keyframe / reference image")
    parser.add_argument(
        "--first-frame",
        metavar="PATH_OR_URL",
        default=None,
        help="first-frame keyframe; local path (inlined as data:) or public http(s) URL",
    )
    parser.add_argument(
        "--last-frame",
        metavar="PATH_OR_URL",
        default=None,
        help="last-frame keyframe; local path (inlined as data:) or public http(s) URL",
    )
    parser.add_argument(
        "--ref-image",
        metavar="PATH_OR_URL",
        action="append",
        default=[],
        help="reference image (repeatable); local path (inlined) or public http(s) URL; exclusive with keyframes",
    )
    parser.add_argument(
        "--resolution",
        default="720p",
        help="720p / 960p / 2k (default: %(default)s)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=4,
        help="integer seconds 4–12 (default: %(default)s)",
    )
    parser.add_argument(
        "--ratio",
        default="16:9",
        help="21:9 / 16:9 / 4:3 / 1:1 / 3:4 / 9:16 (default: %(default)s)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("NXTPATH_MINIMAX_MODEL", DEFAULT_MODEL),
        help="video model (default: %(default)s)",
    )
    parser.add_argument("-o", "--output", help="output mp4 path (default: auto-named in cwd)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="seconds for submit + poll + download (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the final request-body JSON and exit without submitting",
    )
    args = parser.parse_args()

    if args.timeout <= 0:
        sys.exit("error: --timeout must be positive (got {})".format(args.timeout))

    resolution = _validate_resolution(args.resolution)
    duration = _validate_duration(args.duration)
    ratio = _validate_ratio(args.ratio)

    has_keyframe = bool(args.first_frame or args.last_frame)
    has_ref = bool(args.ref_image)
    if has_keyframe and has_ref:
        sys.exit(_MUTEX_ERROR)

    first_frame = (
        _load_image("--first-frame", args.first_frame) if args.first_frame else None
    )
    last_frame = (
        _load_image("--last-frame", args.last_frame) if args.last_frame else None
    )
    ref_images = [_load_image("--ref-image", item) for item in args.ref_image]

    payload = _build_payload(
        args.model,
        args.prompt,
        resolution,
        duration,
        ratio,
        first_frame,
        last_frame,
        ref_images,
    )

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    root, api_key, source = resolve_credentials()
    print("using key from {}, gateway {}".format(source, root))

    started = time.time()
    submit_timeout = max(1, min(60, args.timeout))
    result = _submit(root, api_key, payload, submit_timeout)
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    task_id = output.get("task_id")
    request_id = result.get("request_id")
    if not request_id:
        sys.exit(
            "error: gateway returned no request_id:\n" + json.dumps(result)[:800]
        )
    if not task_id:
        sys.exit(
            "error: gateway returned no output.task_id:\n" + json.dumps(result)[:800]
        )
    print("request_id: " + request_id)
    print("task_id: " + task_id)
    sys.stdout.flush()

    poll_url = root + "/v1/tasks/status?" + urllib.parse.urlencode({"task_id": task_id})
    status_doc = None
    poll_output = {}
    while True:
        left = args.timeout - (time.time() - started)
        if left <= 0:
            _timed_out(args.timeout, status_doc)
        status_doc = _request(poll_url, api_key, max(1, min(30, left)))
        poll_output = (
            status_doc.get("output") if isinstance(status_doc.get("output"), dict) else {}
        )
        status = poll_output.get("task_status") or ""
        if status == "Success":
            break
        if status in ("Failure", "Expired"):
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
        left = args.timeout - (time.time() - started)
        if left <= 0:
            _timed_out(args.timeout, status_doc)
        time.sleep(min(float(POLL_INTERVAL), left))

    urls = poll_output.get("urls") or []
    if not isinstance(urls, list) or not urls:
        sys.exit(
            "error: Success but no output.urls:\n"
            + json.dumps(status_doc, ensure_ascii=False)[:800]
        )

    paths = _output_paths(args.output, len(urls))
    saved = []
    for url, path in zip(urls, paths):
        left = args.timeout - (time.time() - started)
        if left <= 0:
            _timed_out(args.timeout, status_doc)
        data = _download(_artifact_url(root, url), api_key, max(1, left))
        if not data:
            sys.exit("error: downloaded empty video from authenticated handle")
        saved.append((_save(data, path), len(data)))

    usage = _usage_from_status(status_doc, poll_output)
    elapsed = time.time() - started
    usage_txt = json.dumps(usage, ensure_ascii=False)
    if len(saved) == 1:
        path, nbytes = saved[0]
        print(
            "saved: {} ({} bytes, usage {}, {:.0f}s total)".format(
                path, nbytes, usage_txt, elapsed
            )
        )
    else:
        for path, nbytes in saved:
            print("saved: {} ({} bytes)".format(path, nbytes))
        print("usage {}, {:.0f}s total".format(usage_txt, elapsed))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()
