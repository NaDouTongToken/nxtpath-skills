#!/usr/bin/env python3
"""Nxtpath Seedance video generation via the platform gateway ark task line.

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

Local --ref-image files are inlined as data: URLs (the ark line accepts that).
If the encoded images would exceed about 9 MiB, a temporary copy is downscaled
so the submit body stays under the gateway's 10 MiB cap. Local --ref-video
files are uploaded through the signer: an inline data:video is accepted by
the gateway but the task stays Pending (measured 2026-10-10). The signer
holds the storage credentials; this skill never does.
NXTPATH_UPLOAD_SIGNER_URL overrides the hard-coded signer for debugging.
"""

import argparse
import atexit
import base64
import json
import os
import re
import socket
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
DEFAULT_MODEL = "doubao/seedance-2.5"
# seedance-2.5 fails every reference-video task upstream (2026-10-10: inline data:
# and OSS https URL both end in Failure, while text and image references succeed),
# so a reference video without an explicit model runs on 2.0 instead.
VIDEO_REF_MODEL = "doubao/seedance-2.0"
# Production signer. Deploy prints SIGNER_URL=; keep this value in sync with minimax.
DEFAULT_UPLOAD_SIGNER_URL = "https://nxtpathd-signer-cjtxrmbgtv.cn-hangzhou.fcapp.run"
# Video generation is slow; timeout covers submit + poll + download.
DEFAULT_TIMEOUT = 900
POLL_INTERVAL = 5
USER_AGENT = "nxtpath-seedance-video-skill/1.0"
# Seedance reference-video upstream limit. Checked before any upload.
MAX_VIDEO_BYTES = 50 * 1024 * 1024
# Gateway /v1/tasks/submit cap is 10 MiB. Leave room for the JSON envelope.
INLINE_ENCODED_BUDGET = 9 * 1024 * 1024
DOWNLOAD_ATTEMPTS = 4

RESOLUTION_20 = ("480p", "720p")
RESOLUTION_25 = ("480p", "720p", "1080p")
DURATION_20 = (4, 15)
DURATION_25 = (4, 30)
SMART_DURATION = -1

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


# Same resizers as nxtpath-minimax-video. Used only when inline images would
# exceed the gateway body budget; smaller files are inlined unchanged.
_DS_EDGES = (1024, 768, 512, 384, 256)
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


def _force_downscale(src, edge, quality):
    dest = _ds_temp_jpeg()
    got = _ds_resize(src, dest, edge, quality)
    if not got or not os.path.isfile(dest) or os.path.getsize(dest) == 0:
        return None
    return dest


def _downscale_under(src, max_bytes):
    """Return a JPEG path of at most max_bytes, or None. Never modifies src."""
    try:
        nbytes = os.path.getsize(src)
    except OSError as exc:
        sys.exit("error: cannot read image file: {}".format(exc))
    dims = _ds_probe_dims(src)
    tool_failed = True
    for edge in _DS_EDGES:
        dest = _force_downscale(src, edge, _DS_JPEG_Q)
        if dest is None:
            continue
        tool_failed = False
        last = os.path.getsize(dest)
        got = _ds_probe_dims(dest) or ("?", "?")
        ow, oh = dims if dims else ("?", "?")
        print(
            "auto-downscaled: {} {}x{} {} bytes -> {}x{} {} bytes JPEG (inline budget ~9 MiB)".format(
                src, ow, oh, nbytes, got[0], got[1], last
            )
        )
        sys.stdout.flush()
        if last <= max_bytes:
            return dest
    if tool_failed:
        return None
    return None


def _sniff_image(path):
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError as exc:
        sys.exit("error: cannot read image file: {}".format(exc))
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_public_url(value):
    lower = value.strip().lower()
    return lower.startswith("http://") or lower.startswith("https://")


def _is_data_url(value):
    return value.strip().lower().startswith("data:")


def _signer_url():
    return os.environ.get("NXTPATH_UPLOAD_SIGNER_URL", "").strip() or DEFAULT_UPLOAD_SIGNER_URL


def _redact(text, secrets):
    value = "" if text is None else str(text)
    for secret in secrets:
        if secret and len(secret) >= 8 and secret in value:
            value = value.replace(secret, "***")
    return value


def _encode_multipart(fields, filename, content_type, data):
    """Multipart body. The file part is last, which OSS PostObject requires."""
    for _ in range(4):
        boundary = "nxtpath" + uuid.uuid4().hex
        if boundary.encode("ascii") not in data:
            break
    chunks = []
    for name, field_value in fields.items():
        chunks.append(
            "--{b}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n".format(
                b=boundary, n=name, v=field_value
            ).encode("utf-8")
        )
    chunks.append(
        (
            "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{f}\"\r\n"
            "Content-Type: {ct}\r\n\r\n"
        ).format(b=boundary, f=filename, ct=content_type).encode("utf-8")
    )
    chunks.append(data)
    chunks.append("\r\n--{b}--\r\n".format(b=boundary).encode("ascii"))
    body = b"".join(chunks)
    header = "multipart/form-data; boundary=" + boundary
    return header, body


def _data_url(mime, raw):
    return "data:{0};base64,{1}".format(mime, base64.b64encode(raw).decode("ascii"))


def _encoded_len(mime, nbytes):
    prefix = len("data:{0};base64,".format(mime))
    return prefix + (4 * ((nbytes + 2) // 3))


def _entry_encoded(entry):
    if entry["kind"] == "url":
        url = entry["url"]
        if url.lower().startswith("data:"):
            return len(url.encode("utf-8"))
        return 0
    return _encoded_len(entry["mime"], len(entry["data"]))


def _inline_total(entries):
    return sum(_entry_encoded(entry) for entry in entries)


def _fit_inline_budget(entries):
    """Downscale local files until encoded data: URLs fit INLINE_ENCODED_BUDGET."""
    if _inline_total(entries) <= INLINE_ENCODED_BUDGET:
        return
    tool_failed = False
    for entry in entries:
        if entry["kind"] != "file":
            continue
        if _inline_total(entries) <= INLINE_ENCODED_BUDGET:
            return
        others = _inline_total(entries) - _entry_encoded(entry)
        allowance = INLINE_ENCODED_BUDGET - others
        prefix = len("data:image/jpeg;base64,")
        if allowance <= prefix + 4:
            continue
        target = (allowance - prefix) * 3 // 4
        if len(entry["data"]) <= target and _entry_encoded(entry) <= allowance:
            continue
        dest = _downscale_under(entry["path"], target)
        if dest is None:
            tool_failed = True
            continue
        try:
            with open(dest, "rb") as handle:
                data = handle.read()
        except OSError:
            tool_failed = True
            continue
        if not data or _encoded_len("image/jpeg", len(data)) > allowance:
            tool_failed = True
            continue
        entry["mime"] = "image/jpeg"
        entry["data"] = data
    total = _inline_total(entries)
    if total <= INLINE_ENCODED_BUDGET:
        return
    if tool_failed:
        sys.exit(
            "error: inline reference images are {0} bytes encoded, over the ~9 MiB "
            "gateway body budget, and no downscale tool could shrink them "
            "(Pillow, System.Drawing, sips, or ImageMagick). "
            "Shrink the image or pass a public https URL.".format(total)
        )
    sys.exit(
        "error: inline reference images are {0} bytes encoded, over the ~9 MiB "
        "gateway body budget, and downscaling could not shrink them enough. "
        "Pass a public https URL instead.".format(total)
    )


def _resolve_ref_images(values, dry_run):
    """image_url values. Local files become data: URLs. Dry-run does no encoding."""
    entries = []
    for value in values:
        item = (value or "").strip()
        if not item:
            sys.exit("error: empty --ref-image value")
        if _is_data_url(item):
            if item.lower().startswith("data:video/"):
                sys.exit(
                    "error: inline video is not supported; pass a local file or a public http(s) URL"
                )
            entries.append({"kind": "url", "url": item})
            continue
        if _is_public_url(item):
            entries.append({"kind": "url", "url": item})
            continue
        if not os.path.isfile(item):
            if not os.path.exists(item):
                sys.exit("error: --ref-image file does not exist: {}".format(item))
            sys.exit("error: --ref-image is not a file: {}".format(item))
        mime = _sniff_image(item)
        if mime is None:
            sys.exit(
                "error: unsupported image type (need jpeg, png, webp, or gif): {}".format(
                    item
                )
            )
        if dry_run:
            try:
                nbytes = os.path.getsize(item)
            except OSError as exc:
                sys.exit("error: cannot read image file: {}".format(exc))
            entries.append({
                "kind": "url",
                "url": "<inline-image:{0},{1} bytes>".format(
                    os.path.basename(item), nbytes
                ),
            })
            continue
        try:
            with open(item, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            sys.exit("error: cannot read image file: {}".format(exc))
        if not data:
            sys.exit("error: empty image file: {}".format(item))
        entries.append({"kind": "file", "path": item, "mime": mime, "data": data})
    if not dry_run:
        _fit_inline_budget(entries)
    urls = []
    for entry in entries:
        if entry["kind"] == "url":
            urls.append(entry["url"])
        else:
            urls.append(_data_url(entry["mime"], entry["data"]))
    if not dry_run:
        encoded = 0
        for url in urls:
            if url.lower().startswith("data:"):
                encoded += len(url.encode("utf-8"))
        if encoded > INLINE_ENCODED_BUDGET:
            sys.exit(
                "error: inline reference images are {0} bytes encoded, over the ~9 MiB "
                "gateway body budget, and downscaling could not shrink them enough. "
                "Pass a public https URL instead.".format(encoded)
            )
    return urls


def _video_content_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp4":
        return "video/mp4"
    if ext == ".mov":
        return "video/quicktime"
    return None


def _prepare_video_file(path):
    """Extension + ftyp box, then the 50 MiB cap. No network."""
    content_type = _video_content_type(path)
    if content_type is None:
        sys.exit("error: --ref-video must be an .mp4 or .mov file: {}".format(path))
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
    except OSError as exc:
        sys.exit("error: cannot read video file: {}".format(exc))
    if len(head) < 8 or head[4:8] != b"ftyp":
        sys.exit(
            "error: --ref-video is not an mp4/mov container "
            "(expected an ftyp box at offset 4): {}".format(path)
        )
    try:
        nbytes = os.path.getsize(path)
    except OSError as exc:
        sys.exit("error: cannot read video file: {}".format(exc))
    if nbytes < 1:
        sys.exit("error: empty video file: {}".format(path))
    if nbytes > MAX_VIDEO_BYTES:
        sys.exit(
            "error: --ref-video is {0} bytes, over the 50 MiB limit ({1}). "
            "Shrink the video or pass a public https URL: {2}".format(
                nbytes, MAX_VIDEO_BYTES, path
            )
        )
    return content_type


def _signer_failure(exc, signer, api_key):
    if isinstance(exc, urllib.error.HTTPError):
        detail = _redact(_read_http_error(exc), (api_key,))
        if exc.code == 401:
            sys.exit("error: key invalid")
        sys.exit(
            "error: upload signer returned HTTP {0}\n{1}".format(exc.code, detail)
        )
    reason = getattr(exc, "reason", exc)
    sys.exit(
        "error: cannot reach the upload signer at {0}: {1}. "
        "Check the network, then retry. Debug override: NXTPATH_UPLOAD_SIGNER_URL.".format(
            signer, reason
        )
    )


def _request_upload(api_key, filename, content_type, size, timeout):
    signer = _signer_url()
    payload = json.dumps(
        {"filename": filename, "content_type": content_type, "size": size}
    ).encode("utf-8")
    req = urllib.request.Request(
        signer,
        data=payload,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        _signer_failure(exc, signer, api_key)
    except urllib.error.URLError as exc:
        _signer_failure(exc, signer, api_key)
    except (TimeoutError, OSError) as exc:
        _signer_failure(exc, signer, api_key)
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        sys.exit("error: upload signer returned a non-JSON body")
    if not isinstance(doc, dict):
        sys.exit("error: upload signer returned an unexpected body")
    upload = doc.get("upload") if isinstance(doc.get("upload"), dict) else {}
    fields = upload.get("fields") if isinstance(upload.get("fields"), dict) else None
    get_url = doc.get("get_url")
    post_url = upload.get("url")
    if not isinstance(get_url, str) or not get_url.startswith("https://"):
        sys.exit("error: upload signer did not return an https get_url")
    if not isinstance(post_url, str) or not post_url.startswith("https://"):
        sys.exit("error: upload signer did not return an https upload url")
    if not fields:
        sys.exit("error: upload signer did not return form fields")
    return post_url, fields, get_url


def _post_oss(post_url, fields, filename, content_type, data, timeout, api_key):
    header, body = _encode_multipart(fields, filename, content_type, data)
    req = urllib.request.Request(
        post_url,
        data=body,
        headers={"Content-Type": header, "User-Agent": USER_AGENT},
        method="POST",
    )
    secrets = [api_key]
    for value in fields.values():
        if isinstance(value, str):
            secrets.append(value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(256)
            return
    except urllib.error.HTTPError as exc:
        detail = _redact(_read_http_error(exc), secrets)
        sys.exit("error: OSS upload failed: HTTP {0}\n{1}".format(exc.code, detail))
    except urllib.error.URLError as exc:
        sys.exit(
            "error: cannot reach OSS to upload the file: {0}. "
            "Check the network, then retry.".format(exc.reason)
        )
    except (TimeoutError, OSError) as exc:
        sys.exit(
            "error: cannot reach OSS to upload the file: {0}. "
            "Check the network, then retry.".format(exc)
        )


def _upload_video(path, content_type, api_key, timeout):
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        sys.exit("error: cannot read video file: {}".format(exc))
    if not data:
        sys.exit("error: empty video file: {}".format(path))
    if len(data) > MAX_VIDEO_BYTES:
        sys.exit(
            "error: --ref-video is {0} bytes, over the 50 MiB limit ({1}). "
            "Shrink the video or pass a public https URL: {2}".format(
                len(data), MAX_VIDEO_BYTES, path
            )
        )
    filename = os.path.basename(path) or "video.mp4"
    post_url, fields, get_url = _request_upload(
        api_key, filename, content_type, len(data), timeout
    )
    file_type = fields.get("Content-Type") or content_type
    _post_oss(post_url, fields, filename, file_type, data, timeout, api_key)
    return get_url


def _resolve_ref_videos(values, api_key, timeout, dry_run):
    """video_url values. Local files are uploaded. Dry-run does not upload."""
    urls = []
    for value in values:
        item = (value or "").strip()
        if not item:
            sys.exit("error: empty --ref-video value")
        if _is_data_url(item):
            sys.exit(
                "error: inline video is not supported; pass a local file or a public http(s) URL"
            )
        if _is_public_url(item):
            urls.append(item)
            continue
        if not os.path.isfile(item):
            if not os.path.exists(item):
                sys.exit("error: --ref-video file does not exist: {}".format(item))
            sys.exit("error: --ref-video is not a file: {}".format(item))
        content_type = _prepare_video_file(item)
        if dry_run:
            urls.append("<oss-upload:{}>".format(os.path.basename(item)))
            continue
        urls.append(_upload_video(os.path.abspath(item), content_type, api_key, timeout))
    return urls


def _read_http_error(exc):
    try:
        return exc.read().decode("utf-8", "replace")[:800]
    except OSError:
        return ""


def _http_error_exit(exc, detail=None):
    if detail is None:
        detail = _read_http_error(exc)
    sys.exit("error: HTTP {} from gateway\n{}".format(exc.code, detail))


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


def _swapped_namespace(model):
    """doubao/seedance-X ↔ seedance/seedance-X for the rename transition."""
    if model.startswith("doubao/"):
        return "seedance/" + model[len("doubao/") :]
    if model.startswith("seedance/"):
        return "doubao/" + model[len("seedance/") :]
    return None


def _one_line(value):
    text = " ".join(str(value).split())
    if len(text) > 300:
        return text[:300]
    return text


def _warn_transient(where, reason):
    print("warning: {} hit a transient error ({}); retrying".format(where, _one_line(reason)))
    sys.stdout.flush()


def _sleep_remaining(delay, started, timeout):
    left = timeout - (time.time() - started)
    if left <= 0:
        return False
    time.sleep(min(float(delay), left))
    return True


def _open_json(url, api_key, timeout):
    headers = {
        "Authorization": "Bearer " + api_key,
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _poll_until_done(poll_url, api_key, timeout, started, task_id):
    """Poll until Success. Transient errors retry until the overall timeout."""
    status_doc = None
    poll_output = {}
    backoff = 2
    while True:
        left = timeout - (time.time() - started)
        if left <= 0:
            _timed_out(timeout, status_doc, task_id)
        try:
            status_doc = _open_json(poll_url, api_key, max(1, min(30, left)))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                _read_http_error(exc)
                _warn_transient("status poll", "HTTP {}".format(exc.code))
                if not _sleep_remaining(backoff, started, timeout):
                    _timed_out(timeout, status_doc, task_id)
                backoff = min(backoff * 2, 30)
                continue
            _http_error_exit(exc)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            reason = getattr(exc, "reason", None)
            _warn_transient("status poll", reason if reason is not None else exc)
            if not _sleep_remaining(backoff, started, timeout):
                _timed_out(timeout, status_doc, task_id)
            backoff = min(backoff * 2, 30)
            continue
        except ValueError:
            sys.exit("error: gateway returned non-JSON status\ntask_id: {}".format(task_id))
        backoff = 2
        poll_output = (
            status_doc.get("output") if isinstance(status_doc.get("output"), dict) else {}
        )
        status = poll_output.get("task_status") or ""
        if status == "Success":
            return status_doc, poll_output
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
        if not _sleep_remaining(POLL_INTERVAL, started, timeout):
            _timed_out(timeout, status_doc, task_id)


def _download_once(url, api_key, timeout):
    headers = {
        "Authorization": "Bearer " + api_key,
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _download_with_retry(url, api_key, timeout, started, task_id):
    """Retry transient download failures a few times, bounded by the overall timeout."""
    backoff = 2
    last_detail = "unknown error"
    for attempt in range(DOWNLOAD_ATTEMPTS):
        left = timeout - (time.time() - started)
        if left <= 0:
            _timed_out(timeout, None, task_id)
        try:
            return _download_once(url, api_key, max(1, min(120, left)))
        except urllib.error.HTTPError as exc:
            detail = _read_http_error(exc)
            if exc.code != 429 and exc.code < 500:
                sys.exit(
                    "error: HTTP {} downloading video\n{}\ntask_id: {}".format(
                        exc.code, detail, task_id
                    )
                )
            last_detail = "HTTP {}".format(exc.code)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            reason = getattr(exc, "reason", None)
            last_detail = reason if reason is not None else exc
        if attempt + 1 >= DOWNLOAD_ATTEMPTS:
            break
        _warn_transient("artifact download", last_detail)
        if not _sleep_remaining(backoff, started, timeout):
            _timed_out(timeout, None, task_id)
        backoff = min(backoff * 2, 30)
    sys.exit(
        "error: cannot download video after {} attempts ({})\ntask_id: {}".format(
            DOWNLOAD_ATTEMPTS, _one_line(last_detail), task_id
        )
    )


def _is_seedance_25(model):
    return "2.5" in (model or "").lower()


def _family_label(model):
    name = (model or "").lower()
    label = "seedance-2.5" if "2.5" in name else "seedance-2.0"
    if "mini" in name:
        label += "-mini"
    return label


def _pick_model(explicit, ref_videos):
    if explicit:
        if ref_videos and _is_seedance_25(explicit):
            print(
                "warning: {} currently fails reference-video tasks upstream; "
                "use --model {} if this run fails".format(explicit, VIDEO_REF_MODEL),
                file=sys.stderr,
            )
        return explicit
    if ref_videos:
        print(
            "notice: --ref-video given without --model; using {} "
            "(seedance-2.5 reference video is failing upstream)".format(VIDEO_REF_MODEL),
            file=sys.stderr,
        )
        return VIDEO_REF_MODEL
    return DEFAULT_MODEL


def _validate_duration(model, duration):
    if duration is None:
        return
    if _is_seedance_25(model):
        lo, hi = DURATION_25
        if duration == SMART_DURATION:
            return
        if not (lo <= duration <= hi):
            sys.exit(
                "error: --duration {} is out of range for {} "
                "(must be {}–{}, or -1 for smart duration)".format(
                    duration, _family_label(model), lo, hi
                )
            )
        return
    lo, hi = DURATION_20
    if duration == SMART_DURATION:
        sys.exit(
            "error: --duration -1 (smart duration) is only valid for seedance-2.5"
        )
    if not (lo <= duration <= hi):
        sys.exit(
            "error: --duration {} is out of range for {} "
            "(must be {}–{}; no 1–3s)".format(
                duration, _family_label(model), lo, hi
            )
        )


def _validate_resolution(model, resolution):
    allowed = RESOLUTION_25 if _is_seedance_25(model) else RESOLUTION_20
    if resolution not in allowed:
        sys.exit(
            "error: --resolution {} is not supported for {} (must be {})".format(
                resolution, _family_label(model), ", ".join(allowed)
            )
        )


def _parse_bool(value):
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError("expected true/false, got {}".format(value))


def _reject_v2_params(model, seed, generate_audio, return_last_frame):
    if seed is None and generate_audio is None and return_last_frame is None:
        return
    if _is_seedance_25(model):
        return
    names = []
    if seed is not None:
        names.append("--seed")
    if generate_audio is not None:
        names.append("--generate-audio")
    if return_last_frame is not None:
        names.append("--return-last-frame")
    sys.exit(
        "error: {} only valid for seedance-2.5 (got {})".format(
            ", ".join(names), _family_label(model)
        )
    )


def _build_payload(
    model,
    prompt,
    duration,
    resolution,
    ratio,
    ref_images,
    ref_videos,
    seed=None,
    generate_audio=None,
    return_last_frame=None,
):
    content = [{"type": "text", "text": prompt}]
    for url in ref_images:
        content.append({"type": "image_url", "image_url": {"url": url}})
    for url in ref_videos:
        content.append({"type": "video_url", "video_url": {"url": url}})
    parameters = {"resolution": resolution}
    if duration is not None:
        parameters["duration"] = duration
    if ratio:
        parameters["ratio"] = ratio
    if seed is not None:
        parameters["seed"] = seed
    if generate_audio is not None:
        parameters["generate_audio"] = generate_audio
    if return_last_frame is not None:
        parameters["return_last_frame"] = return_last_frame
    return {
        "model": model,
        "input": {"content": content},
        "parameters": parameters,
    }


def _submit(root, api_key, payload, timeout):
    """POST /v1/tasks/submit; on 404 MODEL_NOT_AVAILABLE swap doubao/↔seedance/ once."""
    url = root + "/v1/tasks/submit"
    headers = {
        "Authorization": "Bearer " + api_key,
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }

    def post(body_obj):
        req = urllib.request.Request(
            url,
            data=json.dumps(body_obj).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            return None, (e.code, _read_http_error(e))
        except urllib.error.URLError as e:
            sys.exit("error: cannot reach gateway: {}".format(e.reason))

    result, err = post(payload)
    if result is not None:
        return result
    code, detail = err
    model = payload.get("model") or ""
    alt = _swapped_namespace(model)
    if code == 404 and alt and _is_model_not_available(detail):
        print(
            "notice: {} returned 404 MODEL_NOT_AVAILABLE; retrying once as {}".format(
                model, alt
            )
        )
        sys.stdout.flush()
        retry = dict(payload)
        retry["model"] = alt
        result, err = post(retry)
        if result is not None:
            return result
        code, detail = err
    sys.exit("error: HTTP {} from gateway\n{}".format(code, detail))


def _timed_out(timeout, last_doc, task_id=None):
    parts = ["error: timed out after {}s".format(timeout)]
    if task_id:
        parts.append(
            "task_id: {} (the task may still finish; check it later)".format(task_id)
        )
    if last_doc is not None:
        parts.append(json.dumps(last_doc, ensure_ascii=False)[:2000])
    sys.exit("\n".join(parts))


def _artifact_url(root, url):
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return root + url
    return url


def _output_paths(output, count):
    if not output:
        output = "nxtpath-seedance-{}.mp4".format(time.strftime("%Y%m%d-%H%M%S"))
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


def _argv_for_parser(argv):
    """Fold `--duration -1` into `--duration=-1` so argparse does not eat it as a flag."""
    out = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--duration" and i + 1 < len(argv) and re.match(r"^-?\d+$", argv[i + 1]):
            out.append("--duration=" + argv[i + 1])
            i += 2
            continue
        out.append(arg)
        i += 1
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Generate a Seedance video via the Nxtpath gateway ark task line."
    )
    parser.add_argument("prompt", help="what to generate, or how to animate a reference")
    parser.add_argument(
        "--ref-image",
        metavar="PATH_OR_URL",
        action="append",
        default=[],
        help="reference image (repeatable); local file inlined as data: or public http(s) URL",
    )
    parser.add_argument(
        "--ref-video",
        metavar="PATH_OR_URL",
        action="append",
        default=[],
        help="reference video (repeatable); local mp4/mov uploaded, or public http(s) URL",
    )
    parser.add_argument(
        "--resolution",
        default="480p",
        help="480p/720p (2.0); 480p/720p/1080p (2.5) (default: %(default)s)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="seconds; 2.0: 4–15; 2.5: 4–30 or -1 (smart); omit for the gateway default",
    )
    parser.add_argument(
        "--ratio",
        default=None,
        help="aspect ratio, e.g. 16:9; omit for the upstream default",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("NXTPATH_SEEDANCE_MODEL") or None,
        help="video model (default: {}; {} when --ref-video is given)".format(
            DEFAULT_MODEL, VIDEO_REF_MODEL
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="seedance-2.5 only; mapped to parameters.seed",
    )
    parser.add_argument(
        "--generate-audio",
        type=_parse_bool,
        default=None,
        metavar="BOOL",
        help="seedance-2.5 only; mapped to parameters.generate_audio (true/false)",
    )
    parser.add_argument(
        "--return-last-frame",
        type=_parse_bool,
        default=None,
        metavar="BOOL",
        help="seedance-2.5 only; mapped to parameters.return_last_frame (true/false)",
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
        help="print the final request-body JSON and exit without uploading or submitting",
    )
    args = parser.parse_args(_argv_for_parser(sys.argv[1:]))

    if args.timeout <= 0:
        sys.exit("error: --timeout must be positive (got {})".format(args.timeout))

    args.model = _pick_model(args.model, args.ref_video)
    _validate_duration(args.model, args.duration)
    _validate_resolution(args.model, args.resolution)
    _reject_v2_params(
        args.model, args.seed, args.generate_audio, args.return_last_frame
    )

    if args.dry_run:
        ref_images = _resolve_ref_images(args.ref_image, True)
        ref_videos = _resolve_ref_videos(args.ref_video, "", 1, True)
        payload = _build_payload(
            args.model,
            args.prompt,
            args.duration,
            args.resolution,
            args.ratio,
            ref_images,
            ref_videos,
            seed=args.seed,
            generate_audio=args.generate_audio,
            return_last_frame=args.return_last_frame,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    root, api_key, source = resolve_credentials()
    print("using key from {}, gateway {}".format(source, root))

    started = time.time()
    left = args.timeout - (time.time() - started)
    if left <= 0:
        _timed_out(args.timeout, None)
    upload_timeout = max(1, min(300, left))
    ref_images = _resolve_ref_images(args.ref_image, False)
    ref_videos = _resolve_ref_videos(args.ref_video, api_key, upload_timeout, False)

    payload = _build_payload(
        args.model,
        args.prompt,
        args.duration,
        args.resolution,
        args.ratio,
        ref_images,
        ref_videos,
        seed=args.seed,
        generate_audio=args.generate_audio,
        return_last_frame=args.return_last_frame,
    )

    submit_timeout = max(1, min(180, args.timeout - (time.time() - started)))
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
    status_doc, poll_output = _poll_until_done(
        poll_url, api_key, args.timeout, started, task_id
    )

    urls = poll_output.get("urls") or []
    if not isinstance(urls, list) or not urls:
        sys.exit(
            "error: Success but no output.urls:\n"
            + json.dumps(status_doc, ensure_ascii=False)[:800]
        )

    paths = _output_paths(args.output, len(urls))
    saved = []
    for url, path in zip(urls, paths):
        data = _download_with_retry(
            _artifact_url(root, url), api_key, args.timeout, started, task_id
        )
        if not data:
            sys.exit("error: downloaded empty video from authenticated handle")
        saved.append((_save(data, path), len(data)))

    usage = status_doc.get("usage") if isinstance(status_doc.get("usage"), dict) else {}
    usage_duration = usage.get("duration")
    total_tokens = usage.get("total_tokens")
    elapsed = time.time() - started
    if len(saved) == 1:
        path, nbytes = saved[0]
        print(
            "saved: {} ({} bytes, usage duration {}, total_tokens {}, {:.0f}s total)".format(
                path, nbytes, usage_duration, total_tokens, elapsed
            )
        )
    else:
        for path, nbytes in saved:
            print("saved: {} ({} bytes)".format(path, nbytes))
        print(
            "usage duration {}, total_tokens {}, {:.0f}s total".format(
                usage_duration, total_tokens, elapsed
            )
        )


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()
