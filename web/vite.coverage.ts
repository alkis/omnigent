import istanbul from "vite-plugin-istanbul";
import type { Plugin } from "vite";

export const E2E_COVERAGE_ENV = "OMNIGENT_E2E_COVERAGE_INDEX";

export function createE2eCoveragePlugin(env: NodeJS.ProcessEnv = process.env): Plugin | null {
  if (env[E2E_COVERAGE_ENV] !== "1") return null;

  return istanbul({
    include: "src/**/*.{ts,tsx}",
    exclude: [
      "node_modules/**",
      "src/**/*.test.{ts,tsx}",
      "src/**/*.spec.{ts,tsx}",
      "src/**/*.d.ts",
      "src/test-setup.ts",
      "src/**/*.stories.{ts,tsx}",
      "src/storybook/**",
      "src/**/*storyFixtures.{ts,tsx}",
      "src/**/*StoryFixtures.{ts,tsx}",
      "src/components/ai-elements/**",
    ],
    extension: [".ts", ".tsx"],
    requireEnv: false,
    forceBuildInstrument: true,
  });
}
