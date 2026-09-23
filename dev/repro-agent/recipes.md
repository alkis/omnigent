# Prepared-runtime driving recipes

These are opt-in tools for checking a driving path. They do not reproduce a
ticket by themselves or change the workflow's feature flags. Use the ticket's
actual actions, surface, state, and timing for the reproduction test.

## Launch

Use the workflow-owned runtime described in [repro_env](../repro_env/README.md).
Do not launch a second server inside a workflow attempt. For isolated local
development, build the SPA, then run `python -m dev.repro_env serve` in a
persistent terminal; use another terminal in the same checkout for commands
below. Native recipes need the real `claude` / `codex` binaries; OpenAI Agents
needs the `openai-agents` Python package. All need pytest-playwright and Chromium.
No live-provider credentials are needed: these recipes are intended for mock
model responses. Machine-managed CLI policy can override that routing, even
with an isolated CLI home. Doctor blocks native smoke runs when known machine
policy files are present (or their absence was not recorded); validate in the
configured CI runtime instead of overriding company policy.

## Doctor

```sh
python -m dev.repro_env doctor --harness claude-native
```

Select `claude-native`, `codex-native`, or `openai-agents`. Doctor saves a unique
`doctor-*.json` in `.omnigent/repro-env/` and prints it. It checks the runner and
model connection live and reads launch-time observations from the supervisor's
own environment: checkout revision/dirty state, SPA index hash, OS, Python,
CLI/SDK versions, and the prepared runtime's configuration. A missing version
blocks the smoke check. An older runtime without launch observations must be
restarted in a fresh output directory; Doctor does not guess from the caller's
machine. Restart after changing source or binaries.

To compare material requirements from the report, supply a JSON file:

```json
[
  {"field": "os.system", "expected": "Darwin", "source": "Reporter: macOS"},
  {"field": "model_backend", "expected": "live", "source": "Reporter: live provider"}
]
```

```sh
python -m dev.repro_env doctor --harness codex-native --requirements requirements.json
```

This example flags the Linux/mock substitutions. Comparisons are exact and
type-sensitive; unsupported fields and missing observations are `unknown`.
Unknowns and mismatches return a nonzero exit code. Without requirements the
result is `not_assessed`, even if connectivity passes. Record each mismatch or
unknown in the reproduction plan, with its impact on the reported trigger.
Do not quietly change the expected value to get a pass.

Doctor does not inspect a future session: its surface, model selection, policies,
history, and starting state remain unknown. Confirm those in the ticket-specific
journey. The UI hash identifies an asset, not the source commit that built it.
`model_backend` describes the prepared mock service, not proven session routing.
The supplied requirements still need review for completeness and relevance.

## Drive

Run one recipe at a time; they share mock state:

```sh
python -m dev.repro_env smoke --harness claude-native
python -m dev.repro_env smoke --harness codex-native
python -m dev.repro_env smoke --harness openai-agents
```

Each command runs Doctor, then an existing browser test against the prepared
runtime. It fails if prerequisites are missing, the test skips or fails, or
required evidence cannot be saved. A passed test checks a benign interaction
with controlled replies. It says nothing about whether a reported bug exists.

| Recipe | Session setup and user entry points | Existing driver | Incorrect substitute |
| --- | --- | --- | --- |
| Claude-native | Product session API creates the native wrapper session; runner starts real Claude Code. Two web composer turns, then input typed into the connected terminal. | `tests/e2e_ui/messages/test_native_claude_render_parity.py` / `native_claude_mock_session` | Calling a Python callback, inserting transcript items, or drawing fake terminal output does not drive Claude Code. |
| Codex-native | Product session API creates the native wrapper session with its workspace; runner starts real Codex. Two web composer turns, then real terminal input. | `tests/e2e_ui/messages/test_native_codex_render_parity.py` / `native_codex_mock_session` | An SDK-only turn does not exercise the native bridge. An HTTP request cannot stand in for a reported terminal shortcut or slash command. |
| OpenAI Agents | Product session API creates a custom agent using the OpenAI Agents executor. Five real web composer turns. | `tests/e2e_ui/messages/test_message_render_parity.py::test_custom_agent_message_render_parity` / `custom_agent_session` | A direct executor/function call bypasses the web/runner journey. This recipe has no native vendor TUI. |

All three check unique user/reply markers in the visible UI and canonical
transcript. Those are related views of the same interaction, not independent
proof of the ticket's failure. Mock requests give additional execution context.
These recipes cover an existing session; API-created session setup does **not**
test CLI onboarding, session creation UI, authentication, or reconnect history.

For the real reproduction, reuse the appropriate fixture in an authored test,
replace the benign actions with the reported journey, and assert the specific
symptom. Preserve setup/trigger/observation separately. Start recording before
the trigger; a negative observation needs a confirmed trigger and a justified
waiting interval. Browser route interception, mock approvals, injected product
events, or a different harness must be disclosed as substitutions; they cannot
silently replace the native action. A mock provider is unsuitable for claims
about actual model/provider behavior.

## Evidence

Every smoke command prints its unique directory under
`.omnigent/repro-env/smoke/`. It contains Doctor, pytest setup/call/teardown
outcomes, the session ID, transcript items and captured model requests, plus
Playwright video and trace. The opt-in pytest capture runs before session fixture
cleanup, including when the test assertion fails. A setup failure can have no
session evidence; it must remain a failed check. Skips never count as success.
`result.json` records the final exit status; a missing or empty transcript/model
capture fails the command even if pytest's interaction assertion passed.
Model requests cover only the period since the driver's last mock reset; some
existing drivers reset between turns. This is not a complete per-turn request log.
The files are local diagnostics and may contain prompt or workspace content;
review them before publishing.

For an authored journey, save its session/transcript and model requests before
deleting the session or resetting the mock, and close the browser context in
`finally`. Automatic evidence collection for arbitrary journeys is separate
work; the small smoke capture plugin only supports the three recipes above.

## Cleanup

Pytest closes its browser and removes its temporary session; saved evidence
survives. The workflow owns server/runner shutdown and bundles the repro-env
directory. For a locally started runtime, run `python -m dev.repro_env stop` and
wait for `serve` to exit. Shutdown also saves process logs, the database, and
model requests since the last reset. Retain the attempt directory; a new `serve`
needs a fresh `--output PATH`. Do not stop a shared workflow runtime yourself.
