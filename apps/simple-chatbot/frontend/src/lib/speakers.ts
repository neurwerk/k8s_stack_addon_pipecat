/** Display the backend's attributed transcript without exposing its JSON envelope. */
export function speakerText(text: string): string {
  if (!text.startsWith('{"speaker_turn":') || text.length > 65536) return text;
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return text;
  }
  if (typeof value !== "object" || value === null || !("speaker_turn" in value))
    return text;
  const turns: unknown = value.speaker_turn;
  if (!Array.isArray(turns) || turns.length === 0 || turns.length > 2048)
    return text;
  const lines: string[] = [];
  for (const turn of turns) {
    if (
      typeof turn !== "object" || turn === null ||
      !("speaker" in turn) || typeof turn.speaker !== "string" ||
      !("name" in turn) || typeof turn.name !== "string" ||
      !("text" in turn) || typeof turn.text !== "string"
    ) return text;
    const match = /^\d+:speaker_([0-7])$/.exec(turn.speaker);
    if (!match && turn.speaker !== "Unknown") return text;
    const label = turn.name || (match ? `Speaker ${Number(match[1]) + 1}` : "Unknown speaker");
    lines.push(`${label}: ${turn.text}`);
  }
  return lines.join("\n");
}
