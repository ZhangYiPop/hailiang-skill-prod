import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, waitFor } from "@testing-library/react";

import { groupReleasesByObject, HistoryEvidence } from "@/pages/Workbench";
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

describe("HistoryEvidence", () => {
  it("does not inspect closed evidence and bounds the expanded preview", async () => {
    const read = vi.fn(() => "x".repeat(1000000));
    const value = Object.defineProperty({}, "prompt", { enumerable: true, get: read });
    const { container, rerender } = render(<HistoryEvidence value={value} />);
    expect(read).not.toHaveBeenCalled();
    expect(container.querySelector("pre")).toBeNull();
    const details = container.querySelector("details")!;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    await waitFor(() => expect(container.querySelector("pre")).not.toBeNull());
    expect(container.querySelector("pre")!.textContent!.length).toBeLessThan(6100);
    const calls = read.mock.calls.length;
    rerender(<HistoryEvidence value={value} />);
    expect(read.mock.calls.length).toBe(calls);
    details.open = false;
    fireEvent(details, new Event("toggle"));
    await waitFor(() => expect(container.querySelector("pre")).toBeNull());
  });
});

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
