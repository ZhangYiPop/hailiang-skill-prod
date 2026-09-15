import { describe, expect, it } from "vitest";

import { syncAgentRoutingFrontmatter } from "./agentRoutingFrontmatter";

describe("syncAgentRoutingFrontmatter", () => {
  it("creates a routing skeleton without changing the AGENT.md body", () => {
    const markdown = "# 专家策略\n\n按实际问题决定是否调用 Skill。\n";

    const result = syncAgentRoutingFrontmatter(markdown, ["interest_primary", "cost_calculator"]);

    expect(result).toContain(`skill_routing:
  rules:
    - skill_id: interest_primary
      when: []
    - skill_id: cost_calculator
      when: []`);
    expect(result).toMatch(/---\n# 专家策略\n\n按实际问题决定是否调用 Skill。\n$/);
  });

  it("only appends missing Skill IDs and retains manual rules and unknown fields", () => {
    const markdown = `---
owner: growth-team
skill_routing:
  rules:
    - skill_id: interest_primary
      when:
        - 已明确兴趣方向
      priority: 10
---
# 专家策略
`;

    const result = syncAgentRoutingFrontmatter(markdown, ["interest_primary", "cost_calculator"]);

    expect(result).toContain("owner: growth-team");
    expect(result).toContain("skill_id: interest_primary");
    expect(result).toContain("- 已明确兴趣方向");
    expect(result).toContain("priority: 10");
    expect(result.match(/skill_id: interest_primary/g)).toHaveLength(1);
    expect(result).toContain("skill_id: cost_calculator");
    expect(result).toContain("# 专家策略");
  });

  it("does not alter the source when no locked Skill remains", () => {
    const markdown = "---\nskill_routing:\n  rules: []\n---\n# 专家策略\n";

    expect(syncAgentRoutingFrontmatter(markdown, [])).toBe(markdown);
  });

  it("refuses malformed front matter instead of overwriting it", () => {
    const malformed = "---\nskill_routing: [\n---\n# 专家策略\n";

    expect(() => syncAgentRoutingFrontmatter(malformed, ["interest_primary"])).toThrow("格式错误");
  });
});
