"""Jev-Browser: drive a real browser with TypeSafe's Jev.

Given a URL and a task, opens Chromium via Playwright and loops:
snapshot interactive elements -> ask Jev which action to take -> act.

Jev questions per step (one request):
  next_action   Choice over element ids + done/stuck
  task_complete Noul - is the task complete on the current page
  risky         Noul - would this action purchase/delete/submit personal data

Safety: low confidence or stuck -> pause for user; risky -> typed
confirmation; fields that cannot be confidently filled are flagged and
left empty.
"""

import argparse
import json
import sys
import time

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("playwright is not installed: pip install playwright && playwright install chromium")
    sys.exit(2)

try:
    from typesafe_sdk import Choice, Noul, TypeSafeClient
except ImportError:
    print("typesafe-sdk is not installed: pip install typesafe-sdk")
    sys.exit(2)

CONFIDENCE_FLOOR = 0.3
RISK_THRESHOLD = 0.5
COMPLETE_THRESHOLD = 0.9
SPECIAL_OPTIONS = {"done", "stuck"}

INTERACTIVE_CSS = (
    "a[href], button, input, select, textarea, "
    "[role='button'], [role='link'], [role='checkbox'], [role='radio'], "
    "[role='switch'], [role='tab'], [role='menuitem'], [role='combobox'], "
    "[role='searchbox'], [role='textbox']"
)

_SNAPSHOT_JS = """
(css) => Array.from(document.querySelectorAll(css)).map((e, i) => {
    const r = e.getBoundingClientRect();
    const st = getComputedStyle(e);
    const visible = r.width > 0 && r.height > 0
        && st.visibility !== 'hidden'
        && st.display !== 'none'
        && e.getAttribute('aria-hidden') !== 'true'
        && e.offsetParent !== null;
    return {
        i: i,
        visible: visible,
        tag: e.tagName.toLowerCase(),
        roleAttr: e.getAttribute('role') || '',
        type: e.getAttribute('type') || '',
        label: e.getAttribute('aria-label') || '',
        text: ((e.innerText || e.textContent || '') + '').trim().slice(0, 100),
        ph: e.placeholder || '',
        val: (e.value === undefined ? '' : String(e.value)).slice(0, 80),
        title: e.title || '',
        href: (e.tagName === 'A' && e.href) ? e.href.slice(0, 120) : ''
    };
}).filter(e => e.visible)
"""

_ROLE_BY_TAG = {
    "a": "link",
    "button": "button",
    "select": "combobox",
    "textarea": "textbox",
}


def _input_role(type_attr):
    return {
        "search": "searchbox",
        "checkbox": "checkbox",
        "radio": "radio",
        "button": "button",
        "submit": "button",
        "reset": "button",
    }.get(type_attr, "textbox")


def snapshot_elements(page, cap=80):
    """Return numbered interactive elements from the live DOM."""
    raw = page.evaluate(_SNAPSHOT_JS, INTERACTIVE_CSS)
    elements = []
    for item in raw[:cap]:
        role = item["roleAttr"] if item["roleAttr"] in (
            "button", "link", "checkbox", "radio", "switch", "tab",
            "menuitem", "combobox", "searchbox", "textbox",
        ) else None
        if role is None:
            role = _ROLE_BY_TAG.get(item["tag"]) or _input_role(item["type"])
        name = item["label"] or item["text"] or item["ph"] or item["title"] or item["val"]
        el = {
            "id": f"el_{len(elements) + 1}",
            "role": role,
            "name": name.strip()[:120],
            "value": item["val"] if role in ("textbox", "searchbox") and item["val"] else None,
            "href": item["href"] or None,
            "dom_index": item["i"],
        }
        elements.append(el)
    return elements


def describe_options(elements, exclude_ids=()):
    """Build Choice criteria: one entry per element plus done/stuck."""
    criteria = {}
    for el in elements:
        if el["id"] in exclude_ids:
            continue
        desc = f"{el['role']}"
        if el["name"]:
            desc += f" labeled '{el['name']}'"
        if el["value"]:
            desc += f" current value '{el['value']}'"
        if el["href"]:
            desc += f" pointing to {el['href']}"
        criteria[el["id"]] = desc
    criteria["done"] = "The task appears complete; no further action is needed"
    criteria["stuck"] = "No visible element can advance the task"
    return criteria


def element_state(elements, url, title, task, guidance, history):
    state = {
        "task": task,
        "current_url": url,
        "page_title": title,
        "actions_taken_so_far": history,
        "interactive_elements": elements,
    }
    if guidance:
        state["user_guidance"] = guidance
    return state


def ask_jev(client, state, elements, exclude_ids=()):
    response = client.system_one(
        state=state,
        questions={
            "next_action": Choice(
                instructions=(
                    "You are controlling a browser to complete the task. "
                    "Check actions_taken_so_far: if the task has already been "
                    "carried out on the current page, choose done. Otherwise "
                    "pick the element that best advances the task now, or "
                    "stuck if nothing here can."
                ),
                criteria=describe_options(elements, exclude_ids),
            ),
            "task_complete": Noul(
                instructions="Based on the current page, has the task already been completed?"
            ),
            "risky": Noul(
                instructions=(
                    "If the next action were taken, would it perform a "
                    "destructive or consequential action such as a purchase, "
                    "payment, deletion, account change, or submitting "
                    "personal or financial data?"
                ),
            ),
        },
    )
    answers = response.answers
    return (
        answers["next_action"].choice,
        answers["next_action"].confidence,
        answers["task_complete"].noul,
        answers["risky"].noul,
    )


def element_value_kind(el, task):
    """Guess what to type; return None when not confidently fillable."""
    name = (el.get("name") or "").lower()
    if el["role"] not in ("textbox", "searchbox"):
        return None
    if "search" in name or "query" in name or "find" in name:
        return task
    if "password" in name:
        return None
    if "email" in name:
        return None
    if any(w in name for w in ("phone", "card", "address", "ssn", "name")):
        return None
    return task


def pause_for_user(message):
    print(f"\n[PAUSED] {message}")
    try:
        return input("Type guidance for Jev (or 'abort'): ").strip()
    except (EOFError, KeyboardInterrupt):
        return "abort"


def main():
    parser = argparse.ArgumentParser(description="Drive a browser with Jev decisions.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--steps", type=int, default=25)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--guidance", default=None, help="User guidance from a previous paused run")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    log = {"steps": [], "unfilled_fields": [], "status": None}
    status = None
    history = []
    failed_ids = {}

    try:
        with sync_playwright() as pw, TypeSafeClient() as client:
            browser = pw.chromium.launch(headless=args.headless)
            page = browser.new_page()
            try:
                page.goto(args.url, timeout=30000, wait_until="domcontentloaded")
            except PWTimeout:
                print("Navigation timed out; continuing with whatever loaded.")
            page.wait_for_timeout(1500)

            for step in range(1, args.steps + 1):
                elements = snapshot_elements(page)
                url = page.url
                title = page.title()

                try:
                    choice, confidence, complete, risky = ask_jev(
                        client,
                        element_state(elements, url, title, args.task, args.guidance, history),
                        elements,
                        exclude_ids=set(failed_ids.get(url, ())),
                    )
                except Exception as exc:
                    log["status"] = "error"
                    log["error"] = str(exc)
                    print(f"TypeSafe API error: {exc}")
                    break

                entry = {"step": step, "url": url, "choice": choice, "confidence": round(confidence, 3)}
                print(f"[step {step}] {url} -> {choice} (confidence {confidence:.2f}, complete {complete:.2f}, risky {risky:.2f})")

                if complete > COMPLETE_THRESHOLD:
                    entry["action"] = "complete"
                    log["steps"].append(entry)
                    status = "success"
                    print(f"Task appears complete on: {url}")
                    break

                if choice in SPECIAL_OPTIONS:
                    entry["action"] = choice
                    log["steps"].append(entry)
                    if choice == "done":
                        status = "success"
                        print("Jev says the task is done.")
                    else:
                        status = "stuck"
                        print(f"Jev is stuck on {url} with task: {args.task}")
                    break

                if confidence < CONFIDENCE_FLOOR:
                    entry["action"] = "paused_low_confidence"
                    log["steps"].append(entry)
                    summary = "\n".join(f"  {e['id']}: {e['role']} '{e['name']}'" for e in elements[:20])
                    g = pause_for_user(
                        f"Low confidence ({confidence:.2f}) choosing the next action on {url}.\nVisible elements:\n{summary}"
                    )
                    if g.lower() == "abort" or not g:
                        status = "aborted"
                        break
                    args.guidance = g
                    continue

                el = next((e for e in elements if e["id"] == choice), None)
                if el is None:
                    status = "stuck"
                    print(f"Jev chose {choice} but it is not on the page anymore.")
                    break
                if risky > RISK_THRESHOLD:
                    entry["action"] = "paused_risky"
                    log["steps"].append(entry)
                    print(f"\n[RISK] Jev thinks '{el['role']} {el['name']}' may purchase, delete, or submit personal data.")
                    confirm = input("Proceed? Type 'yes' to continue, anything else to abort: ").strip().lower()
                    if confirm != "yes":
                        status = "aborted"
                        break

                # Act
                try:
                    locator = page.locator(INTERACTIVE_CSS).nth(el["dom_index"])
                    if el["role"] in ("textbox", "searchbox"):
                        value = element_value_kind(el, args.task)
                        if value is None:
                            entry["action"] = "paused_unfillable"
                            log["steps"].append(entry)
                            log["unfilled_fields"].append({"id": el["id"], "name": el["name"]})
                            print(f"\n[FLAG] Cannot confidently fill field '{el['name']}' (no derivable value from the task).")
                            g = pause_for_user("Fill it in the browser window yourself, then press Enter here (or type 'abort').")
                            if g.lower() == "abort":
                                status = "aborted"
                                break
                            args.guidance = g
                        else:
                            locator.fill(value, timeout=5000)
                            entry["action"] = f"fill '{value[:40]}'"
                    elif el["role"] in ("checkbox", "radio", "switch"):
                        locator.check(timeout=5000)
                        entry["action"] = "check"
                    else:
                        locator.click(timeout=5000)
                        entry["action"] = "click"
                except PWTimeout:
                    entry["action"] = "error: element not actionable (hidden or stale)"
                    failed_ids.setdefault(url, []).append(el["id"])
                    print(f"Action failed (stale/hidden element {el['id']}). Excluding it and re-snapshotting.")
                except Exception as exc:
                    if "Target" in type(exc).__name__ or "closed" in str(exc).lower():
                        raise
                    entry["action"] = f"error: {exc}"
                    print(f"Action failed: {exc}")

                log["steps"].append(entry)
                if entry["action"] in ("click", "check") or (entry["action"] or "").startswith("fill "):
                    history.append({
                        "step": step,
                        "url_before": url,
                        "element": f"{el['id']} ({el['role']} '{el['name']}')",
                        "action": entry["action"],
                    })
                if status is None:
                    page.wait_for_timeout(1200)
            else:
                status = "limit"
                print(f"Reached step limit ({args.steps}).")

            try:
                browser.close()
            except Exception:
                pass

    except Exception as exc:
        if "Target" in type(exc).__name__ or "closed" in str(exc).lower():
            status = status or "aborted"
            print("\nBrowser window was closed before the run finished.")
        else:
            status = status or "error"
            log["error"] = str(exc)
            print(f"\nUnexpected error: {exc}")

    log["status"] = status
    print("\n=== RESULT ===")
    print(json.dumps({"status": status, "unfilled_fields": log["unfilled_fields"]}, indent=2))
    return 0 if status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
