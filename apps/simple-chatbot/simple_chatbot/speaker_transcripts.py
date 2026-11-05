"""Align word timestamps with finalized activity without inventing speaker labels."""

import json
import math
from collections.abc import Mapping

from simple_chatbot.diarization import RATE, SpeakerSpan
from simple_chatbot.speakers import SpeakerProfiles


def attribute_transcript(
    payload: Mapping, span: SpeakerSpan, profiles: SpeakerProfiles
) -> tuple[str, str]:
    text = payload["text"]
    words = payload.get("words")
    parts: list[tuple[str | None, str]] = []
    if (
        span.generation == profiles.generation
        and isinstance(words, list)
        and 0 < len(words) <= 2048
    ):
        previous = 0.0
        for word in words:
            if not isinstance(word, dict):
                break
            value, start, end = word.get("word"), word.get("start"), word.get("end")
            if (
                not isinstance(value, str)
                or not value.strip()
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or isinstance(start, bool)
                or isinstance(end, bool)
                or not math.isfinite(start)
                or not math.isfinite(end)
                or not previous <= start < end
                or end > (span.end - span.start) / RATE
            ):
                break
            speaker = span.speaker(span.start + round(start * RATE), span.start + round(end * RATE))
            parts.append((speaker, value))
            previous = end
        else:
            # Some providers omit words or normalize the separate text differently.
            # Never silently discard text to make it fit their timestamps.
            if "".join(text.split()) != "".join("".join(word for _, word in parts).split()):
                parts = []
        if len(parts) != len(words):
            parts = []
    if not parts:
        parts = [(None, text)]
    profiles.observe([speaker for speaker, _ in parts])
    groups: list[dict[str, str]] = []
    for speaker, word in parts:
        label = profiles.names.get(speaker, "") if speaker else ""
        # Include the generation so IDs from before 'forget' cannot be mistaken
        # for new voices. JSON distinguishes transcript data from label metadata.
        identity = f"{profiles.generation}:{speaker}" if speaker else "Unknown"
        if groups and groups[-1]["speaker"] == identity:
            groups[-1]["text"] += " " + word.strip()
        else:
            groups.append({"speaker": identity, "name": label, "text": word.strip()})
    return json.dumps({"speaker_turn": groups}, ensure_ascii=False), profiles.current_speaker or ""
