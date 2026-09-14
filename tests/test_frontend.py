import socket
import threading
import time
from pathlib import Path
import pytest
import uvicorn
from playwright.sync_api import sync_playwright, expect
from app.main import create_app


@pytest.fixture
def web_url(context):
    context.settings.demo_auth = True
    context.settings.api_users["admin"]["tenant"] = "demo"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(context), log_level="error",
        timeout_graceful_shutdown=5, timeout_keep_alive=1))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)
    sock.close()
    assert not thread.is_alive()


@pytest.mark.browser
def test_workbench_stream_reload_mobile_and_manual_queue(web_url, context):
    output = Path(__file__).resolve().parent.parent / "reports" / "screenshots"
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(web_url)
        expect(page.locator(".ticket-item")).to_have_count(6)
        page.locator("#ticket-tab").click()
        page.locator('[data-ticket="T20260203002"]').click()
        page.locator("#process-btn").click()
        expect(page.locator("#run-status")).to_have_text("审核通过", timeout=15000)
        expect(page.locator("#proposal")).to_contain_text("19.95")
        expect(page.locator("#tool-evidence")).to_contain_text("calculate_compensation")
        expect(page.locator("#timeline li")).to_have_count(7)
        page.locator("#accept-btn").click()
        expect(page.locator("#feedback-status")).to_have_text("已采纳建议")
        page.screenshot(path=str(output / "desktop.png"), full_page=True)
        page.reload()
        expect(page.locator(".ticket-item")).to_have_count(6)
        page.locator("#ticket-tab").click()
        page.locator('[data-ticket="T20260203002"]').click()
        expect(page.locator("#run-status")).to_have_text("审核通过")
        expect(page.locator("#feedback-status")).to_have_text("已采纳建议")
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#menu-btn").click()
        expect(page.locator("#sidebar")).to_have_class("sidebar open")
        page.locator('[data-ticket="T20260203002"]').click()
        expect(page.locator("#sidebar")).to_have_class("sidebar")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(output / "mobile.png"), full_page=True)
        context.llm.bad = True
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator('[data-ticket="T20260206004"]').click()
        page.locator("#process-btn").click()
        expect(page.locator("#run-status")).to_have_text("待人工复核", timeout=15000)
        expect(page.locator("#accept-btn")).to_be_disabled()
        original_cookies = page.context.cookies()
        page.locator("#staff-btn").click()
        page.locator("#staff-token").fill("admin")
        page.locator("#staff-form button[type=submit]").click()
        expect(page.locator("#staff-dialog")).not_to_be_visible()
        page.locator("#manual-tab").click()
        page.locator("[data-claim]").click()
        page.locator("[data-decide]").click()
        page.locator("#decision-note").fill("人工拒绝无依据的超额退款")
        page.locator("#decision-outcome").select_option("rejected")
        page.locator("#decision-form button[type=submit]").click()
        expect(page.locator("#manual-list")).to_contain_text("已复核")
        page.context.add_cookies(original_cookies)
        page.reload()
        expect(page.locator(".ticket-item")).to_have_count(6)
        page.locator("#ticket-tab").click()
        page.locator('[data-ticket="T20260206004"]').click()
        expect(page.locator("#run-status")).to_have_text("人工已复核")
        expect(page.locator("#review")).to_contain_text("人工拒绝无依据的超额退款")
        assert not errors, errors
        browser.close()


@pytest.mark.browser
def test_customer_chat_and_ticket_management_desktop_mobile(web_url, context):
    from test_chat_and_tickets import CustomerLLM
    context.llm = CustomerLLM()
    output = Path(__file__).resolve().parent.parent / "reports" / "screenshots"
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width":1440,"height":1000})
        errors=[]
        page.on("pageerror",lambda error: errors.append(str(error)))
        page.goto(web_url)
        expect(page.locator("#chat-view")).to_be_visible()
        expect(page.locator("#chat-input")).to_be_enabled()
        page.locator("#chat-input").fill("我要取消未发货订单")
        page.locator("#chat-send-btn").click()
        expect(page.locator(".chat-message.assistant .chat-bubble").last).to_contain_text("订单号")
        expect(page.locator("#chat-input")).to_be_enabled()
        page.locator("#chat-input").fill("SO20260201005")
        page.locator("#chat-send-btn").click()
        expect(page.locator(".chat-message.assistant .chat-bubble").last).to_contain_text("89.00",timeout=15000)
        expect(page.locator(".chat-message.user")).to_have_count(2)
        expect(page.locator("#chat-send-btn")).to_be_enabled()
        page.screenshot(path=str(output/"chat-desktop.png"))
        page.locator("[data-open-ticket]").click()
        expect(page.locator("#run-status")).to_have_text("审核通过")
        expect(page.locator("#edit-ticket-btn")).to_be_disabled()
        page.locator("#new-ticket-btn").click()
        page.locator("#ticket-order").select_option("SO20260201005")
        page.locator("#ticket-problem").fill("新工单：取消未发货订单")
        page.locator("#ticket-auto-run").uncheck()
        page.locator("#save-ticket-btn").click()
        expect(page.locator("#ticket-dialog")).not_to_be_visible()
        created=page.locator("#ticket-title").inner_text()
        expect(page.locator("#edit-ticket-btn")).to_be_enabled()
        page.locator("#edit-ticket-btn").click()
        page.locator("#ticket-problem").fill("修改后的售后问题")
        page.locator("#save-ticket-btn").click()
        expect(page.locator("#ticket-description")).to_have_text("修改后的售后问题")
        page.locator("#delete-ticket-btn").click()
        page.locator("#confirm-delete-btn").click()
        expect(page.locator("#delete-dialog")).not_to_be_visible()
        page.locator("#trash-btn").click()
        expect(page.locator("#trash-list")).to_contain_text(created)
        page.locator("[data-restore-ticket]").click()
        expect(page.locator("#trash-list")).to_contain_text("回收站为空")
        page.locator("#chat-tab").click()
        page.set_viewport_size({"width":390,"height":844})
        page.wait_for_function("document.querySelector('#sidebar').getBoundingClientRect().right <= 0")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(output/"chat-mobile.png"))
        page.reload()
        expect(page.locator(".chat-message.user")).to_have_count(2)
        expect(page.locator(".chat-message.assistant .chat-bubble").last).to_contain_text("89.00")
        page.locator("#delete-chat-btn").click()
        page.locator("#confirm-delete-btn").click()
        expect(page.locator("#delete-dialog")).not_to_be_visible()
        expect(page.locator(".conversation-item")).to_have_count(0)
        assert not errors,errors
        browser.close()
