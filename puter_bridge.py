#!/usr/bin/env python3
"""OpenAI-style local bridge backed by Puter User-Pays AI drivers.

Auth: PUTER_AUTH_TOKEN from ~/.config/novamaster/puter.env.
Endpoints:
- /health
- /v1/models
- /v1/chat/completions
- /v1/images/generations
- /v1/audio/speech
- /v1/videos/generations      (puter-video-generation: sora-2/veo-3 via Puter)
- /v1/audio/transcriptions    (puter-speech2txt: xAI STT via Puter)
- /v1/images/describe         (puter-ocr: vision/OCR via Puter)
"""

from __future__ import annotations

import base64
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


VERSION = "v2-puter-grok"
ENV_PATH = os.path.expanduser("~/.config/novamaster/puter.env")
PUTER_API = "https://api.puter.com/drivers/call"
DEFAULT_MODEL = os.environ.get("PUTER_DEFAULT_MODEL", "x-ai/grok-4.3")
DEFAULT_IMAGE_MODEL = os.environ.get("PUTER_DEFAULT_IMAGE_MODEL", "gpt-image-1.5")
REASONING_MIN_MAX_TOKENS = int(os.environ.get("PUTER_REASONING_MIN_MAX_TOKENS", "256"))

GROK_TEXT_MODELS = [
    "x-ai/grok-build-0.1",
    "x-ai/grok-4.3",
    "x-ai/grok-4.20",
    "x-ai/grok-4.20-multi-agent",
    "x-ai/grok-4-1-fast",
    "x-ai/grok-4-1-fast-non-reasoning",
    "x-ai/grok-code-fast-1",
    "x-ai/grok-4",
    "x-ai/grok-4-fast",
    "x-ai/grok-4-fast-non-reasoning",
    "x-ai/grok-4-0709",
    "x-ai/grok-3",
    "x-ai/grok-3-fast",
    "x-ai/grok-3-mini",
    "x-ai/grok-3-mini-fast",
    "x-ai/grok-2-vision-1212",
    "x-ai/grok-beta",
    "x-ai/grok-vision-beta",
    "x-ai/grok-2",
    "x-ai/grok-2-vision",
]

GENERAL_TEXT_MODELS = [
    "gpt-4o-mini",
    "gpt-4o",
    "claude-haiku-4-5-20251001",
    "claude-3-7-sonnet-20250219",
    "gemini-2.5-flash",
]

ZAI_TEXT_MODELS = [
    "z-ai/glm-5.2",
    "z-ai/glm-5.1",
    "z-ai/glm-5v-turbo",
    "z-ai/glm-5-turbo",
    "z-ai/glm-5",
    "z-ai/glm-4.7",
    "z-ai/glm-4.7-flash",
    "z-ai/glm-4.7-flashx",
    "z-ai/glm-4.6v",
    "z-ai/glm-4.6v-flash",
    "z-ai/glm-4.6v-flashx",
    "z-ai/glm-4.6",
    "z-ai/glm-4.5",
    "z-ai/glm-4.5-air",
    "z-ai/glm-4.5-airx",
    "z-ai/glm-4.5-flash",
    "z-ai/glm-4.5-x",
    "z-ai/glm-4.5v",
    "z-ai/glm-4-32b",
    "z-ai/glm-4-32b-0414-128k",
    "z-ai/autoglm-phone-multilingual",
]

IMAGE_MODELS = [
    "grok-2-image",
    "grok-image",
    "gpt-image-2",
    "gpt-image-1.5",
    "gpt-image-1",
    "gpt-image-1-mini",
    "dall-e-3",
    "nano-banana",
    "gemini-2.5-flash-image-preview",
    "flux-schnell",
]

MODEL_ALIASES = {
    "supergrok": "x-ai/grok-4.3",
    "supergrok-zero": "x-ai/grok-4.3",
    "grok": "x-ai/grok-4.3",
    "grok-browser": "x-ai/grok-4.3",
    "grok-4.3": "x-ai/grok-4.3",
    "grok43": "x-ai/grok-4.3",
    "grok-fast": "x-ai/grok-4-1-fast",
    "grok-code": "x-ai/grok-build-0.1",
    "grok-build": "x-ai/grok-build-0.1",
    "glm": "z-ai/glm-5.2",
    "glm-5.2": "z-ai/glm-5.2",
    "glm5.2": "z-ai/glm-5.2",
    "glm52": "z-ai/glm-5.2",
    "glm-5-2": "z-ai/glm-5.2",
    "zai-org-glm-5-2": "z-ai/glm-5.2",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
    "glm-5.1": "z-ai/glm-5.1",
    "glm5.1": "z-ai/glm-5.1",
    "glm-5v": "z-ai/glm-5v-turbo",
    "glm-5v-turbo": "z-ai/glm-5v-turbo",
    "glm-5-turbo": "z-ai/glm-5-turbo",
}

IMAGE_ALIASES = {
    "grok-image": "grok-2-image",
    "x-ai/grok-2-image": "grok-2-image",
    "nano-banana": "gemini-2.5-flash-image-preview",
}


def _load_token() -> str | None:
    token = os.environ.get("PUTER_AUTH_TOKEN")
    if token:
        return token
    try:
        with open(ENV_PATH, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PUTER_AUTH_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return None


TOKEN = _load_token()


def _redact_error(text: str) -> str:
    """Keep upstream diagnostics useful without leaking long bearer-like values."""
    return text.replace(TOKEN or "", "[redacted]")[:900]


def _puter_call(
    interface: str,
    method: str,
    args: dict[str, Any],
    *,
    driver: str | None = None,
    test_mode: bool | None = None,
    timeout: int = 120,
    raw_response: bool = False,
) -> tuple[Any, str | None, dict[str, str]]:
    payload: dict[str, Any] = {
        "interface": interface,
        "method": method,
        "args": args,
    }
    if driver:
        payload["driver"] = driver
    if test_mode is not None:
        payload["test_mode"] = bool(test_mode)

    request = urllib.request.Request(
        PUTER_API,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Origin": "https://puter.com",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        return None, f"HTTP {exc.code}: {_redact_error(body)}", {}
    except Exception as exc:
        return None, f"{type(exc).__name__}: {_redact_error(str(exc))}", {}

    if raw_response:
        return body, None, headers

    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return body, None, headers

    if isinstance(data, dict) and data.get("success") is False:
        return None, _redact_error(json.dumps(data, ensure_ascii=True)), headers
    if isinstance(data, dict) and "error" in data and not data.get("success"):
        return None, _redact_error(json.dumps(data, ensure_ascii=True)), headers
    if isinstance(data, dict) and "result" in data:
        return data.get("result"), None, headers
    return data, None, headers


def _resolve_model(model: Any) -> str:
    if isinstance(model, str) and model.strip():
        clean = model.strip()
        return MODEL_ALIASES.get(clean, clean)
    return DEFAULT_MODEL


def _resolve_image_model(model: Any) -> str:
    if isinstance(model, str) and model.strip():
        clean = model.strip()
        return IMAGE_ALIASES.get(clean, clean)
    return DEFAULT_IMAGE_MODEL


def _chat_driver_for(model: str) -> str | None:
    if model.startswith("x-ai/"):
        return "ai-chat"
    if model.startswith("z-ai/"):
        return "ai-chat"
    if model.startswith("openrouter:"):
        return "openrouter"
    return None


def _is_reasoning_model(model: str) -> bool:
    return model.startswith("z-ai/glm-5") or model.startswith("z-ai/glm-4.7")


def _image_driver_for(model: str, provider: str | None) -> str:
    if provider == "xai" or model == "grok-2-image":
        return "xai-image-generation"
    if provider == "gemini" or model == "gemini-2.5-flash-image-preview":
        return "gemini-image-generation"
    if provider == "together":
        return "together-image-generation"
    if provider == "replicate-image-generation":
        return "replicate-image-generation"
    return "openai-image-generation"


def _normalize_chat_message(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        message = result.get("message")
        if isinstance(message, dict):
            return {
                "role": message.get("role", "assistant"),
                "content": message.get("content", ""),
                **({"tool_calls": message["tool_calls"]} if "tool_calls" in message else {}),
            }
        if isinstance(result.get("content"), str):
            return {"role": "assistant", "content": result["content"]}
        if isinstance(result.get("text"), str):
            return {"role": "assistant", "content": result["text"]}
    if isinstance(result, str):
        return {"role": "assistant", "content": result}
    return {"role": "assistant", "content": json.dumps(result, ensure_ascii=True)}


def _puter_complete(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    model = _resolve_model(body.get("model"))
    args: dict[str, Any] = {
        "messages": body.get("messages", []),
        "model": model,
    }
    for key in (
        "temperature",
        "max_tokens",
        "top_p",
        "tools",
        "tool_choice",
        "response_format",
        "stop",
        "seed",
    ):
        if key in body and body[key] is not None:
            args[key] = body[key]
    if _is_reasoning_model(model):
        current_max = args.get("max_tokens")
        if current_max is None:
            args["max_tokens"] = REASONING_MIN_MAX_TOKENS
        else:
            try:
                if int(current_max) < REASONING_MIN_MAX_TOKENS:
                    args["max_tokens"] = REASONING_MIN_MAX_TOKENS
            except (TypeError, ValueError):
                args["max_tokens"] = REASONING_MIN_MAX_TOKENS

    result, error, _ = _puter_call(
        "puter-chat-completion",
        "complete",
        args,
        driver=_chat_driver_for(model),
        timeout=180,
    )
    if error:
        return None, error
    return _normalize_chat_message(result), None


def _image_result_item(value: Any, response_format: str) -> dict[str, Any]:
    if isinstance(value, dict):
        if response_format == "b64_json" and isinstance(value.get("url"), str):
            url = value["url"]
            if url.startswith("data:") and "," in url:
                return {"b64_json": url.split(",", 1)[1]}
        return value
    if isinstance(value, str):
        if response_format == "b64_json":
            if value.startswith("data:") and "," in value:
                return {"b64_json": value.split(",", 1)[1]}
            try:
                request = urllib.request.Request(
                    value,
                    headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"},
                )
                decoded = urllib.request.urlopen(request, timeout=30).read()
                return {"b64_json": base64.b64encode(decoded).decode("ascii")}
            except Exception:
                return {"url": value}
        return {"url": value}
    return {"text": json.dumps(value, ensure_ascii=True)}


def _puter_image(body: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None, "missing prompt"
    model = _resolve_image_model(body.get("model"))
    provider = body.get("provider")
    if not isinstance(provider, str):
        provider = None

    args: dict[str, Any] = {"prompt": prompt, "model": model}
    for key in (
        "quality",
        "size",
        "n",
        "width",
        "height",
        "aspect_ratio",
        "ratio",
        "negative_prompt",
        "seed",
        "response_format",
        "input_image",
        "input_images",
        "image_url",
        "image_base64",
    ):
        if key in body and body[key] is not None:
            args[key] = body[key]
    if provider:
        args["provider"] = provider

    result, error, _ = _puter_call(
        "puter-image-generation",
        "generate",
        args,
        driver=_image_driver_for(model, provider),
        test_mode=body.get("test_mode") if "test_mode" in body else None,
        timeout=240,
    )
    if error:
        return None, error

    response_format = body.get("response_format", "url")
    if response_format not in ("url", "b64_json"):
        response_format = "url"
    values = result if isinstance(result, list) else [result]
    return [_image_result_item(value, response_format) for value in values], None


def _puter_video(body: dict[str, Any]) -> tuple[str | None, str | None]:
    """Video generation via puter-video-generation. Returns a downloadable URL."""
    prompt = body.get("prompt") or body.get("input")
    if not isinstance(prompt, str) or not prompt.strip():
        return None, "missing prompt"
    args: dict[str, Any] = {
        "prompt": prompt,
        "model": body.get("model", "sora-2"),
        "seconds": body.get("seconds", 5),
    }
    if body.get("size"):
        args["size"] = body["size"]
    if body.get("aspect_ratio"):
        args["aspect_ratio"] = body["aspect_ratio"]
    if body.get("test_mode") is not None:
        args["test_mode"] = bool(body["test_mode"])

    result, error, _ = _puter_call(
        "puter-video-generation",
        "generate",
        args,
        driver="ai-video",
        test_mode=args.get("test_mode"),
        timeout=300,
    )
    if error:
        return None, error
    if isinstance(result, dict):
        url = result.get("url") or result.get("src") or (result.get("result") if isinstance(result.get("result"), str) else None)
    elif isinstance(result, str):
        url = result
    else:
        return None, f"unexpected video response: {str(result)[:200]}"
    if not url:
        return None, "video result had no url"
    return url, None


def _puter_transcribe(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Speech-to-text via puter-speech2txt. Accepts audio as data-URI/base64 in `audio` or `file`."""
    source = body.get("audio") or body.get("file") or body.get("input")
    if not source:
        return None, "missing audio (send data URI / base64 in 'audio')"
    args: dict[str, Any] = {"file": source}
    for key in ("provider", "language", "format", "translate"):
        if body.get(key) is not None:
            args[key] = body[key]
    if body.get("test_mode") is not None:
        args["test_mode"] = bool(body["test_mode"])

    result, error, _ = _puter_call(
        "puter-speech2txt",
        "transcribe",
        args,
        driver="ai-speech2txt",
        test_mode=args.get("test_mode"),
        timeout=180,
    )
    if error:
        return None, error
    if isinstance(result, dict):
        return result, None
    return {"text": str(result)}, None


def _puter_describe(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Vision/OCR via puter-ocr. Accepts an image as data-URI/base64 in `image` or `source`."""
    source = body.get("image") or body.get("source") or body.get("input")
    if not source:
        return None, "missing image (send data URI / base64 in 'image')"
    args: dict[str, Any] = {"source": source}
    # NB: de ai-ocr driver accepteert GEEN provider-arg (xai -> 502).
    # Alleen language doorgeven; provider wordt genegeerd.
    if body.get("language") is not None:
        args["language"] = body["language"]
    if body.get("test_mode") is not None:
        args["test_mode"] = bool(body["test_mode"])

    result, error, _ = _puter_call(
        "puter-ocr",
        "recognize",
        args,
        driver="ai-ocr",
        test_mode=args.get("test_mode"),
        timeout=180,
    )
    if error:
        return None, error
    if isinstance(result, dict):
        return result, None
    return {"text": str(result)}, None


def _puter_speech(body: dict[str, Any]) -> tuple[bytes | None, str | None, str]:
    text = body.get("input") or body.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, "missing input", "audio/mpeg"
    args: dict[str, Any] = {
        "text": text,
        "voice": body.get("voice", "eve"),
        "output_format": body.get("response_format", "mp3"),
    }
    provider = body.get("provider")
    if isinstance(provider, str) and provider:
        args["provider"] = provider
    elif isinstance(body.get("model"), str) and body["model"].startswith("x-ai/"):
        args["provider"] = "xai"
    for key in ("model", "language", "engine", "speed", "ssml"):
        if key in body and body[key] is not None:
            args[key] = body[key]

    # xAI-voices (eve/ara/rex/sal/leo) draaien op de ai-tts driver; Polly-voices (Lotte/Maxim) ook.
    # De oude "aws-polly" driver-diskriminator accepteert geen xai-voices — dus default naar ai-tts.
    driver = body.get("driver")
    if not isinstance(driver, str) or not driver:
        driver = "ai-tts" if args.get("provider") in ("xai", "x-ai") or (isinstance(body.get("model"), str) and body["model"].startswith("x-ai/")) else "ai-tts"
    result, error, headers = _puter_call(
        "puter-tts",
        "synthesize",
        args,
        driver=driver,
        test_mode=body.get("test_mode") if "test_mode" in body else None,
        timeout=120,
        raw_response=True,
    )
    if error:
        return None, error, "audio/mpeg"
    content_type = headers.get("content-type", "audio/mpeg")
    if not isinstance(result, bytes):
        return None, "unexpected speech response", content_type
    return result, None, content_type


class _Handler(BaseHTTPRequestHandler):
    server_version = "PuterBridge/2"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.address_string(), fmt % args))

    def _json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _send_chat_stream(self, model: str, message: dict[str, Any]) -> None:
        content = message.get("content") if isinstance(message.get("content"), str) else ""
        stream_id = f"chatcmpl-puter-{int(time.time())}"
        chunks = [
            {
                "id": stream_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}],
            },
            {
                "id": stream_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")

    def do_GET(self) -> None:
        if self.path == "/health":
            return self._json(
                200,
                {
                    "status": "ok" if TOKEN else "missing_token",
                    "version": VERSION,
                    "token": bool(TOKEN),
                    "default_model": DEFAULT_MODEL,
                    "capabilities": ["chat", "chat_stream_single_chunk", "images", "audio_speech", "video", "speech2txt", "ocr"],
                },
            )
        if self.path == "/v1/models":
            models = GROK_TEXT_MODELS + GENERAL_TEXT_MODELS + ZAI_TEXT_MODELS + IMAGE_MODELS
            return self._json(
                200,
                {
                    "object": "list",
                    "data": [{"id": model, "object": "model", "owned_by": "puter"} for model in models],
                },
            )
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not TOKEN:
            return self._json(503, {"error": "missing PUTER_AUTH_TOKEN"})
        body = self._read_json()
        if body is None:
            return self._json(400, {"error": "invalid json"})

        if self.path in ("/v1/chat/completions", "/chat/completions"):
            messages = body.get("messages", [])
            if not isinstance(messages, list) or not messages:
                return self._json(400, {"error": "missing messages"})
            message, error = _puter_complete(body)
            if error:
                return self._json(502, {"error": error})
            model = _resolve_model(body.get("model"))
            if body.get("stream"):
                return self._send_chat_stream(model, message or {"role": "assistant", "content": ""})
            return self._json(
                200,
                {
                    "id": f"chatcmpl-puter-{int(time.time())}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                },
            )

        if self.path == "/v1/images/generations":
            items, error = _puter_image(body)
            if error:
                return self._json(502, {"error": error})
            return self._json(200, {"created": int(time.time()), "data": items or []})

        if self.path == "/v1/audio/speech":
            audio, error, content_type = _puter_speech(body)
            if error:
                return self._json(502, {"error": error})
            return self._bytes(200, audio or b"", content_type)

        if self.path == "/v1/videos/generations":
            url, error = _puter_video(body)
            if error:
                return self._json(502, {"error": error})
            return self._json(200, {"created": int(time.time()), "data": [{"url": url}]})

        if self.path == "/v1/audio/transcriptions":
            result, error = _puter_transcribe(body)
            if error:
                return self._json(502, {"error": error})
            return self._json(200, result or {})

        if self.path == "/v1/images/describe":
            result, error = _puter_describe(body)
            if error:
                return self._json(502, {"error": error})
            return self._json(200, result or {})

        return self._json(404, {"error": "not found"})


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19998)
    args = parser.parse_args()

    if not TOKEN:
        print("[FATAL] no PUTER_AUTH_TOKEN; run puter_google_login.py first", file=sys.stderr)
        sys.exit(1)

    def _shutdown(*_: Any) -> None:
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    HTTPServer.allow_reuse_address = True
    server = HTTPServer((args.host, args.port), _Handler)
    print(f"puter-bridge {VERSION} listening on {args.host}:{args.port}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
