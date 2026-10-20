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

const OPENING_HEADER = /首轮/;
const PERSONA_HEADER = /画像/;
const SCENE_HEADER = /场景|skill/i;
const GOAL_HEADER = /目标/;

export function parseDelimited(text: string): string[][] {
  const source = text.replace(/^\uFEFF/, "");
  const sample = source.split(/\r?\n/, 1)[0] ?? "";
  const tabs = sample.split("\t").length - 1;
  const commas = sample.split(",").length - 1;
  const delimiter = tabs > commas ? "\t" : ",";
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (quoted) {
      if (char === "\"") {
        if (source[index + 1] === "\"") {
          cell += "\"";
          index += 1;
        } else {
          quoted = false;
        }
      } else {
        cell += char;
      }
      continue;
    }
    if (char === "\"") {
      quoted = true;
    } else if (char === delimiter) {
      row.push(cell);
      cell = "";
    } else if (char === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else if (char !== "\r") {
      cell += char;
    }
  }
  if (cell || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

export async function rowsFromWorkbook(buffer: ArrayBuffer): Promise<string[][]> {
  const loaded = await import("xlsx");
  const XLSX = "read" in loaded ? loaded : loaded.default;
  const book = XLSX.read(buffer, { type: "array" });
  const sheet = book.Sheets[book.SheetNames[0]];
  if (!sheet) throw new Error("表格里没有可读取的工作表。");
  return XLSX.utils.sheet_to_json(sheet, { header: 1, raw: false, defval: "" }) as string[][];
}

export function casesFromRows(rows: unknown[][]): BatchCaseDraft[] {
  const normalized = rows
    .map((row) => (Array.isArray(row) ? row : []).map((cell) => String(cell ?? "").trim()))
    .filter((row) => row.some(Boolean));
  const headerIndex = normalized.findIndex((row) => OPENING_HEADER.test(row.join(" ")) && PERSONA_HEADER.test(row.join(" ")));
  const header = headerIndex >= 0 ? normalized[headerIndex] : null;
  const sceneIndex = header ? header.findIndex((cell) => SCENE_HEADER.test(cell)) : 0;
  const openingIndex = header ? header.findIndex((cell) => OPENING_HEADER.test(cell)) : 1;
  const personaIndex = header ? header.findIndex((cell) => PERSONA_HEADER.test(cell)) : 2;
  const goalIndex = header ? header.findIndex((cell) => GOAL_HEADER.test(cell)) : 3;
  const body = header ? normalized.slice(headerIndex + 1) : normalized;
  const cases: BatchCaseDraft[] = [];
  for (const row of body) {
    const opening = row[openingIndex] ?? "";
    const persona = row[personaIndex] ?? "";
    if (!opening || !persona) continue;
    cases.push({
      scene: sceneIndex >= 0 ? row[sceneIndex] ?? "" : "",
      opening,
      persona,
      goal: goalIndex >= 0 ? row[goalIndex] ?? "" : "",
    });
  }
  return cases;
}
