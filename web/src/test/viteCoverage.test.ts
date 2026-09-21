import { describe, expect, it } from "vitest";
import { createE2eCoveragePlugin, E2E_COVERAGE_ENV } from "../../vite.coverage";

describe("createE2eCoveragePlugin", () => {
  it("leaves ordinary builds unchanged", () => {
    expect(createE2eCoveragePlugin({})).toBeNull();
  });

  it("enables Istanbul only for coverage-index builds", () => {
    const plugin = createE2eCoveragePlugin({ [E2E_COVERAGE_ENV]: "1" });

    expect(plugin?.name).toBe("vite:istanbul");
  });
});
