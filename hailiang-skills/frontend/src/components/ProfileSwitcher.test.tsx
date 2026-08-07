import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ProfileSwitcher } from "@/components/ProfileSwitcher";

describe("ProfileSwitcher", () => {
  it("requires school year and grade before creating a child profile", async () => {
    const onCreate = vi.fn();
    render(
      <ProfileSwitcher
        profiles={[]}
        activeProfileId=""
        onSelect={vi.fn()}
        onCreate={onCreate}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "新建孩子" }));
    expect(screen.getByRole("button", { name: "创建" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("新孩子名称"), { target: { value: "小明" } });
    fireEvent.change(screen.getByLabelText("学年"), { target: { value: "2026-2027" } });
    fireEvent.change(screen.getByLabelText("年级"), { target: { value: "高一" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "创建" }));
    });

    expect(onCreate).toHaveBeenCalledWith({ name: "小明", schoolYear: "2026-2027", grade: "高一" });
  });
});
