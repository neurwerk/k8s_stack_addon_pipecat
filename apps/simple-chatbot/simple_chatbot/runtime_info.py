"""Sanitized deployment labels and best-effort, bounded runtime health metadata."""

import asyncio
import ipaddress
import json
import os
import re
import socket
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from simple_chatbot.activity import Activity
from simple_chatbot.config import Settings


def hostname(url: str) -> str:
    return (urlsplit(url).hostname or "Unknown")[:253]


def hosting(host: str) -> str:
    if host.endswith((".svc", ".svc.cluster.local")):
        return "Our Kubernetes"
    try:
        address = ipaddress.ip_address(host)
        if address.is_private and not address.is_loopback and not address.is_link_local:
            return "Our private server"
    except ValueError:
        pass
    # A public address or arbitrary DNS name does not establish ownership.
    return "Remote endpoint"


def onnx_session_metadata(providers: list[str]) -> dict[str, str]:
    """Describe providers attached to a loaded model, not globally available devices."""
    gpu_providers = {
        "CUDAExecutionProvider",
        "ROCMExecutionProvider",
        "TensorrtExecutionProvider",
        "CoreMLExecutionProvider",
        "DmlExecutionProvider",
        "MIGraphXExecutionProvider",
    }
    compute = "Unknown"
    if providers == ["CPUExecutionProvider"]:
        compute = "CPU"
    elif any(provider in gpu_providers for provider in providers):
        # A session can still run some operations on the CPU fallback provider.
        compute = "GPU-capable"
    return {
        "compute": compute,
        "evidence": "ONNX session providers: " + ", ".join(providers)[:210],
    }


def configured_models(settings: Settings) -> dict[str, dict]:
    location = "Our Kubernetes" if os.environ.get("KUBERNETES_SERVICE_HOST") else "App host"
    host = os.environ.get("NODE_NAME") or socket.gethostname()
    detail = "Node" if os.environ.get("NODE_NAME") else "App host / pod"
    local = dict(hosting=location, host=host[:253], compute="Unknown", evidence=detail)
    models = {
        "vad": dict(local, model="Silero VAD"),
        "turn": dict(local, model="Smart Turn v3.2"),
    }
    for stage, speech in (("stt", settings.stt), ("tts", settings.tts)):
        if speech:
            host = hostname(speech.base_url)
            models[stage] = dict(
                model=speech.model,
                hosting=hosting(host),
                host=host,
                compute="Unknown",
                evidence="Configured endpoint",
            )
    if settings.diarization:
        host = hostname(settings.diarization.url)
        models["diarization"] = dict(
            model="Nemotron",
            hosting=hosting(host),
            host=host,
            compute="Unknown",
            evidence="Configured endpoint",
        )
    # The platform's canonical remote/<provider>/<model> namespace identifies
    # the selected route; it does not claim knowledge of the provider's hardware.
    route = re.fullmatch(r"remote/([a-zA-Z0-9_.-]+)/(.+)", settings.model_id)
    models["llm"] = dict(
        model=settings.model_id,
        hosting="Third-party inference" if route else "Gateway route",
        host=route[1] if route else hostname(settings.gateway_url),
        compute="Unknown",
        evidence="Configured model route",
        via=hostname(settings.gateway_url),
    )
    return models


async def health_metadata(http: httpx.AsyncClient, endpoint: str) -> dict:
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ""
    authority = f"[{host}]" if ":" in host else host
    if parsed.port:
        authority += f":{parsed.port}"
    url = urlunsplit(
        ("https" if parsed.scheme in ("https", "wss") else "http", authority, "/health", "", "")
    )
    try:
        async with asyncio.timeout(3):
            # A fresh Request does not inherit client cookies, headers or credentials.
            response = await http.send(
                httpx.Request("GET", url), stream=True, auth=None, follow_redirects=False
            )
            try:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 16384:
                        return {}
                value = json.loads(data)
                if not isinstance(value, dict):
                    return {}
                device = value.get("device")
                if device in ("cpu", "cuda", "gpu", "mps"):
                    return {
                        "compute": "CPU" if device == "cpu" else "GPU",
                        "evidence": "Reported by /health",
                        "checked_at": int(time.time()),
                    }
            finally:
                await response.aclose()
    except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
        pass
    return {}


async def refresh_models(
    activity: Activity, settings: Settings, local_models: dict[str, dict]
) -> None:
    baseline = configured_models(settings)
    baseline.update(local_models)
    activity.models = {key: dict(value) for key, value in baseline.items()}
    endpoints = {
        stage: speech.base_url
        for stage, speech in (("stt", settings.stt), ("tts", settings.tts))
        if speech
    }
    if settings.diarization:
        endpoints["diarization"] = settings.diarization.url
    async with httpx.AsyncClient(trust_env=False, timeout=2) as http:
        while True:
            results = await asyncio.gather(
                *(health_metadata(http, url) for url in endpoints.values())
            )
            for stage, result in zip(endpoints, results, strict=True):
                # Never retain a stale GPU claim after a failed refresh.
                activity.models[stage] = {**baseline[stage], **result}
            await asyncio.sleep(60)
