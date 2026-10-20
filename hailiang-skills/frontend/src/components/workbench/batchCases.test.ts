import { describe, expect, it } from "vitest";
import { parsePastedCases } from "./batchCases";

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
