import { describe, expect, it } from "vitest";

import { groupReleasesByObject } from "@/pages/Workbench";
import type { ObjectRelease } from "@/utils/workbenchApi";

function release(overrides: Partial<ObjectRelease>): ObjectRelease {
  return {
    release_id: "rel_default",
    object_id: "obj_default",
    object_type: "skill",
    object_key: "default_skill",
    name: "默认 Skill",
    revision_id: "rev_default",
    release_no: 1,
    version: "v1",
    is_current: false,
    dependency_locks: [],
    content_hash: "hash",
    archived: false,
    published_by: "tester",
    published_at: "2026-09-07T00:00:00Z",
    ...overrides,
  };
}

describe("groupReleasesByObject", () => {
  it("groups versions by object ID and puts the current version first", () => {
    const groups = groupReleasesByObject([
      release({ release_id: "rel_skill_v1", object_id: "obj_skill", object_key: "study_skill", name: "学习 Skill", release_no: 1, version: "v1" }),
      release({ release_id: "rel_other_v1", object_id: "obj_other", object_key: "other_skill", name: "其他 Skill", release_no: 1, version: "v1" }),
      release({ release_id: "rel_skill_v2", object_id: "obj_skill", object_key: "study_skill", name: "学习 Skill", release_no: 2, version: "v2", is_current: true }),
    ]);

    const skillGroup = groups.find((group) => group.object_id === "obj_skill");
    expect(groups).toHaveLength(2);
    expect(skillGroup?.releases.map((item) => item.version)).toEqual(["v2", "v1"]);
  });
});
