import { describe, expect, it } from "vitest";
import { updateOverlayToastOffset } from "./updateOverlayInset";

describe("updateOverlayToastOffset", () => {
  it("uses the safe-area top inset plus the shell gap", () => {
    expect(updateOverlayToastOffset()).toBe("calc(1rem + var(--omnigent-inset-top))");
  });
});
