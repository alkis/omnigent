// After the real runner idle watchdog fires, desktop shows the session offline.
// The timeout is compressed to seconds; this does not validate an overnight idle period.
// Run after building the SPA: node --test e2e/desktop_idle_runner_offline.e2e.js

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const RECORD_DIR = path.join(__dirname, "recordings", "idle-runner-offline");
// Short enough to observe without an hour-long wait; long enough that the
// runner reaches "online" before the watchdog fires.
const IDLE_TIMEOUT_S = 20;

describe(
  "desktop shell — session reaped after idle is offline the next morning",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-e2e-"));
      server = await spawnServer(tmpDir, { idleTimeoutS: IDLE_TIMEOUT_S });
    });

    after(async () => {
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it("shows the conversation offline after the runner idle-reaps", async () => {
      const convId = server.createConversation();

      // Leave the session idle with no client attached; the runner's watchdog
      // reaps it. The server then reports the runner offline.
      const wentOffline = await server.waitForRunnerOffline(90_000);
      assert.equal(
        wentOffline,
        true,
        `runner still online after > 90s idle (configured window ${IDLE_TIMEOUT_S}s)`,
      );

      // The "next morning": open the app and view the conversation.
      const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-data-"));
      const { electronApp, window, stopDisplayCapture } = await launchDesktop({
        recordDir: RECORD_DIR,
        serverUrl: server.serverUrl,
        userDataDir,
      });
      let disconnectedVisible = false;
      try {
        const landing = window.getByTestId("new-chat-landing-input");
        try {
          await landing.waitFor({ state: "visible", timeout: 30_000 });
        } catch {
          // A transient failure of the very first load parks the window on an
          // error surface with nothing to auto-retry; navigate once more.
          await window.goto(server.serverUrl);
          await landing.waitFor({ state: "visible", timeout: 30_000 });
        }
        await window.reload();
        const link = window.locator(`a[href*="/c/${convId}"]`).first();
        await link.waitFor({ state: "visible", timeout: 30_000 });
        await link.click();
        await window.waitForURL(new RegExp(`/c/${convId}([/?#]|$)`), { timeout: 30_000 });
        // The reaped session surfaces the reconnect affordance.
        await window
          .getByTestId("disconnected-indicator")
          .waitFor({ state: "visible", timeout: 30_000 });
        disconnectedVisible = true;
        // Hold the offline state on screen so the recording shows it.
        await window.waitForTimeout(2_500);
      } finally {
        await electronApp.close();
        await stopDisplayCapture();
        saveRecording(RECORD_DIR, "idle-offline");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      assert.equal(
        disconnectedVisible,
        true,
        `conversation /c/${convId} did not surface the offline/reconnect indicator after the runner was reaped`,
      );
    });
  },
);
