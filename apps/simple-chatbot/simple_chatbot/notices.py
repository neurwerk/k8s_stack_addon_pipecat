"""Separate the gateway's appended report, including split streaming markers."""

MARKER = "\n\n---\nPII Engine Notice\n"


class SpeechControlFilter:
    """Remove inline model reasoning/control blocks across arbitrary chunk boundaries."""

    TAGS = {
        name: f"<{name}>"
        for name in ("think", "analysis", "reasoning", "tool_call", "function_call")
    }

    def __init__(self) -> None:
        self.pending = ""
        self.stack: list[str] = []

    def feed(self, text: str) -> str:
        self.pending += text
        output = ""
        tags = {
            **{tag: (name, True) for name, tag in self.TAGS.items()},
            **{f"</{name}>": (name, False) for name in self.TAGS},
        }
        while self.pending:
            if not self.pending.startswith("<"):
                index = self.pending.find("<")
                index = index if index >= 0 else len(self.pending)
                if not self.stack:
                    output += self.pending[:index]
                self.pending = self.pending[index:]
                continue
            match = next((tag for tag in tags if self.pending.startswith(tag)), None)
            if match:
                name, opening = tags[match]
                if opening:
                    self.stack.append(name)
                elif self.stack and self.stack[-1] == name:
                    self.stack.pop()
                self.pending = self.pending[len(match) :]
            elif any(tag.startswith(self.pending) for tag in tags):
                break
            else:
                if not self.stack:
                    output += self.pending[0]
                self.pending = self.pending[1:]
        return output


class NoticeSplitter:
    def __init__(self) -> None:
        self.pending = ""
        self.notice = ""
        self.in_notice = False

    def feed(self, text: str) -> str:
        if self.in_notice:
            self.notice = (self.notice + text)[:32768]
            return ""
        self.pending += text
        if MARKER in self.pending:
            speech, self.notice = self.pending.split(MARKER, 1)
            self.notice = self.notice[:32768]
            self.pending = ""
            self.in_notice = True
            return speech
        # Only withhold a possible marker prefix, not an arbitrary chunk length.
        keep = 0
        for size in range(1, min(len(self.pending), len(MARKER) - 1) + 1):
            if self.pending.endswith(MARKER[:size]):
                keep = size
        result = self.pending[:-keep] if keep else self.pending
        self.pending = self.pending[-keep:] if keep else ""
        return result

    def finish(self) -> str:
        # A truncated marker is not speech. A complete normal suffix is already emitted.
        self.pending = ""
        return ""
