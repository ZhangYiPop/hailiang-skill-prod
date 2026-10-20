export type BatchCaseDraft = {
  scene: string;
  opening: string;
  persona: string;
  goal: string;
};

const HEADER = /^(场景|skill|首轮)/i;

export function parsePastedCases(text: string): BatchCaseDraft[] {
  const rows: BatchCaseDraft[] = [];
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const cells = trimmed.includes("\t")
      ? trimmed.split("\t").map((cell) => cell.trim())
      : trimmed.split(/\s*\|\s*/).map((cell) => cell.trim());
    if (cells.length < 3) continue;
    if (rows.length === 0 && HEADER.test(cells[0]) && HEADER.test(cells[1])) continue;
    const [scene, opening, persona, goal = ""] = cells;
    if (!opening || !persona) continue;
    rows.push({ scene, opening, persona, goal });
  }
  return rows;
}

export function casesToTsv(cases: BatchCaseDraft[]): string {
  return cases
    .map((item) => [item.scene, item.opening, item.persona, item.goal].join("\t"))
    .join("\n");
}
