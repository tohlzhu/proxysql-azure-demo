"""Live browser test: run against make demo-local, never substitute API results."""

import os

import pytest

pytestmark = pytest.mark.web


@pytest.mark.skipif(
    not os.getenv("DEMO_BASE_URL"), reason="Set DEMO_BASE_URL for live browser test"
)
def test_console_success_rejection_and_restore():
    from playwright.sync_api import sync_playwright

    url = os.environ["DEMO_BASE_URL"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        page.locator("#scenario").select_option("qps_above")
        page.locator("#duration").fill("3")
        page.locator("#start-run").click()
        page.wait_for_function(
            "() => Number(document.querySelector('#success-count').textContent) > 0 "
            "&& Number(document.querySelector('#rejected-count').textContent) > 0",
            timeout=30000,
        )
        page.reload()
        page.wait_for_function(
            "() => document.querySelector('#run-status').textContent.includes('completed')",
            timeout=30000,
        )
        with page.expect_download() as download:
            page.locator("#download-run").click()
        import json

        result = json.loads(download.value.path().read_text())
        assert result["schema_version"] == 1
        assert result["status"] == "completed"
        assert result["summary"]["success"] > 0
        assert result["summary"]["rate_limited"] > 0
        assert result["summary"]["unavailable"] == 0
        assert result["summary"]["total"] == sum(result["summary"]["status_codes"].values())
        assert not errors
        browser.close()
