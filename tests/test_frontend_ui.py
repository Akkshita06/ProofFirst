"""
Frontend E2E regression tests, run against a real live uvicorn process with
a real browser (Playwright). These exist because both bugs they cover are
DOM-level / HTTP-call-count bugs that a pure ASGI TestClient test (see
test_api.py) can't observe on its own — you have to actually click things in
a rendered page.

Bug 1 (stacked duplicate tables): a direct call to a view-render function
(e.g. `renderLeads($("#view"))`) instead of the router's `render()`
dispatcher appends a second table on top of the first, because
`renderLeads` only ever appends and never clears its container. Regression-
tested here by counting `table.data-table` elements after triggering an
in-panel action.

Bug 2 (approval panel mutates on view): opening the approval panel used to
call `POST /api/leads/{id}/run`, silently re-running the whole pipeline and
inserting a new PENDING_APPROVAL row every time someone just looked at it.
Regression-tested here by asserting the activity log length (a proxy for
"did anything get re-run/inserted") stays flat across repeated opens.

Requires: `pip install playwright && playwright install chromium`
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """Spins up the real app as a subprocess (not the ASGI TestClient) so a
    real browser can talk to it over HTTP, with a fresh DB for this module."""
    db_path = ROOT / "proof_first" / "prooffirst.db"
    if db_path.exists():
        db_path.unlink()

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "proof_first.server:app", "--port", str(port)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                if requests.get(f"{base_url}/api/leads", timeout=1).ok:
                    break
            except requests.exceptions.ConnectionError:
                pass
            time.sleep(0.2)
        else:
            proc.kill()
            raise RuntimeError("live_server did not come up in time")
        yield base_url
    finally:
        proc.kill()
        proc.wait(timeout=5)
        if db_path.exists():
            db_path.unlink()


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api").sync_playwright().start()
    b = playwright.chromium.launch()
    yield b
    b.close()
    playwright.stop()


@pytest.fixture()
def page(browser):
    p = browser.new_page()
    yield p
    p.close()


def test_no_duplicate_tables_after_inpanel_research_run(live_server, page):
    """Regression test for Bug 1: run research from inside the record panel
    while on the Leads route, and confirm exactly one leads table exists
    afterwards — not stacked duplicates."""
    page.goto(f"{live_server}/#leads")
    page.wait_for_selector("table.data-table")
    assert page.locator("table.data-table").count() == 1, "table should render exactly once on initial load"

    # Open the first lead's record panel and trigger a real research run.
    page.locator("table.data-table tbody tr").first.click()
    page.wait_for_selector("#sidePanel.open")
    run_button = page.locator("#panelActions button", has_text="Research")
    run_button.click()

    # Wait for the toast confirming the pipeline finished, which only fires
    # after the table has already been re-rendered.
    page.wait_for_selector(".toast", timeout=10000)
    page.wait_for_timeout(300)  # let any (buggy) extra render finish painting

    assert page.locator("table.data-table").count() == 1, (
        "exactly one table.data-table should exist after an in-panel action — "
        "more than one means a view function was called directly instead of "
        "going through the render() dispatcher"
    )

    # Close the panel, then run it again to make sure the count doesn't creep
    # up on repeated actions.
    page.locator("#panelClose").click()
    page.wait_for_selector("#sidePanel:not(.open)")
    page.locator("table.data-table tbody tr").first.click()
    page.wait_for_selector("#sidePanel.open")
    page.locator("#panelActions button", has_text="Research").click()
    page.wait_for_selector(".toast", timeout=10000)
    page.wait_for_timeout(300)
    assert page.locator("table.data-table").count() == 1


def test_approval_panel_is_read_only_on_repeated_open(live_server, page):
    """Regression test for Bug 2: opening the approval panel repeatedly for
    the same lead must not change which action is shown, and must not add
    rows to the activity log (our proxy for "did the pipeline actually
    re-run and insert something")."""
    # lead_002 reliably resolves to ACT / a proposed action in the fixtures.
    lead_id = "lead_002"
    requests.post(f"{live_server}/api/leads/{lead_id}/run", timeout=5)

    activity_before = requests.get(f"{live_server}/api/leads/{lead_id}/activity", timeout=5).json()["activity"]
    action_before = requests.get(f"{live_server}/api/leads/{lead_id}/action", timeout=5).json()["action"]
    assert action_before is not None

    page.goto(f"{live_server}/#actions")
    page.wait_for_load_state("networkidle")

    seen_action_ids = set()
    for _ in range(3):
        page.locator(f'[data-lead-id="{lead_id}"]').first.click()
        page.wait_for_selector("#sidePanel.open")
        page.wait_for_selector(".approval-cta, .empty-state", timeout=10000)
        action_id_el = page.locator(".panel-title")
        seen_action_ids.add(action_id_el.text_content())
        page.locator("#panelClose").click()
        page.wait_for_timeout(150)

    activity_after = requests.get(f"{live_server}/api/leads/{lead_id}/activity", timeout=5).json()["activity"]
    action_after = requests.get(f"{live_server}/api/leads/{lead_id}/action", timeout=5).json()["action"]

    assert len(activity_after) == len(activity_before), (
        f"activity log grew from {len(activity_before)} to {len(activity_after)} rows just from "
        "opening the approval panel 3 times — it should be read-only"
    )
    assert action_after["id"] == action_before["id"], "action_id changed just from viewing the panel"
