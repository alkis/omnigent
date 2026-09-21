import argparse
import json
import uuid
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from omnigent.spec.types import LocalToolInfo
from omnigent.tools.base import ToolContext
from omnigent.tools.local import load_local_python_tools

parser = argparse.ArgumentParser(
    description="Validate bundled tools through real subprocess dispatch and browser/API turns."
)
parser.add_argument("--checkout", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
source = Path(__file__).resolve().parent / "repro-agent"
infos = [
    LocalToolInfo(name=p.stem, path=str(p.relative_to(source)), language="python")
    for p in (source / "tools/python").glob("*.py")
]
tools = {
    t.name(): t
    for t in load_local_python_tools(infos, source, srt_available=False, uv_available=False)
}
ctx = ToolContext(task_id="integration", agent_id="validation", workspace=source)


def call(name, **kwargs):
    result = tools[name].invoke(json.dumps(kwargs), ctx)
    try:
        value = json.loads(result)
    except json.JSONDecodeError as exc:
        raise RuntimeError(result) from exc
    print(name, json.dumps(value)[:1200], flush=True)
    assert "error" not in value, value
    return value


state = call(
    "repro_start_environment",
    checkout=str(args.checkout.resolve()),
    output=str(args.output.resolve()),
    lease_seconds=600,
)
env = state["environment"]
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        for harness in ("claude-native", "codex-native", "openai-agents"):
            sess = call(
                "repro_start_session",
                environment=env,
                harness=harness,
                journey=f"{harness}: two-turn real session capability validation",
            )
            sid = sess["session_id"]
            context = browser.new_context(record_video_dir=str(Path(env).parent / "video"))
            page = context.new_page()
            page.goto(sess["browser_url"])
            if harness != "openai-agents":
                # Terminal-first sessions start at the terminal tab.
                expect(page.get_by_test_id("terminal-view").last).to_have_attribute(
                    "data-state", "connected", timeout=120000
                )
                page.get_by_test_id("view-mode-chat").click()
            for i in range(2):
                prompt = f"user-{uuid.uuid4().hex}: Please respond briefly."
                reply = f"answer-{uuid.uuid4().hex}"
                if i == 0:
                    turn = call(
                        "repro_prepare_browser_turn",
                        environment=env,
                        session_id=sid,
                        prompt=prompt,
                        scripted_reply=reply,
                    )
                    page.get_by_role("textbox", name="Message the agent").fill(prompt)
                    page.get_by_role("button", name="Send", exact=True).click()
                else:
                    turn = call(
                        "repro_send_message",
                        environment=env,
                        session_id=sid,
                        prompt=prompt,
                        scripted_reply=reply,
                    )
                expect(page.get_by_text(reply, exact=True)).to_be_visible(timeout=90000)
                result = call(
                    "repro_wait_for_turn",
                    environment=env,
                    session_id=sid,
                    turn_id=turn["turn_id"],
                    timeout=30,
                )
                assert result["status"] == "completed", result
            context.close()
            call("repro_inspect_session", environment=env, session_id=sid)
            call("repro_close_session", environment=env, session_id=sid)
        browser.close()
finally:
    call("repro_stop_environment", environment=env)
