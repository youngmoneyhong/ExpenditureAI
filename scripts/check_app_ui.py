"""Local browser smoke test. Requires optional playwright and installed Edge."""

from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    output = Path(__file__).resolve().parents[1] / ".statement-test"
    output.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            headless=True,
        )
        try:
            for name, width, height in [("desktop", 1440, 1000), ("mobile", 390, 844)]:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto("http://localhost:8501", wait_until="networkidle")
                page.get_by_test_id("stFileUploader").wait_for()
                assert page.get_by_test_id("stException").count() == 0
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(output / f"app-{name}.png"), full_page=True)
                page.close()
                print(f"{name}: upload screen loaded, no app exceptions or page overflow")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
