import { Fragment } from "react";

// The gateway emits a small Markdown report, not arbitrary HTML. Render only
// its paragraphs, code spans and three-column table, keeping all content text.
function inline(text: string) {
  return text.split(/(`[^`\n]+`)/g).map((part, index) =>
    part.startsWith("`") && part.endsWith("`") ? (
      <code key={index} className="rounded bg-muted px-1">{part.slice(1, -1)}</code>
    ) : <Fragment key={index}>{part}</Fragment>,
  );
}

function Report({ text }: { text: string }) {
  const lines = text.split("\n");
  const header = lines.findIndex((line, index) =>
    /^\|\s*Entity\s*\|\s*Request\s*\|\s*Response\s*\|\s*$/.test(line) &&
    /^\|\s*-+\s*\|\s*-+\s*\|\s*-+\s*\|\s*$/.test(lines[index + 1] ?? ""),
  );
  let end = header + 2;
  const rows: string[][] = [];
  if (header >= 0) {
    while (end < lines.length) {
      const line = lines[end]?.trim() ?? "";
      if (!line.startsWith("|") || !line.endsWith("|")) break;
      const cells = line.slice(1, -1).split("|").map((cell) => cell.trim());
      if (cells.length !== 3) break;
      rows.push(cells);
      end++;
    }
  }
  return (
    <>
      <p className="whitespace-pre-wrap">{inline((header < 0 ? lines : lines.slice(0, header)).join("\n"))}</p>
      {header >= 0 && (
        <>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">PII detection and policy actions</caption>
              <thead><tr>{["Entity", "Request", "Response"].map((label) => (
                <th key={label} scope="col" className="border-b p-2">{label}</th>
              ))}</tr></thead>
              <tbody>{rows.map((row, index) => (
                <tr key={index}>{row.map((cell, column) => (
                  <td key={column} className="border-b p-2 align-top">{inline(cell)}</td>
                ))}</tr>
              ))}</tbody>
            </table>
          </div>
          <p className="whitespace-pre-wrap">{inline(lines.slice(end).join("\n"))}</p>
        </>
      )}
    </>
  );
}

export function ProtectionNotices({ notices }: { notices: string[] }) {
  if (!notices.length) return null;
  return (
    <section aria-label="PII protection notices" className="max-h-80 space-y-3 overflow-y-auto border-t p-4">
      {notices.map((text, index) => (
        <article key={index} className="rounded-lg border bg-muted/40 p-3 text-sm">
          <h3 className="mb-2 font-medium">PII protection notice — powered by neurwerk</h3>
          <Report text={text} />
        </article>
      ))}
    </section>
  );
}
