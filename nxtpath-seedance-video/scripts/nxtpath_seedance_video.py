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
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://api.nxtpath.ai"
DEFAULT_MODEL = "doubao/seedance-2.0"
# Video generation is slow; timeout covers submit + poll + download.
DEFAULT_TIMEOUT = 900
POLL_INTERVAL = 5
USER_AGENT = "nxtpath-seedance-video-skill/1.0"

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


def _is_seedance_25(model):
    return "2.5" in (model or "").lower()


def _family_label(model):
    name = (model or "").lower()
    label = "seedance-2.5" if "2.5" in name else "seedance-2.0"
    if "mini" in name:
        label += "-mini"
    return label


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


def _is_public_url(value):
    lower = value.strip().lower()
    return lower.startswith("http://") or lower.startswith("https://")


_REF_IMAGE_LOCAL_ERROR = (
    "error: --ref-image 本地文件暂不可用，ark 线参考素材目前只收公网 URL；"
    "把图片放到可公网访问的地址再传 URL；"
    "网关字节通道支持在路上（router #2186），落地后本 skill 会跟进"
)


def _require_public_urls(flag, values):
    urls = []
    for value in values:
        item = value.strip()
        if not item:
            sys.exit("error: empty {} value".format(flag))
        if not _is_public_url(item):
            if flag == "--ref-image":
                sys.exit(_REF_IMAGE_LOCAL_ERROR)
            sys.exit(
                "error: {} requires a public URL (http:// or https://); "
                "local file paths are not uploaded".format(flag)
            )
        urls.append(item)
    return urls


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
    parser.add_argument("prompt", help="what to generate, or how to animate a reference URL")
    parser.add_argument(
        "--ref-image",
        metavar="URL",
        action="append",
        default=[],
        help="public reference image URL (repeatable); local paths are rejected",
    )
    parser.add_argument(
        "--ref-video",
        metavar="URL",
        action="append",
        default=[],
        help="public reference video URL (repeatable); local paths are rejected",
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
        default=os.environ.get("NXTPATH_SEEDANCE_MODEL", DEFAULT_MODEL),
        help="video model (default: %(default)s)",
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
    args = parser.parse_args(_argv_for_parser(sys.argv[1:]))

    if args.timeout <= 0:
        sys.exit("error: --timeout must be positive (got {})".format(args.timeout))

    _validate_duration(args.model, args.duration)
    _validate_resolution(args.model, args.resolution)
    _reject_v2_params(
        args.model, args.seed, args.generate_audio, args.return_last_frame
    )
    ref_images = _require_public_urls("--ref-image", args.ref_image)
    ref_videos = _require_public_urls("--ref-video", args.ref_video)

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
