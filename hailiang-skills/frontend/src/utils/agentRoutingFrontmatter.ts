import { parseDocument, YAMLMap, YAMLSeq } from "yaml";

const DELIMITER = "---";

function splitFrontMatter(markdown: string): { yaml: string; body: string } | null {
  if (!markdown.startsWith(`${DELIMITER}\n`)) return null;
  const closing = markdown.indexOf(`\n${DELIMITER}`, DELIMITER.length + 1);
  if (closing < 0) throw new Error("AGENT.md YAML front matter 缺少结束分隔符 ---");
  const bodyStart = closing + DELIMITER.length + 1;
  return {
    yaml: markdown.slice(DELIMITER.length + 1, closing),
    body: markdown.slice(bodyStart).replace(/^\n/, ""),
  };
}

/**
 * Add only missing Skill skeletons. Existing YAML nodes are retained by the
 * YAML document AST, so a business user's rules/comments are never replaced.
 */
export function syncAgentRoutingFrontmatter(markdown: string, skillIds: string[]): string {
  const source = String(markdown ?? "");
  const uniqueIds = [...new Set(skillIds.map((value) => value.trim()).filter(Boolean))];
  if (!uniqueIds.length) return source;
  const existing = splitFrontMatter(source);
  const document = parseDocument(existing?.yaml ?? "");
  if (document.errors.length) {
    throw new Error(`AGENT.md YAML front matter 格式错误：${document.errors[0].message}`);
  }
  if (!document.contents) {
    const root = document.createNode({}) as YAMLMap;
    root.flow = false;
    document.contents = root as never;
  }
  if (!(document.contents instanceof YAMLMap)) {
    throw new Error("AGENT.md YAML front matter 必须是对象，无法自动同步 Skill 模板。");
  }
  const root = document.contents as YAMLMap;
  let routing = root.get("skill_routing", true) as unknown;
  if (routing == null) {
    const generatedRouting = document.createNode({ rules: [] }) as YAMLMap;
    generatedRouting.flow = false;
    const generatedRules = generatedRouting.get("rules", true);
    if (generatedRules instanceof YAMLSeq) generatedRules.flow = false;
    root.set("skill_routing", generatedRouting);
    routing = root.get("skill_routing", true);
  }
  if (!(routing instanceof YAMLMap)) {
    throw new Error("skill_routing 必须是对象，无法自动同步 Skill 模板。");
  }
  let rules = routing.get("rules", true) as unknown;
  if (rules == null) {
    const generatedRules = document.createNode([]) as YAMLSeq;
    generatedRules.flow = false;
    routing.set("rules", generatedRules);
    rules = routing.get("rules", true);
  }
  if (!(rules instanceof YAMLSeq)) {
    throw new Error("skill_routing.rules 必须是数组，无法自动同步 Skill 模板。");
  }
  const existingIds = new Set(
    rules.items.flatMap((item) => {
      if (!(item instanceof YAMLMap)) return [];
      const value = item.get("skill_id");
      return typeof value === "string" ? [value] : [];
    }),
  );
  for (const skillId of uniqueIds) {
    if (!existingIds.has(skillId)) {
      const generatedRule = document.createNode({ skill_id: skillId, when: [] }) as YAMLMap;
      generatedRule.flow = false;
      rules.add(generatedRule);
    }
  }
  return `${DELIMITER}\n${document.toString().trimEnd()}\n${DELIMITER}\n${existing?.body ?? source}`;
}
