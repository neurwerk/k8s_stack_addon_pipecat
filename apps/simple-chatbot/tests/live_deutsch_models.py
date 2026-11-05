"""Opt-in German TTS -> WAV -> STT -> model -> TTS service smoke check; never run in CI.

Uses an already running bot pod and its existing speech configuration. An operator
supplies a personal gateway key in an ignored .env and an explicit cluster context.
Only synthetic input WAVs and timing summaries leave the pod; no text or secrets.
This bypasses Pipecat turn handling, sentence streaming, diarization, and browser
audio; it does not assess language quality or conversational context retention.
"""

import argparse
import base64
import io
import json
import subprocess
import tempfile
import wave
from pathlib import Path

# This fixed code runs in the existing bot container, where the pinned runtime
# dependencies and speech credentials are already available. No files are written.
IN_POD = r"""
import asyncio
import audioop
import base64
import io
import json
import re
import sys
import time
import wave

import httpx

from simple_chatbot.config import load_settings
from simple_chatbot.notices import NoticeSplitter, SpeechControlFilter
from simple_chatbot.services import bounded_pcm, bounded_sse, speech_headers, speech_text

PROMPTS = (
    ("deutsch_kurz.wav", "Erzähl mir kurz etwas über Elefanten."),
    ("deutsch_nachfrage.wav", "Erzähl mir mehr."),
    (
        "deutsch_elefantengeschichte.wav",
        "Erzähl mir eine Geschichte über Elefanten, in der eine Elefantenfamilie "
        "auf einer langen Reise ein kleines Elefantenkind wiederfindet.",
    ),
)
SYSTEM = (
    "Du bist ein freundlicher Sprachassistent. Antworte ausschließlich auf Deutsch, "
    "kurz und ohne Formatierung. Beginne direkt mit der Antwort, ohne 'Gern'. "
    "Behalte den Gesprächskontext für Nachfragen."
)
stage = "configuration"


async def synthesize(http, settings, text, seconds):
    started = time.monotonic()
    first_ms = None
    chunks = []
    size = 0
    async with http.stream(
        "POST", settings.base_url.rstrip("/") + "/audio/speech",
        headers=speech_headers(settings),
        json={"model": settings.model, "voice": settings.voice,
              "input": text, "response_format": "pcm"},
    ) as response:
        response.raise_for_status()
        async for chunk in bounded_pcm(response):
            if first_ms is None:
                first_ms = round((time.monotonic() - started) * 1000)
            size += len(chunk)
            if size > 24000 * 2 * seconds:
                raise ValueError("Audio duration exceeded the test limit")
            chunks.append(chunk)
    if not size:
        raise ValueError("No speech audio")
    return b"".join(chunks), first_ms


def wav_from_pcm(pcm):
    sample, _ = audioop.ratecv(pcm, 2, 1, 24000, 16000, None)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(sample)
    return output.getvalue()


async def transcribe(http, settings, audio):
    started = time.monotonic()
    async with http.stream(
        "POST", settings.base_url.rstrip("/") + "/audio/transcriptions",
        headers=speech_headers(settings),
        files={"file": ("deutsch_sprache.wav", audio, "audio/wav")},
        data={"model": settings.model, "language": "de", "response_format": "json"},
    ) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=4096):
            body.extend(chunk)
            if len(body) > 65536:
                raise ValueError("Transcript too large")
    text = json.loads(body)["text"]
    if not isinstance(text, str) or not 1 <= len(text) <= 8192:
        raise ValueError("No valid transcript")
    return text, round((time.monotonic() - started) * 1000)


async def reply(http, settings, key, history):
    started = time.monotonic()
    first_ms = None
    size = 0
    parts = []
    splitter = NoticeSplitter()
    speech = SpeechControlFilter()
    async with http.stream(
        "POST", settings.gateway_url, follow_redirects=False,
        headers={"Authorization": "Bearer " + key},
        json={"model": settings.model_id, "messages": history,
              "stream": True, "max_tokens": 512},
    ) as response:
        response.raise_for_status()
        async for event in bounded_sse(response):
            if event.get("error"):
                raise ValueError("Model refused reply")
            for choice in event.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                text = choice.get("delta", {}).get("content")
                if not isinstance(text, str):
                    continue
                size += len(text)
                if size > 12000:
                    raise ValueError("Reply too long")
                clean = speech.feed(splitter.feed(text))
                if clean.strip() and first_ms is None:
                    first_ms = round((time.monotonic() - started) * 1000)
                parts.append(clean)
    text = "".join(parts).strip()
    if len(text) < 10:
        raise ValueError("No spoken model reply")
    if not re.search(
        r"\b(ich|du|sie|wir|und|der|die|das|ist|sind|eine|einem|einen|"
        r"elefanten|haben|leben|ihre|ihnen|bei|mit|für|auf)\b",
        text.casefold(),
    ):
        raise ValueError("Model reply does not look German")
    return text, first_ms


async def run():
    global stage
    request = json.load(sys.stdin)
    key = request["api_key"]
    settings = load_settings()
    if not settings.stt or not settings.tts or settings.stt.language != "de":
        raise ValueError("German speech configuration unavailable")
    history = [{"role": "system", "content": SYSTEM}]
    results = []
    async with asyncio.timeout(210):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(35, connect=10), follow_redirects=False,
            trust_env=False,
        ) as http:
            for index, (filename, prompt) in enumerate(PROMPTS[:request["turns"]]):
                stage = f"input_tts_{index + 1}"
                input_pcm, input_first_ms = await synthesize(http, settings.tts, prompt, 20)
                audio = wav_from_pcm(input_pcm)
                stage = f"stt_{index + 1}"
                transcript, stt_ms = await transcribe(http, settings.stt, audio)
                expected = "mehr" if index == 1 else "elefant"
                if expected not in transcript.casefold():
                    raise ValueError("German transcription did not match the prompt")
                history.append({"role": "user", "content": transcript})
                stage = f"model_{index + 1}"
                answer, llm_first_ms = await reply(http, settings, key, history)
                if index == 2 and len(re.findall(r"[.!?]", answer)) < 2:
                    raise ValueError("Story reply was too short")
                history.append({"role": "assistant", "content": answer})
                stage = f"output_tts_{index + 1}"
                output_pcm, output_first_ms = await synthesize(
                    http, settings.tts, await speech_text(answer, "sentence"), 45,
                )
                results.append({
                    "filename": filename, "wav": base64.b64encode(audio).decode("ascii"),
                    "input_tts_first_ms": input_first_ms, "stt_ms": stt_ms,
                    "llm_first_ms": llm_first_ms, "output_tts_first_ms": output_first_ms,
                    "output_audio_seconds": round(len(output_pcm) / 48000, 2),
                })
    return results


try:
    print(json.dumps({"turns": asyncio.run(run())}))
except httpx.HTTPStatusError as error:
    print(json.dumps({"error": "Service rejected test request", "stage": stage,
                      "status": error.response.status_code}))
except Exception as error:
    kind = type(error).__name__
    if kind not in {"ValueError", "ReadTimeout", "ConnectTimeout", "ReadError", "ConnectError"}:
        kind = "other"
    detail = {
        "Audio duration exceeded the test limit": "audio_duration_limit",
        "No spoken model reply": "empty_model_reply",
        "Model reply does not look German": "not_german",
        "Model refused reply": "model_refused",
        "Reply too long": "reply_limit",
        "Incomplete gateway stream": "incomplete_gateway_stream",
        "Incomplete gateway completion": "incomplete_gateway_completion",
        "Unsuccessful gateway completion": "unfinished_gateway_completion",
    }.get(str(error), "")
    print(json.dumps({"error": "Live model test failed", "stage": stage,
                      "kind": kind, "detail": detail}))
"""


def gateway_key(path: Path) -> str:
    entries = [
        line.partition("=")[2].strip()
        for line in path.read_text().splitlines()
        if line.startswith("NEURWERK_API_KEY=")
    ]
    if len(entries) != 1 or not entries[0]:
        raise ValueError("Expected one NEURWERK_API_KEY in the ignored env file")
    value = entries[0]
    if value[0] in ("'", '"'):
        if len(value) < 3 or value[-1] != value[0]:
            raise ValueError("Expected a nonempty, consistently quoted API key")
        value = value[1:-1]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--turns", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Parent directory for temporary synthetic WAVs, removed after the check",
    )
    args = parser.parse_args()
    try:
        key = gateway_key(args.env_file)
        command = [
            "kubectl",
            "--context",
            args.context,
            "-n",
            args.namespace,
            "exec",
            "-i",
            "deployment/" + args.deployment,
            "-c",
            "bot",
            "--",
            "env",
            "PYTHONPATH=/scripts/bundle",
            "/tmp/app-venv/bin/python",
            "-c",
            IN_POD,
        ]
        process = subprocess.run(
            command,
            input=json.dumps({"api_key": key, "turns": args.turns}),
            text=True,
            capture_output=True,
            timeout=240,
            check=False,
        )
        # Do not expose Kubernetes stderr, provider responses, or credentials.
        if process.returncode:
            raise ValueError("Pod execution failed")
        result = json.loads(process.stdout)
        if "error" in result:
            stage = result.get("stage")
            status = result.get("status")
            kind = result.get("kind")
            detail = result.get("detail")
            if stage == "configuration" or (
                isinstance(stage, str)
                and stage
                in {
                    f"{step}_{turn}"
                    for step in ("input_tts", "stt", "model", "output_tts")
                    for turn in range(1, 4)
                }
            ):
                print(
                    f"German live model test failed at {stage}"
                    + (f" (HTTP {status})" if isinstance(status, int) else "")
                    + (
                        f" ({kind})"
                        if kind
                        in {
                            "ValueError",
                            "ReadTimeout",
                            "ConnectTimeout",
                            "ReadError",
                            "ConnectError",
                        }
                        else ""
                    )
                    + (
                        f" ({detail})"
                        if detail
                        in {
                            "audio_duration_limit",
                            "empty_model_reply",
                            "not_german",
                            "model_refused",
                            "reply_limit",
                            "incomplete_gateway_stream",
                            "incomplete_gateway_completion",
                            "unfinished_gateway_completion",
                        }
                        else ""
                    )
                )
            else:
                print("German live model test failed (no credentials or transcripts printed).")
            return 1
        with tempfile.TemporaryDirectory(
            prefix="deutsch-modelltest-", dir=args.output_dir
        ) as folder:
            for turn in result["turns"]:
                audio = base64.b64decode(turn.pop("wav"), validate=True)
                if len(audio) > 16000 * 2 * 22:
                    raise ValueError("Input WAV exceeded test limit")
                path = Path(folder) / turn["filename"]
                with wave.open(io.BytesIO(audio)) as wav:
                    if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (
                        1,
                        2,
                        16000,
                    ):
                        raise ValueError("Input WAV has unexpected audio format")
                path.write_bytes(audio)
                print(json.dumps(turn, ensure_ascii=False))
        print(f"{args.turns} German model turn(s) passed; temporary WAVs were removed.")
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired, KeyError, TypeError):
        print("German live model test failed (no credentials or transcripts printed).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
