import { describe, expect, it } from "vitest";
import { casesFromRows, parseDelimited, parsePastedCases, rowsFromWorkbook } from "./batchCases";

describe("parsePastedCases", () => {
  it("reads tab-separated rows and skips the header", () => {
    const text = [
      "场景（skill）\t首轮对话 (1)\t画像信息 (1)\t会话最终目标 (1)",
      "选科参谋\t数学差可以选物理方向吗\t城市:山东；学段:高一\t明确选科组合",
    ].join("\n");
    expect(parsePastedCases(text)).toEqual([
      {
        scene: "选科参谋",
        opening: "数学差可以选物理方向吗",
        persona: "城市:山东；学段:高一",
        goal: "明确选科组合",
      },
    ]);
  });

  it("accepts pipe-separated rows", () => {
    expect(parsePastedCases("升学潜力评估 | 600分能上什么大学 | 城市:安徽；学段:高一 | 判断院校层次")).toEqual([
      {
        scene: "升学潜力评估",
        opening: "600分能上什么大学",
        persona: "城市:安徽；学段:高一",
        goal: "判断院校层次",
      },
    ]);
  });
});

describe("casesFromRows", () => {
  it("reads Feishu-style headers in any column order", () => {
    expect(casesFromRows([
      ["记录 ID", "会话最终目标 (1)", "画像信息 (1)", "首轮对话 (1)", "场景（skill）"],
      ["rec_1", "明确选科组合", "城市:山东；学段:高一", "数学差可以选物理方向吗", "选科参谋"],
    ])).toEqual([
      {
        scene: "选科参谋",
        opening: "数学差可以选物理方向吗",
        persona: "城市:山东；学段:高一",
        goal: "明确选科组合",
      },
    ]);
  });

  it("parses quoted csv cells that contain commas", () => {
    const rows = parseDelimited("场景,首轮对话,画像信息,会话最终目标\n选科,\"你好,我想选科\",\"城市:成都,学段:高二\",给出组合\n");
    expect(casesFromRows(rows)).toEqual([
      {
        scene: "选科",
        opening: "你好,我想选科",
        persona: "城市:成都,学段:高二",
        goal: "给出组合",
      },
    ]);
  });

  it("reads an xlsx sheet with the evaluation headers", async () => {
    const loaded = await import("xlsx");
    const XLSX = loaded;
    const book = XLSX.utils.book_new();
    const sheet = XLSX.utils.aoa_to_sheet([
      ["场景（skill）", "首轮对话 (1)", "画像信息 (1)", "会话最终目标 (1)"],
      ["选科参谋", "怎么选", "城市:山东；学段:高一", "给出组合"],
    ]);
    XLSX.utils.book_append_sheet(book, sheet, "多轮对话session集");
    const buffer = XLSX.write(book, { type: "array", bookType: "xlsx" }) as ArrayBuffer;
    expect(casesFromRows(await rowsFromWorkbook(buffer))).toEqual([
      {
        scene: "选科参谋",
        opening: "怎么选",
        persona: "城市:山东；学段:高一",
        goal: "给出组合",
      },
    ]);
  });
});
