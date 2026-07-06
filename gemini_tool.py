"""
gemini_tool.py
--------------
Opens Google Gemini in a browser, submits prompts, and captures responses.

═══════════════════════════════════════════════════════════════════════════════
INTERACTIVE MODE  (recommended — keeps the browser open)
═══════════════════════════════════════════════════════════════════════════════

  Keeps a single browser session alive and reads prompts in a loop:

      python gemini_tool.py --interactive

  On first launch (no saved cookies), the browser opens and you're prompted
  to log in.  Cookies are saved automatically so future runs skip login.

  Type prompts at the "gemini> " prompt.  Special commands:
      /new     — start a fresh Gemini chat
      /quit    — exit (browser closes)
      Ctrl+C   — same as /quit

═══════════════════════════════════════════════════════════════════════════════
CALLABLE TOOL MODE
═══════════════════════════════════════════════════════════════════════════════

  From another Python script or shell pipeline:

      result = subprocess.check_output(
          ["python", "gemini_tool.py", "What is the capital of France?"],
          text=True,
      )
      # `result` contains only the response text — no banners, no logging.

  From the command line (quiet by default when stdout is piped):

      python gemini_tool.py "Explain quicksort" | tee response.txt

  Force human-readable banners even when piped:

      python gemini_tool.py --verbose "Hello"

═══════════════════════════════════════════════════════════════════════════════
LOGIN & COOKIE MANAGEMENT
═══════════════════════════════════════════════════════════════════════════════

  First-time setup — opens a browser, waits for you to log in, saves cookies:

      python gemini_tool.py --login

  Cookies are saved to ~/.gemini_cookies.json (or --cookie-file PATH).
  All subsequent runs load those cookies automatically — no login needed.

  If you run ANY mode without saved cookies, the tool will automatically
  open the browser and prompt you to log in (no separate --login step needed).

  Force a fresh login (overwrite stale cookies):

      python gemini_tool.py --login --force

═══════════════════════════════════════════════════════════════════════════════
BATCH MODE
═══════════════════════════════════════════════════════════════════════════════

      python gemini_tool.py --batch prompts.txt --output results.json
      python gemini_tool.py --batch prompts.txt --output results.json --resume

═══════════════════════════════════════════════════════════════════════════════
PYTHON API  (import directly instead of shelling out)
═══════════════════════════════════════════════════════════════════════════════

      from gemini_tool import GeminiSession

      with GeminiSession() as g:
          answer = g.prompt("What is 2+2?")
          print(g.prompt("Follow-up question"))   # same browser session

Dependencies:
    pip install selenium undetected-chromedriver pyperclip
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Optional

from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    NoSuchElementException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    import pyperclip
    _pyperclip_available = True
except ImportError:
    pyperclip = None
    _pyperclip_available = False


def _test_pyperclip() -> bool:
    """Test if pyperclip is working and returns text."""
    if not _pyperclip_available:
        return False
    try:
        test_str = f"gemini_tool_test_{time.time()}"
        pyperclip.copy(test_str)
        time.sleep(0.1)
        result = pyperclip.paste()
        if isinstance(result, str) and test_str in result:
            return True
        return False
    except Exception:
        return False


_pyperclip_working = _test_pyperclip()
if not _pyperclip_working:
    pyperclip = None

try:
    import undetected_chromedriver as uc
except ImportError:
    raise SystemExit("Please install: pip install undetected-chromedriver")

# ── Constants ────────────────────────────────────────────────────────────────
GEMINI_URL = "https://gemini.google.com/app"
CHECKPOINT_FILE = "gemini_checkpoint.json"
LOG_FILE = "gemini_tool.log"
DEFAULT_COOKIE_FILE = os.path.join(os.path.expanduser("~"), ".gemini_cookies.json")

PAGE_TIMEOUT = 30
RESPONSE_TIMEOUT = 300  # Gemini can take a while to generate (5 min for long prompts)
LOGIN_POLL_INTERVAL = 2  # seconds between login-detection polls
LOGIN_TIMEOUT = 300  # 5 minutes to complete manual login
BETWEEN_PROMPTS_MIN = 3.0
BETWEEN_PROMPTS_MAX = 6.0

# ── Logging ──────────────────────────────────────────────────────────────────
# File handler always active; console handler only in verbose mode.

class _SafeFlushFileHandler(logging.FileHandler):
    """FileHandler that swallows flush() errors (Google Drive VFS on Windows)."""
    def flush(self):
        try:
            super().flush()
        except OSError:
            pass

class _SafeFlushStreamHandler(logging.StreamHandler):
    """StreamHandler that swallows flush() errors."""
    def flush(self):
        try:
            super().flush()
        except OSError:
            pass

_file_handler = _SafeFlushFileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

log = logging.getLogger("gemini_tool")
log.setLevel(logging.INFO)
log.addHandler(_file_handler)

_console_handler: Optional[logging.StreamHandler] = None


def _enable_console_logging() -> None:
    global _console_handler
    if _console_handler is None:
        _console_handler = _SafeFlushStreamHandler(sys.stderr)
        _console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        log.addHandler(_console_handler)


def _is_verbose_mode() -> bool:
    """True when we should show human-readable banners and console logs."""
    return sys.stdout.isatty()


# ── Cookie persistence ───────────────────────────────────────────────────────

def save_cookies(driver: uc.Chrome, cookie_file: str) -> None:
    """Export the browser's cookies for gemini.google.com to a JSON file."""
    cookies = driver.get_cookies()
    relevant = [
        c for c in cookies
        if any(
            domain in (c.get("domain", ""))
            for domain in [".google.com", "gemini.google.com", ".googleapis.com"]
        )
    ]
    path = Path(cookie_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(relevant, indent=2, default=str), encoding="utf-8")
    log.info(f"Saved {len(relevant)} cookies to {cookie_file}")


def load_cookies(driver: uc.Chrome, cookie_file: str) -> bool:
    """Load cookies from file into the browser. Returns True if cookies existed."""
    path = Path(cookie_file)
    if not path.exists():
        log.info(f"No cookie file found at {cookie_file}")
        return False

    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"Failed to read cookie file: {exc}")
        return False

    if not cookies:
        return False

    try:
        driver.get("https://gemini.google.com/")
        time.sleep(1)
    except WebDriverException:
        pass

    loaded = 0
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        cookie.pop("sameSite", None)
        cookie.pop("storeId", None)
        if "expiry" in cookie:
            try:
                cookie["expiry"] = int(cookie["expiry"])
            except (ValueError, TypeError):
                cookie.pop("expiry", None)
        try:
            driver.add_cookie(cookie)
            loaded += 1
        except Exception:
            pass

    log.info(f"Loaded {loaded}/{len(cookies)} cookies from {cookie_file}")
    return loaded > 0


# ══════════════════════════════════════════════════════════════════════════════
# DYNAMIC ELEMENT DISCOVERY  (replaces all hardcoded selector lists)
# ══════════════════════════════════════════════════════════════════════════════
#
# Instead of brittle CSS selectors that break when Gemini renames classes,
# these functions use JavaScript to locate elements by *functional traits*:
#   - The input is a visible, non-tiny contenteditable element (or textarea).
#   - The send button is a <button> near the input that has a send-like icon
#     or label.
#   - Responses are the largest text blocks that appeared after we submitted.
#
# Each function returns a Selenium WebElement (or list), never a selector
# string.  If heuristics all fail, we fall back to very broad queries and
# pick the best match.
# ══════════════════════════════════════════════════════════════════════════════

# ── JS: find the prompt input ────────────────────────────────────────────────

_JS_FIND_INPUT = """
// Strategy 1: aria-label hints (most reliable when present)
var ariaHints = [
    'prompt', 'message', 'ask', 'enter a prompt', 'type something',
    'talk to gemini', 'enter text', 'chat'
];
var allEditable = document.querySelectorAll(
    '[contenteditable="true"], textarea, input[type="text"]'
);
for (var i = 0; i < allEditable.length; i++) {
    var el = allEditable[i];
    if (el.offsetWidth < 50 || el.offsetHeight < 15) continue;  // too small
    var label = (
        (el.getAttribute('aria-label') || '') +
        (el.getAttribute('placeholder') || '') +
        (el.getAttribute('data-placeholder') || '')
    ).toLowerCase();
    for (var j = 0; j < ariaHints.length; j++) {
        if (label.indexOf(ariaHints[j]) !== -1) return el;
    }
}

// Strategy 2: any visible contenteditable with a role hinting at input
var ceAll = document.querySelectorAll('[contenteditable="true"]');
for (var i = 0; i < ceAll.length; i++) {
    var el = ceAll[i];
    if (el.offsetWidth < 50 || el.offsetHeight < 15) continue;
    var role = (el.getAttribute('role') || '').toLowerCase();
    if (role === 'textbox' || role === 'combobox') return el;
}

// Strategy 3: the largest visible contenteditable (likely the main input)
var best = null, bestArea = 0;
for (var i = 0; i < ceAll.length; i++) {
    var el = ceAll[i];
    var rect = el.getBoundingClientRect();
    if (rect.width < 50 || rect.height < 15) continue;
    // Prefer elements lower on the page (inputs are typically at the bottom)
    var area = rect.width * rect.height;
    var bonus = rect.top;  // higher top = further down = more likely input
    var score = area + bonus * 2;
    if (score > bestArea) { bestArea = score; best = el; }
}
if (best) return best;

// Strategy 4: fallback to any visible textarea
var tas = document.querySelectorAll('textarea');
for (var i = 0; i < tas.length; i++) {
    if (tas[i].offsetWidth > 50) return tas[i];
}

return null;
"""

# ── JS: find the send button ────────────────────────────────────────────────

_JS_FIND_SEND_BUTTON = """
var buttons = document.querySelectorAll('button');
var inputEl = arguments[0];  // pass the input element for proximity checks

// Score each button; return the best
var best = null, bestScore = -1;

for (var i = 0; i < buttons.length; i++) {
    var btn = buttons[i];
    if (btn.offsetWidth < 10 || btn.offsetHeight < 10) continue;
    if (btn.disabled) continue;

    var score = 0;
    var text = (
        (btn.textContent || '') +
        (btn.getAttribute('aria-label') || '') +
        (btn.getAttribute('data-tooltip') || '') +
        (btn.getAttribute('mattooltip') || '') +
        (btn.getAttribute('title') || '')
    ).toLowerCase();

    // Strong signals in label
    if (text.indexOf('send') !== -1) score += 100;
    if (text.indexOf('submit') !== -1) score += 80;

    // SVG send-arrow icon heuristic: button contains an SVG with a
    // path that looks like a send arrow (has a 'polygon' or 'path')
    var svg = btn.querySelector('svg');
    if (svg) {
        var paths = svg.querySelectorAll('path, polygon');
        if (paths.length > 0 && paths.length < 4) score += 30;
    }

    // Class name hints
    var cls = (btn.className || '').toLowerCase();
    if (cls.indexOf('send') !== -1) score += 60;
    if (cls.indexOf('submit') !== -1) score += 50;

    // Proximity to the input element
    if (inputEl) {
        var inputRect = inputEl.getBoundingClientRect();
        var btnRect = btn.getBoundingClientRect();
        var dist = Math.hypot(
            btnRect.left - inputRect.right,
            btnRect.top - inputRect.top
        );
        // Bonus if the button is near the right edge of the input
        if (dist < 200) score += 40;
        if (dist < 100) score += 30;
    }

    if (score > bestScore) { bestScore = score; best = btn; }
}

return best;
"""

# ── JS: count response containers ───────────────────────────────────────────

_JS_COUNT_RESPONSES = """
// Look for elements that are clearly model responses.
// We try multiple strategies and return the highest count found.

var strategies = [
    // Custom elements (Gemini web components — multiple naming conventions)
    function() { return document.querySelectorAll('model-response').length; },
    function() { return document.querySelectorAll('message-content').length; },
    function() { return document.querySelectorAll('chat-message').length; },
    function() { return document.querySelectorAll('response-container').length; },
    function() { return document.querySelectorAll('immersive-entry-chip').length; },

    // Role-based (ARIA)
    function() {
        return document.querySelectorAll(
            '[data-message-author-role="model"], [data-author-role="model"], ' +
            '[data-message-role="model"], [role="article"]'
        ).length;
    },

    // Turn-based containers (newer Gemini uses turn-based wrappers)
    function() {
        var turns = document.querySelectorAll(
            '[class*="turn"], [class*="Turn"], [class*="conversation-turn"]'
        );
        var count = 0;
        for (var i = 0; i < turns.length; i++) {
            // Only count turns that look like assistant/model turns
            var el = turns[i];
            var cls = (el.className || '').toLowerCase();
            var txt = el.textContent || '';
            if (txt.length > 20 && cls.indexOf('user') === -1) count++;
        }
        return count;
    },

    // Class heuristics: containers with "response" or "answer" in class
    function() {
        var all = document.querySelectorAll(
            '[class*="response"], [class*="answer"], [class*="Reply"]'
        );
        var containers = [];
        for (var i = 0; i < all.length; i++) {
            var el = all[i];
            if (el.offsetHeight > 40 && el.textContent.trim().length > 20) {
                containers.push(el);
            }
        }
        return containers.length;
    },

    // Markdown blocks (each response typically has one)
    function() {
        return document.querySelectorAll(
            '.markdown, [class*="markdown"], [class*="response-text"], ' +
            '[class*="generated"], [class*="model-text"]'
        ).length;
    },
];

var maxCount = 0;
for (var i = 0; i < strategies.length; i++) {
    try {
        var c = strategies[i]();
        if (c > maxCount) maxCount = c;
    } catch(e) {}
}
return maxCount;
"""

# ── JS: extract latest response text ────────────────────────────────────────

_JS_EXTRACT_RESPONSE = """
// Returns the FULL visible body text of the page, then lets the Python
// caller slice past its `<<<__GEMINI_RESPONSE_BELOW__>>>` end-of-prompt
// marker to recover the model reply.
//
// History: this function used to do clever DOM querying ("find the last
// <model-response>", "find the largest <p> under 50K chars", etc.) but
// every variant either returned Gemini's chat-composer chrome buttons,
// the user prompt's 14-37K char paragraph, or the bare "Gemini said"
// label, depending on how the DOM had drifted. Recovering the response
// from the full page text via a unique marker is more robust than chasing
// Gemini's evolving DOM.
//
// We keep the structured strategies below as best-effort fast-paths —
// when one of them DOES find a reasonably-sized model response, we save
// the caller from having to slice through 100K of body text. If they all
// miss, Strategy 9 returns the body text and Python's _clean_json takes
// over.

// As of May 2026 the Gemini DOM uses:
//   <response-container>   — the model reply wrapper (exactly 1 per chat)
//   <user-query>           — the user message wrapper
// (The older `<model-response>` and `<message-content>` tags no longer
// exist; legacy selectors all returned 0 in our probe.)
//
// Strategy: grab the LAST <response-container> directly. If it has text,
// return that. If it's empty, fall through to body.innerText so the
// Python marker-slice can still recover gracefully.

// Try Gemini's model-reply elements in order of specificity. DOM dump
// from a live capture (see junk/html.txt, May 2026) showed the response
// rendered as:
//
//   <model-response>
//     <response-container>
//       <message-content id="message-content-id-...">
//         <code-block>...JSON...</code-block>
//       </message-content>
//     </response-container>
//   </model-response>
//
// IMPORTANT: when ANY of these elements exists, we return its text — even
// if it's empty/short. We do NOT fall back to body.innerText in that
// case, because the caller's wait loop watches text length to detect
// streaming, and body.innerText (which contains the user prompt) is
// stable from the moment the prompt is submitted, so it always looks
// "done" — even before Gemini emits its first response token.
//
// Returning the empty/streaming model reply lets the wait loop see length
// grow from 0 → final_length as Gemini streams, and STABLE_SECONDS=3 of
// no change reliably means "Gemini finished".
// Reverted to the May 2026 known-working version. Iterate the model-
// reply custom elements in order of specificity and return the LAST
// match's text, regardless of length. The wait loop watches the
// returned length grow as Gemini streams, so an empty/short response
// keeps polling and a substantive one stabilises and captures.
//
// Filters (BARE_LABELS, isLoadingOnly, marker-fallback) were tried but
// caused worse failures than they solved — when they returned "" the
// wait loop had nothing to anchor on and timed out at 4 minutes.
var selectors = [
    'message-content',
    'response-container',
    'model-response'
];
for (var s = 0; s < selectors.length; s++) {
    var els = document.querySelectorAll(selectors[s]);
    if (els.length) {
        var last = els[els.length - 1];
        // Strategy index = selector index (0=message-content, 1=response-container, 2=model-response)
        return {text: (last.innerText || last.textContent || '').trim(), strategy: s};
    }
}
// No active selector matched — empty fallback before dead-code strategies below.
return {text: '', strategy: -1};

function getText(el) {
    if (!el) return '';
    var txt = (el.innerText || el.textContent || '').trim();
    return txt;
}

function lastNonEmpty(nodeList) {
    for (var i = nodeList.length - 1; i >= 0; i--) {
        var t = getText(nodeList[i]);
        if (t.length > 5) return t;
    }
    return '';
}

// True if any ancestor (or the element itself) looks like a user message
// container — needed because newer Gemini wraps the user prompt in elements
// that may match generic [class*="message"] / [class*="content"] selectors.
function isUserScoped(el) {
    var cur = el;
    var hops = 0;
    while (cur && hops < 8) {
        var cls = ((cur.className && cur.className.toString) ?
                   cur.className.toString() : '').toLowerCase();
        var tag = (cur.tagName || '').toLowerCase();
        if (tag === 'user-query' || tag === 'user-message' ||
            cls.indexOf('user-query') !== -1 ||
            cls.indexOf('user-message') !== -1 ||
            cls.indexOf('user-input') !== -1 ||
            cur.getAttribute && (
                cur.getAttribute('data-message-author-role') === 'user' ||
                cur.getAttribute('data-author-role') === 'user' ||
                cur.getAttribute('data-message-role') === 'user')) {
            return true;
        }
        cur = cur.parentElement;
        hops++;
    }
    return false;
}

// Strategy 0 (NEW, highest priority): the LAST response-class element in
// DOM order that isn't inside a user-message container. In every chat UI
// the model reply comes after the user prompt, so the last response-like
// element is the latest reply.
//
// Two important filters:
//   • SKIP buttons / inputs / labels — Gemini's chat composer has buttons
//     with text like "Stop response" that match [class*="response"].
//   • Require a minimum text length so we don't pick toolbar chrome.
//     A real model JSON reply will be hundreds of chars at minimum.
function isInteractiveChrome(el) {
    var cur = el;
    var hops = 0;
    while (cur && hops < 8) {
        var tag = (cur.tagName || '').toLowerCase();
        var cls = ((cur.className && cur.className.toString) ?
                   cur.className.toString() : '').toLowerCase();
        if (tag === 'button' || tag === 'input' || tag === 'textarea' ||
            tag === 'select' || tag === 'label' || tag === 'form' ||
            tag === 'aside' || tag === 'menu' ||
            cls.indexOf('input-area') !== -1 ||
            cls.indexOf('composer') !== -1 ||
            cls.indexOf('toolbar') !== -1 ||
            cls.indexOf('chat-input') !== -1 ||
            cls.indexOf('prompt-input') !== -1 ||
            cls.indexOf('text-input') !== -1 ||
            cls.indexOf('input-container') !== -1 ||
            cls.indexOf('input-area-container') !== -1 ||
            cls.indexOf('rich-textarea') !== -1 ||
            (cur.getAttribute && (
                cur.getAttribute('role') === 'button' ||
                cur.getAttribute('role') === 'toolbar' ||
                cur.getAttribute('role') === 'textbox' ||
                cur.getAttribute('role') === 'menu' ||
                cur.getAttribute('role') === 'menuitem' ||
                cur.getAttribute('role') === 'navigation'))) {
            return true;
        }
        cur = cur.parentElement;
        hops++;
    }
    return false;
}

// Pass A — STRONG signals: custom tags + ARIA author roles that ONLY
// match model replies. (We removed `<message-content>` here because newer
// Gemini uses that tag for user messages too, which made us return the
// 37K-char user prompt instead of the 5K-char reply.)
var strongResponse = document.querySelectorAll(
    'model-response, ' +
    '[data-message-author-role="model"], [data-author-role="model"], ' +
    '[data-message-role="model"], [data-author-role="assistant"], ' +
    '[data-message-author-role="assistant"]'
);
for (var i = strongResponse.length - 1; i >= 0; i--) {
    var el = strongResponse[i];
    if (isUserScoped(el)) continue;
    if (isInteractiveChrome(el)) continue;
    if (el.getAttribute && el.getAttribute('contenteditable') === 'true') continue;
    var t = getText(el);
    // 50-char floor: skips bare labels like "Gemini said" (11 chars) while
    // still admitting empty-ish replies wrapped in markdown ("```json [] ```").
    // Strategy 3: Pass A — strong signals (model-response, role=model/assistant)
    if (t.length >= 50 && t.length < 30000) return {text: t, strategy: 3};
}

// Pass B — WEAK signals: class-name heuristics. These are noisier (the
// chat composer toolbar has classes like "response-controls" with buttons
// labelled "Stop response"), so require a substantial amount of text.
var weakResponse = document.querySelectorAll(
    'response-container, ' +
    '[class*="model-response"], [class*="ModelResponse"], ' +
    '[class*="response-container"], [class*="assistant-response"], ' +
    '[class*="model-text"], [class*="response-text"], ' +
    '[class*="response-content"], [class*="model-output"]'
);
for (var i = weakResponse.length - 1; i >= 0; i--) {
    var el = weakResponse[i];
    if (isUserScoped(el)) continue;
    if (isInteractiveChrome(el)) continue;
    if (el.getAttribute && el.getAttribute('contenteditable') === 'true') continue;
    var t = getText(el);
    // Real Gemini responses for our use case are 100+ chars; toolbars are
    // typically <80 chars total. 150 is a comfortable cutoff.
    // Strategy 4: Pass B — weak signals (class-name heuristics)
    if (t.length >= 150 && t.length < 200000) return {text: t, strategy: 4};
}

// (Legacy Strategies 1-6 removed — they queried `<message-content>`,
// `<model-response>`, etc. without size or user-scope filters and ended
// up returning the entire 14K-char user prompt for long extract calls.
// Pass A and Pass B above cover their intent with proper filtering.)

// Strategy 7: pick the LAST sufficiently-long text block in DOM order
// that isn't inside a user-message container or chat composer toolbar.
// Reverse iteration prefers the model's reply over the (often-larger)
// user prompt; the chrome-skip filter prevents us from landing on the
// composer's "Stop response / microphone / Edit prompt" buttons.
//
// Important caps:
//   • length >= 100  : skip toolbar text (~60 chars total)
//   • length < 25000 : skip the user message itself, which on long extract
//     prompts is 30K+ chars. The model's actual reply for our use case is
//     2K-15K chars — comfortably under 25K.
var textBlocks = document.querySelectorAll(
    'p, pre, code, blockquote, ' +
    '[class*="markdown"], [class*="response-content"]'
);
for (var i = textBlocks.length - 1; i >= 0; i--) {
    var el = textBlocks[i];
    if (el.getAttribute('contenteditable') === 'true') continue;
    if (isUserScoped(el)) continue;
    if (isInteractiveChrome(el)) continue;
    var t = getText(el);
    // Strategy 7: last sufficiently-long text block in <p>/<pre>/<code>/etc.
    if (t.length >= 100 && t.length < 12000) return {text: t, strategy: 7};
}

// Strategy 8: broadest fallback. Same reverse-order rule — the last large
// non-chrome container is most likely the conversation transcript area
// (and contains the model reply at its tail). Cap at 25K chars to avoid
// landing on a wrapper div that contains the user prompt's 30K-char text.
var candidates = document.querySelectorAll(
    'div[class], section, article, main'
);
for (var i = candidates.length - 1; i >= 0; i--) {
    var el = candidates[i];
    if (el.getAttribute('contenteditable') === 'true') continue;
    if (el.tagName === 'NAV' || el.tagName === 'HEADER') continue;
    if (isUserScoped(el)) continue;
    if (isInteractiveChrome(el)) continue;
    var t = getText(el);
    // Strategy 8: broadest div/section/article/main fallback
    if (t.length >= 100 && t.length < 12000) return {text: t, strategy: 8};
}

// Strategy 9: absolute last resort — return the entire visible body text.
// Yes, this includes Gemini's chrome AND the echoed user prompt. The
// caller (startup_researcher._clean_json) slices past the unique
// `<<<__GEMINI_RESPONSE_BELOW__>>>` marker that we appended to the prompt
// — anything after that marker is necessarily the model reply (or empty).
// Doing this in Python is more robust than guessing which DOM element is
// the response.
// Strategy 9: absolute last resort — entire body.innerText
return {text: (document.body && document.body.innerText) || '', strategy: 9};
"""

# ── JS: check if response is complete ───────────────────────────────────────

_JS_IS_COMPLETE = """
// Check various signals that the response has finished streaming.

var mdBlocks = document.querySelectorAll(
    '.markdown, [class*="markdown"], [class*="generated"], [class*="model-text"]'
);
if (mdBlocks.length) {
    var last = mdBlocks[mdBlocks.length - 1];
    if (last.getAttribute('aria-busy') === 'false') return true;
}

var footers = document.querySelectorAll(
    '[class*="footer"][class*="complete"], [class*="response"][class*="complete"], ' +
    '[class*="done"], [class*="finished"]'
);
if (footers.length > 0) return true;

var cursors = document.querySelectorAll(
    '[class*="cursor"][class*="blink"], [class*="typing"], ' +
    '[class*="loading"][class*="indicator"], [class*="streaming"], ' +
    '[class*="generating"], [class*="spinner"]'
);
if (cursors.length > 0) return false;

var buttons = document.querySelectorAll('button');
for (var i = 0; i < buttons.length; i++) {
    var text = (
        (buttons[i].textContent || '') +
        (buttons[i].getAttribute('aria-label') || '')
    ).toLowerCase();
    if (text.indexOf('stop') !== -1 && text.indexOf('generat') !== -1) {
        return false;
    }
}

var responseContainers = document.querySelectorAll(
    'model-response, message-content, [data-message-author-role="model"], ' +
    'chat-message, response-container, [class*="turn"]:not([class*="user"]), ' +
    '[role="article"]'
);
if (responseContainers.length > 0) {
    var last = responseContainers[responseContainers.length - 1];
    var actionBtns = last.querySelectorAll(
        'button[aria-label*="copy" i], button[aria-label*="share" i], ' +
        'button[aria-label*="thumb" i], button[aria-label*="good" i], ' +
        'button[aria-label*="bad" i], button[aria-label*="like" i]'
    );
    if (actionBtns.length > 0) return true;
    
    // Signal: if last response has substantial text and we're past the initial load
    var txt = last.textContent || last.innerText || '';
    if (txt.trim().length > 50) {
        var editables = document.querySelectorAll('[contenteditable="true"], textarea');
        for (var j = 0; j < editables.length; j++) {
            if (editables[j].offsetWidth > 50 && editables[j].offsetHeight > 15) {
                var inpTxt = editables[j].textContent || editables[j].innerText || '';
                // Input is empty or minimal = response is complete
                if (inpTxt.trim().length < 10) return true;
            }
        }
    }
}

var allEditable = document.querySelectorAll(
    '[contenteditable="true"], textarea'
);
for (var i = 0; i < allEditable.length; i++) {
    var el = allEditable[i];
    if (el.offsetWidth > 50 && el.offsetHeight > 15) {
        var txt = el.textContent || el.innerText || '';
        if (txt.trim().length < 10) return true;
    }
}

return false;
"""

# ── JS: check if chat is ready (logged in) ──────────────────────────────────

_JS_IS_CHAT_READY = """
// Returns true if a prompt input element is visible on the page,
// meaning the user is logged in and the chat interface is loaded.
""" + _JS_FIND_INPUT + """
// (The above script returns the element or null)
"""


# ── Driver ───────────────────────────────────────────────────────────────────

def _create_ephemeral_profile() -> str:
    """Create a fresh temporary Chrome profile directory.
    Returns the path.  Caller is responsible for cleanup."""
    tmpdir = tempfile.mkdtemp(prefix="gemini_ephemeral_")
    log.info(f"Created ephemeral profile: {tmpdir}")
    return tmpdir


def _cleanup_profile(profile_dir: str) -> None:
    """Remove an ephemeral profile directory, ignoring errors."""
    try:
        shutil.rmtree(profile_dir, ignore_errors=True)
        log.info(f"Cleaned up ephemeral profile: {profile_dir}")
    except Exception as exc:
        log.warning(f"Failed to clean up profile {profile_dir}: {exc}")


def _detect_chrome_major() -> Optional[int]:
    """Auto-detect installed Chrome major version on Win/Mac/Linux."""
    import platform, subprocess, re as _re
    plat = platform.system()
    cmds: list[list[str]] = []
    if plat == "Windows":
        cmds = [
            ["reg", "query",
             r"HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon",
             "/v", "version"],
            ["powershell", "-command",
             "(Get-Item 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe')"
             ".VersionInfo.ProductVersion"],
            ["powershell", "-command",
             "(Get-Item 'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe')"
             ".VersionInfo.ProductVersion"],
        ]
    elif plat == "Darwin":
        cmds = [["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "--version"]]
    else:
        cmds = [["google-chrome", "--version"],
                ["google-chrome-stable", "--version"],
                ["chromium-browser", "--version"],
                ["chromium", "--version"]]
    for cmd in cmds:
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL,
                                          timeout=10).decode()
            match = _re.search(r"(\d+)\.\d+\.\d+", out)
            if match:
                ver = int(match.group(1))
                log.info(f"Detected Chrome major version: {ver}")
                return ver
        except Exception:
            continue
    return None


def init_driver(
    chrome_major: Optional[int] = None,
    profile_dir: Optional[str] = None,
    headless: bool = False,
) -> uc.Chrome:
    """Initialise an undetected Chrome instance.

    If *profile_dir* is None, an ephemeral temp directory is created and
    attached as ``driver._ephemeral_profile`` so callers can clean it up.
    """
    ephemeral = profile_dir is None
    profile = profile_dir or _create_ephemeral_profile()
    os.makedirs(profile, exist_ok=True)

    options = uc.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--lang=en-US")
    options.add_argument(f"--user-data-dir={profile}")

    # ── Ephemeral-session flags ──────────────────────────────────────────
    # Disable features that persist state between runs.
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-client-side-phishing-detection")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-extensions")
    # Aggressive cookie / cache isolation
    options.add_argument("--disable-features=NetworkService,NetworkServiceInProcess")
    options.add_argument("--disable-site-isolation-trials")

    if headless:
        options.add_argument("--headless=new")

    version = chrome_major or _detect_chrome_major() or 146
    driver = uc.Chrome(options=options, version_main=version)
    driver.set_page_load_timeout(PAGE_TIMEOUT)
    driver.set_script_timeout(15)  # prevent execute_script from blocking forever
    try:
        driver.minimize_window()
    except Exception:
        pass
    log.info(f"Using Chrome profile at: {profile}")

    # Tag ephemeral profiles so quit_driver() can clean up
    driver._ephemeral_profile = profile if ephemeral else None
    return driver


def browser_pids(driver) -> set:
    """The OS PIDs this driver owns: the Chrome browser (uc sets .browser_pid)
    and the chromedriver service process (selenium's Service.process.pid).
    Captured BEFORE quit() -- quit() nulls these out on the way down, so a
    post-quit read finds nothing. Best-effort: whatever attributes exist."""
    pids = set()
    bpid = getattr(driver, "browser_pid", None)
    if isinstance(bpid, int):
        pids.add(bpid)
    try:
        proc = getattr(getattr(driver, "service", None), "process", None)
        spid = getattr(proc, "pid", None)
        if isinstance(spid, int):
            pids.add(spid)
    except Exception:
        pass
    return pids


def _force_kill_pids(pids) -> None:
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        except Exception:
            pass


def hard_quit(driver) -> None:
    """quit() the driver AND guarantee its Chrome/chromedriver processes die.

    driver.quit() silently fails fairly often on Windows (WinError 6, 'the
    handle is invalid') and leaves the browser processes alive. Over a long run
    these leaked windows pile up and eventually exhaust RAM -- confirmed
    2026-07-06: the Gemini session, restarting on a hang, hit
    `MemoryError()` spawning yet another Chrome and took down the whole run
    (and its watchdog) after ~7h of accumulation. So: capture the PIDs first,
    quit(), then force-kill the process tree for any that survived."""
    pids = browser_pids(driver)
    try:
        driver.quit()
    except Exception:
        pass
    _force_kill_pids(pids)


def quit_driver(driver: uc.Chrome) -> None:
    """Quit the driver (force-killing leaked processes) and clean up any
    ephemeral profile directory."""
    ephemeral = getattr(driver, "_ephemeral_profile", None)
    hard_quit(driver)
    if ephemeral:
        _cleanup_profile(ephemeral)


# ── Login detection ──────────────────────────────────────────────────────────

def _is_chat_ready(driver: uc.Chrome) -> bool:
    """Return True if the Gemini chat input is visible (i.e. we're logged in)."""
    try:
        el = driver.execute_script(_JS_FIND_INPUT)
        return el is not None
    except (TimeoutException, WebDriverException):
        return False
    except Exception:
        return False


def gemini_login(
    driver: uc.Chrome,
    cookie_file: str = DEFAULT_COOKIE_FILE,
    verbose: bool = True,
) -> None:
    """
    Ensure the user is logged in to Gemini.

    Strategy (in order):
      1. Load cookies from file -> navigate to Gemini -> check if chat is ready.
      2. If the Chrome profile already has a session, detect it.
      3. Otherwise, pause and wait for the user to log in manually.
         On success, save cookies to file for next time.
    """
    # ── Attempt 1: cookie file ──
    log.info("Attempting login via saved cookies...")
    had_cookies = load_cookies(driver, cookie_file)

    if had_cookies:
        driver.get(GEMINI_URL)
        for _ in range(12):
            time.sleep(1)
            if _is_chat_ready(driver):
                log.info("Logged in via saved cookies.")
                if verbose:
                    _print_stderr("  ✓ Logged in automatically from saved cookies.")
                save_cookies(driver, cookie_file)
                return

    # ── Attempt 2: existing Chrome profile session ──
    log.info("Cookies didn't work or absent. Trying Chrome profile session...")
    driver.get(GEMINI_URL)
    for _ in range(10):
        time.sleep(1)
        if _is_chat_ready(driver):
            log.info("Logged in via Chrome profile session.")
            if verbose:
                _print_stderr("  ✓ Logged in automatically from browser profile.")
            save_cookies(driver, cookie_file)
            return

    # ── Attempt 3: manual login with auto-detection ──
    if verbose:
        _print_stderr("")
        _print_stderr("=" * 60)
        _print_stderr("  No saved session found — login required.")
        _print_stderr("")
        _print_stderr("  Please log in to your Google account in the browser")
        _print_stderr("  window that just opened.  The tool will detect your")
        _print_stderr("  login automatically — no need to press Enter.")
        _print_stderr("")
        _print_stderr(f"  Cookies will be saved to: {cookie_file}")
        _print_stderr("  so you won't need to log in again.")
        _print_stderr("=" * 60)
        _print_stderr("")

    log.info("Waiting for manual login (polling every %ss, timeout %ss)...",
             LOGIN_POLL_INTERVAL, LOGIN_TIMEOUT)

    deadline = time.time() + LOGIN_TIMEOUT
    while time.time() < deadline:
        time.sleep(LOGIN_POLL_INTERVAL)
        if _is_chat_ready(driver):
            log.info("Manual login detected!")
            if verbose:
                _print_stderr("  ✓ Login detected. Saving session for next time.")
            save_cookies(driver, cookie_file)
            return

    raise TimeoutError(
        f"Login was not completed within {LOGIN_TIMEOUT}s. "
        "Please try again with:  python gemini_tool.py --login"
    )


def _print_stderr(msg: str) -> None:
    """Print to stderr so it never contaminates stdout capture."""
    print(msg, file=sys.stderr)


# ── Prompt / Response ────────────────────────────────────────────────────────

def _find_input(driver: uc.Chrome):
    """Find the prompt input element using JS heuristics."""
    try:
        return driver.execute_script(_JS_FIND_INPUT)
    except (TimeoutException, WebDriverException) as exc:
        log.warning(f"  _find_input failed: {exc.__class__.__name__}")
        return None


def _find_send_button(driver: uc.Chrome, input_el):
    """Find the send button using JS heuristics."""
    try:
        return driver.execute_script(_JS_FIND_SEND_BUTTON, input_el)
    except (TimeoutException, WebDriverException) as exc:
        log.warning(f"  _find_send_button failed: {exc.__class__.__name__}")
        return None


# ── JS: find file input element ────────────────────────────────────────────

_JS_FIND_FILE_INPUT = """
// Find hidden file input elements for uploads
var inputs = document.querySelectorAll('input[type="file"]');
var best = null;
for (var i = 0; i < inputs.length; i++) {
    var el = inputs[i];
    // Hidden inputs are typically display:none or opacity:0
    if (el.offsetWidth > 0 || el.getAttribute('accept')) {
        best = el;
        break;
    }
}
// If no visible one, return any file input
if (!best && inputs.length > 0) best = inputs[0];
return best;
"""

_JS_FIND_UPLOAD_BUTTON = """
// Find the upload/attach button (typically near the input)
var keywords = ['upload', 'attach', 'add', 'file', 'image', 'photo', 'media', 'paperclip', 'clip'];
var buttons = document.querySelectorAll('button, [role="button"], [tabindex="0"]');
var best = null, bestScore = 0;
for (var i = 0; i < buttons.length; i++) {
    var btn = buttons[i];
    if (btn.disabled) continue;
    if (btn.offsetWidth < 10 || btn.offsetHeight < 10) continue;
    var text = (
        (btn.textContent || '') +
        (btn.getAttribute('aria-label') || '') +
        (btn.getAttribute('data-tooltip') || '')
    ).toLowerCase();
    var score = 0;
    for (var j = 0; j < keywords.length; j++) {
        if (text.indexOf(keywords[j]) !== -1) {
            score = keywords.length - j;  // earlier keyword = higher score
            break;
        }
    }
    if (score > bestScore) {
        bestScore = score;
        best = btn;
    }
}
return best;
"""

_JS_CHECK_UPLOAD_READY = """
// Check if upload is available (file chips appear after upload)
var chips = document.querySelectorAll(
    '[class*="chip"], [class*="Chip"], [class*="file-chip"], ' +
    '[class*="attachment"], [class*="Attachment"], ' +
    '[class*="uploaded"], [class*="preview"]'
);
return chips.length > 0;
"""


def _find_file_input(driver: uc.Chrome):
    """Find the hidden file input element."""
    try:
        return driver.execute_script(_JS_FIND_FILE_INPUT)
    except Exception as exc:
        log.warning(f"  _find_file_input failed: {exc}")
        return None


def _find_upload_button(driver: uc.Chrome):
    """Find the upload/attach button."""
    try:
        return driver.execute_script(_JS_FIND_UPLOAD_BUTTON)
    except Exception as exc:
        log.warning(f"  _find_upload_button failed: {exc}")
        return None


def upload_files(driver: uc.Chrome, files: list[str]) -> bool:
    """
    Upload files to Gemini input.

    Args:
        driver: Chrome driver instance
        files: List of file paths to upload

    Returns:
        True if upload succeeded, False otherwise
    """
    if not files:
        return True

    for filepath in files:
        if not os.path.exists(filepath):
            log.error(f"File not found: {filepath}")
            return False

    log.info(f"  Uploading {len(files)} file(s)...")

    # Method 1: Direct file input manipulation
    file_input = _find_file_input(driver)
    if file_input:
        try:
            # Use absolute paths
            abs_paths = [os.path.abspath(f) for f in files]
            # Selenium can handle multiple files with space-separated paths
            file_input.send_keys("\n".join(abs_paths))
            time.sleep(1)
            log.info(f"  Files sent via file input.")
            return True
        except Exception as exc:
            log.info(f"  File input method failed: {exc}")

    # Method 2: Click upload button and use native dialog
    upload_btn = _find_upload_button(driver)
    if upload_btn:
        try:
            # Click the button to trigger any state change
            upload_btn.click()
            time.sleep(0.5)

            # Now find the file input that may have appeared
            file_input = _find_file_input(driver)
            if file_input:
                abs_paths = [os.path.abspath(f) for f in files]
                file_input.send_keys("\n".join(abs_paths))
                time.sleep(1)
                log.info(f"  Files uploaded via button click.")
                return True
        except Exception as exc:
            log.info(f"  Upload button method failed: {exc}")

    log.warning("  Could not find file input or upload button")
    return False


def _count_responses(driver: uc.Chrome) -> int:
    """Count the number of model response containers on the page."""
    try:
        return driver.execute_script(_JS_COUNT_RESPONSES) or 0
    except (TimeoutException, WebDriverException):
        return 0
    except Exception:
        return 0


def _is_response_complete(driver: uc.Chrome) -> bool:
    """Check whether the latest response has signalled completion."""
    try:
        return bool(driver.execute_script(_JS_IS_COMPLETE))
    except (TimeoutException, WebDriverException):
        return False
    except Exception:
        return False


# Tracks which JS strategy (in _JS_EXTRACT_RESPONSE) returned the most recent
# response text. Updated by _extract_latest_response_text on each call. -1 means
# unknown / extraction failed / no strategy fired. See HANDOFF.md and Task B3
# (2026-06-05) — instrumentation for observability of DOM-drift.
_LAST_EXTRACTOR_STRATEGY: int = -1


def _extract_latest_response_text(driver: uc.Chrome) -> str:
    """Extract the text of the most recent model response.

    Also records which JS extraction strategy fired into the module-level
    ``_LAST_EXTRACTOR_STRATEGY`` (mirrored onto ``GeminiSession`` as
    ``last_extractor_strategy`` after ``prompt()`` returns).
    """
    global _LAST_EXTRACTOR_STRATEGY
    try:
        _raw = driver.execute_script(_JS_EXTRACT_RESPONSE)
        if isinstance(_raw, dict):
            result = _raw.get("text", "") or ""
            _LAST_EXTRACTOR_STRATEGY = int(_raw.get("strategy", -1))
        else:
            # Backward-compat: older JS returned a bare string.
            result = _raw or ""
            _LAST_EXTRACTOR_STRATEGY = -1
        return result
    except (TimeoutException, WebDriverException):
        _LAST_EXTRACTOR_STRATEGY = -1
        return ""
    except Exception:
        _LAST_EXTRACTOR_STRATEGY = -1
        return ""


def send_prompt(driver: uc.Chrome, prompt: str, files: list[str] = None,
                max_retries: int = 2) -> str:
    """
    Paste a prompt into Gemini and wait for the response.
    Returns the response text, or an empty string on failure.

    Args:
        driver: Chrome driver instance
        prompt: Text prompt to send
        files: Optional list of file paths to upload before sending
        max_retries: Number of retries on failure

    Uses the OS clipboard (pyperclip + Ctrl+V) as the primary input method
    so the full prompt is pasted instantly instead of being typed character
    by character.  Falls back to JS ClipboardEvent, then send_keys.
    """
    if files is None:
        files = []

    last_error = None

    # TODO: migrate to retry_policy.retry (currently tangled with driver.get()
    # reset between attempts; see retry_policy.py for bounded backoff + jitter
    # + error classification)
    for attempt in range(max_retries + 1):
        try:
            return _send_prompt_inner(driver, prompt, files)
        except Exception as exc:
            last_error = exc
            log.warning(f"  send_prompt attempt {attempt + 1} failed: {exc}")
            if attempt < max_retries:
                time.sleep(2)
                try:
                    driver.get(GEMINI_URL)
                    time.sleep(2)
                except Exception:
                    pass

    log.error(f"  All {max_retries + 1} attempts failed: {last_error}")
    return ""


def _send_prompt_inner(driver: uc.Chrome, prompt: str, files: list[str] = None) -> str:
    """Inner implementation of send_prompt with retry wrapper."""
    if files is None:
        files = []

    baseline_count = _count_responses(driver)

    input_el = _find_input(driver)
    if not input_el:
        log.error("Could not find Gemini input box. DOM may have changed.")
        raise RuntimeError("Could not find Gemini input box")

    input_el.click()
    time.sleep(0.3)

    # Upload files first (if any)
    if files:
        uploaded = upload_files(driver, files)
        if uploaded:
            log.info(f"  Uploaded {len(files)} file(s)")
            time.sleep(1)  # Wait for upload to process
        else:
            log.warning("  File upload may have failed")

    # Clear any existing text
    try:
        input_el.send_keys(Keys.CONTROL + "a")
        time.sleep(0.1)
        input_el.send_keys(Keys.DELETE)
    except Exception:
        pass
    time.sleep(0.2)

    # Use clipboard + paste (most reliable for Gemini's contenteditable)
    pasted = False
    clipboard_max_chars = 50000  # Conservative limit for Windows clipboard

    # Clean prompt: strip non-BMP Unicode (causes ChromeDriver issues)
    def clean_for_chrome(text):
        """Remove characters outside BMP that ChromeDriver can't handle."""
        return ''.join(c for c in text if ord(c) <= 0xFFFF)

    clean_prompt = clean_for_chrome(prompt)

    try:
        import platform
        if platform.system() == 'Windows':
            # Use pyperclip directly if available (uses Windows clipboard API, most reliable)
            if _pyperclip_available and pyperclip is not None:
                pyperclip.copy(clean_prompt)
                time.sleep(0.2)
                log.info("  Set clipboard via pyperclip (%d chars).", len(clean_prompt))
            else:
                # Fallback: temp file piped through PowerShell
                import subprocess
                import tempfile
                tmp_fd, temp_file = tempfile.mkstemp(suffix='.txt', prefix='gemini_clip_')
                try:
                    with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                        f.write(clean_prompt)
                    subprocess.run(
                        ['powershell', '-command',
                         f"Get-Content -Path '{temp_file}' -Encoding UTF8 -Raw | Set-Clipboard"],
                        capture_output=True, timeout=15
                    )
                finally:
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass
                time.sleep(0.3)
                log.info("  Set clipboard via PowerShell temp-file (%d chars).", len(clean_prompt))

            driver.execute_script("arguments[0].focus();", input_el)
            time.sleep(0.1)
            input_el.send_keys(Keys.CONTROL + "v")
            time.sleep(0.5)

            # Verify paste worked
            current = driver.execute_script(
                "return (arguments[0].innerText || '').length;", input_el
            )
            pasted = current and current >= len(clean_prompt) * 0.8
            if pasted:
                log.info("  Pasted via clipboard (%d/%d chars).", current, len(clean_prompt))
            else:
                log.info("  Clipboard paste unverified (%s/%d chars) — trying JS focus+paste.",
                         current, len(clean_prompt))
                # Ensure element has focus, then retry paste
                driver.execute_script("arguments[0].focus();", input_el)
                time.sleep(0.1)
                input_el.send_keys(Keys.CONTROL + "v")
                time.sleep(0.5)
                current2 = driver.execute_script(
                    "return (arguments[0].innerText || '').length;", input_el
                )
                pasted = current2 and current2 >= len(clean_prompt) * 0.8
                if pasted:
                    log.info("  Retry paste succeeded (%s/%d chars).", current2, len(clean_prompt))

    except Exception as exc:
        log.info(f"  Clipboard failed: {exc}")

    # If clipboard didn't work, use execCommand('insertText') — works for any length,
    # fires proper React/synthetic events, no clipboard needed.
    if not pasted:
        log.info("  Using execCommand insertText (%d chars).", len(clean_prompt))
        try:
            result = driver.execute_script("""
                var el = arguments[0];
                var text = arguments[1];
                el.focus();
                document.execCommand('selectAll', false, null);
                var ok = document.execCommand('insertText', false, text);
                return ok;
            """, input_el, clean_prompt)
            time.sleep(0.5)
            current = driver.execute_script(
                "return (arguments[0].innerText || '').length;", input_el
            )
            pasted = current and current >= len(clean_prompt) * 0.8
            if pasted:
                log.info("  insertText succeeded (%s/%d chars).", current, len(clean_prompt))
            else:
                log.info("  insertText result=%s, got %s/%d chars — falling back to innerText.",
                         result, current, len(clean_prompt))
                # Final fallback: direct innerText set with full event suite
                driver.execute_script("""
                    var el = arguments[0];
                    var text = arguments[1];
                    el.focus();
                    el.innerText = text;
                    el.dispatchEvent(new InputEvent('beforeinput', {bubbles:true,inputType:'insertText',data:text}));
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                """, input_el, clean_prompt)
                time.sleep(0.5)
                pasted = True
        except Exception as exc:
            log.info("  execCommand insertText failed: %s — using innerText fallback.", exc)
            driver.execute_script("""
                var el = arguments[0];
                var text = arguments[1];
                el.focus();
                el.innerText = text;
                el.dispatchEvent(new Event('input', {bubbles:true}));
            """, input_el, clean_prompt)
            time.sleep(0.5)
            pasted = True

    # Trigger React state update
    driver.execute_script("""
        var el = arguments[0];
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    """, input_el)
    time.sleep(0.5)

    # Verify text is in the input
    current = driver.execute_script(
        "return (arguments[0].innerText || '').length;", input_el
    )
    if current and current > 0:
        log.info("  Text in input: %d chars", current)
    else:
        log.warning("  Text may not be in input")

    # Wait for send button to be enabled
    time.sleep(0.5)

    # Find and click the send button
    send_btn = _find_send_button(driver, input_el)

    if send_btn:
        # Check if button is enabled
        try:
            is_disabled = send_btn.get_dom_attribute('disabled')
            if is_disabled:
                log.info("  Send button disabled, waiting...")
                time.sleep(1)
        except Exception:
            pass

        try:
            # Use JavaScript click which bypasses overlay issues
            driver.execute_script("arguments[0].click();", send_btn)
            log.info("  Submitted via JS click.")
        except Exception as exc:
            log.info(f"  JS click failed: {exc}, trying regular click")
            try:
                send_btn.click()
            except Exception:
                # Last resort: keyboard
                input_el.send_keys(Keys.RETURN)
    else:
        log.info("  No send button found, using Enter")
        input_el.send_keys(Keys.RETURN)

    time.sleep(0.5)

    # Check if submit worked
    try:
        remaining = driver.execute_script(
            "return (arguments[0].innerText || '').trim().length;", input_el
        )
        if remaining and remaining > 0:
            log.warning("  Text still in input - retrying submit")
            input_el.send_keys(Keys.RETURN)
            time.sleep(0.5)
    except Exception:
        pass

    log.info("  Waiting for Gemini response...")
    response_text = _wait_for_response(driver, baseline_count)
    return response_text


def _wait_for_response(driver: uc.Chrome, baseline_count: int) -> str:
    deadline = time.time() + RESPONSE_TIMEOUT

    try:
        baseline_text = _extract_latest_response_text(driver) or ""
    except Exception:
        baseline_text = ""
    baseline_text_len = len(baseline_text)

    consecutive_errors = 0
    while time.time() < deadline:
        try:
            current_count = _count_responses(driver)
            consecutive_errors = 0
        except Exception:
            current_count = 0
            consecutive_errors += 1
            if consecutive_errors > 15:
                log.warning("  Too many consecutive errors polling response count.")
                final_text = _extract_latest_response_text(driver) or ""
                if final_text:
                    return final_text
                return ""
            time.sleep(1)
            continue

        if current_count > baseline_count:
            log.info("  Response detected via container count (%d -> %d).",
                     baseline_count, current_count)
            break

        try:
            current_text = _extract_latest_response_text(driver) or ""
            if len(current_text) > baseline_text_len + 30:
                log.info("  Response detected via text-length change (%d -> %d).",
                         baseline_text_len, len(current_text))
                break
        except Exception:
            pass

        time.sleep(0.5)
    else:
        final_text = _extract_latest_response_text(driver) or ""
        if len(final_text) > baseline_text_len + 30:
            log.info("  Response found at timeout boundary (%d chars).", len(final_text))
            return final_text
        log.warning("  Timed out waiting for a response container.")
        return ""

    response_appeared_at = time.time()

    STABLE_SECONDS = 3.0
    MIN_WAIT_SECONDS = 4.0
    HARD_MAX_SECONDS = 240.0
    POLL_INTERVAL = 0.5

    log.info("  Response container detected. Waiting for completion...")
    prev_text_normalized = ""
    best_text = ""
    stable_ticks = 0
    stable_ticks_needed = int(STABLE_SECONDS / POLL_INTERVAL)
    consecutive_errors = 0
    last_progress_log = time.time()

    while time.time() < deadline:
        elapsed = time.time() - response_appeared_at

        if elapsed >= HARD_MAX_SECONDS:
            best = _extract_latest_response_text(driver)
            if best:
                log.info("  Hard max wait (%.0fs) — returning best text (%d chars).",
                         elapsed, len(best))
                return best
            log.warning("  Hard max wait reached with no text.")
            return ""

        try:
            text = _extract_latest_response_text(driver)
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
            if consecutive_errors > 10:
                log.warning("  Too many consecutive errors extracting text.")
                return best_text if best_text else ""
            time.sleep(1)
            continue

        normalized = (text or "").rstrip()
        if text and len(text) > len(best_text):
            best_text = text

        if normalized and normalized == prev_text_normalized:
            stable_ticks += 1
        else:
            stable_ticks = 0
        prev_text_normalized = normalized

        if time.time() - last_progress_log > 45:
            log.info("  Still waiting... elapsed=%.0fs, textLen=%d, stableTicks=%d",
                     elapsed, len(text or ""), stable_ticks)
            last_progress_log = time.time()

        if elapsed >= MIN_WAIT_SECONDS and text:
            if _is_response_complete(driver) and stable_ticks >= 2:
                log.info("  Completion signal + stable text — done.")
                return text

            if stable_ticks >= stable_ticks_needed:
                log.info("  Text stable for %.1fs — done.", STABLE_SECONDS)
                return text

        time.sleep(POLL_INTERVAL)

    log.warning("  Timed out (%.0fs), returning best available text.", RESPONSE_TIMEOUT)
    result = _extract_latest_response_text(driver) or best_text
    return result


# ══════════════════════════════════════════════════════════════════════════════
# DEEP RESEARCH JS HELPERS
# ══════════════════════════════════════════════════════════════════════════════
#
# These scripts locate the UI elements specific to Gemini's Deep Research
# feature, which lives in the Tools dropdown menu (the "+" / "Tools" button
# at the bottom-left of the input area).
#
# The Tools menu is a Material-style panel that opens when the Tools button is
# clicked.  It lists: "Create image", "Canvas", "Deep research", "Create video",
# "Create music", "Guided learning", etc.
# ══════════════════════════════════════════════════════════════════════════════

_JS_OPEN_TOOLS_MENU = """
// Find and click the Tools button that opens the tools dropdown.
// It is located near the prompt input and contains the text "Tools"
// or has an aria-label / tooltip hinting at tools / more options.

var toolKeywords = ['tools', 'more options', 'more', 'add', 'expand'];

// Strategy 1: explicit aria-label or text match on button elements
var buttons = document.querySelectorAll('button, [role="button"]');
for (var i = 0; i < buttons.length; i++) {
    var btn = buttons[i];
    if (!btn.offsetWidth || !btn.offsetHeight) continue;
    var label = (
        (btn.getAttribute('aria-label') || '') +
        (btn.getAttribute('data-tooltip') || '') +
        (btn.getAttribute('mattooltip') || '') +
        (btn.getAttribute('title') || '') +
        (btn.textContent || '')
    ).toLowerCase().trim();
    // "Tools" exact match is the strongest signal
    if (label === 'tools' || label.startsWith('tools')) {
        btn.click();
        return true;
    }
}

// Strategy 2: partial match — button whose visible label is just "Tools"
for (var i = 0; i < buttons.length; i++) {
    var btn = buttons[i];
    if (!btn.offsetWidth) continue;
    var txt = (btn.textContent || '').trim().toLowerCase();
    var aria = (btn.getAttribute('aria-label') || '').toLowerCase();
    if (txt === 'tools' || aria === 'tools') {
        btn.click();
        return true;
    }
    if (txt.indexOf('tools') !== -1 || aria.indexOf('tools') !== -1) {
        btn.click();
        return true;
    }
}

// Strategy 3: the "+" button near the input (opens tools on some Gemini builds)
var inputEl = null;
var allEditable = document.querySelectorAll('[contenteditable="true"], textarea');
for (var i = 0; i < allEditable.length; i++) {
    if (allEditable[i].offsetWidth > 50) { inputEl = allEditable[i]; break; }
}
if (inputEl) {
    var inputRect = inputEl.getBoundingClientRect();
    for (var i = 0; i < buttons.length; i++) {
        var btn = buttons[i];
        if (!btn.offsetWidth) continue;
        var r = btn.getBoundingClientRect();
        // Button must be close to and below the input
        var nearInput = Math.abs(r.top - inputRect.bottom) < 60 &&
                        r.left < inputRect.left + 100;
        if (nearInput) {
            var txt = (btn.textContent || '').trim();
            var aria2 = (btn.getAttribute('aria-label') || '').toLowerCase();
            if (txt === '+' || aria2.indexOf('tool') !== -1 || aria2.indexOf('add') !== -1) {
                btn.click();
                return true;
            }
        }
    }
}

return false;
"""

_JS_FIND_DEEP_RESEARCH_OPTION = """
// After the Tools menu is open, find the 'Deep research' list item.
// The menu is typically a mat-menu or similar Angular Material panel.

// Strategy 1: any visible element whose text is exactly / closely "Deep research"
var allEls = document.querySelectorAll(
    'button, [role="menuitem"], [role="option"], li, mat-list-item, ' +
    '[class*="menu-item"], [class*="option"], [class*="list-item"]'
);
for (var i = 0; i < allEls.length; i++) {
    var el = allEls[i];
    if (!el.offsetWidth && !el.offsetHeight) continue;
    var txt = (el.textContent || '').trim().toLowerCase();
    if (txt === 'deep research' || txt.startsWith('deep research')) {
        return el;
    }
}

// Strategy 2: broader text search — element contains "deep research" and is visible
var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
var node;
while ((node = walker.nextNode())) {
    var val = node.nodeValue.trim().toLowerCase();
    if (val === 'deep research') {
        // Walk up to find the clickable ancestor
        var el = node.parentElement;
        for (var depth = 0; depth < 5 && el; depth++) {
            if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'menuitem' ||
                el.getAttribute('role') === 'option' ||
                el.getAttribute('role') === 'listitem') {
                if (el.offsetWidth || el.offsetHeight) return el;
            }
            el = el.parentElement;
        }
        // Return the text's direct parent as fallback
        if (node.parentElement && (node.parentElement.offsetWidth || node.parentElement.offsetHeight)) {
            return node.parentElement;
        }
    }
}

return null;
"""

_JS_IS_DEEP_RESEARCH_ACTIVE = """
// Return true if the UI currently shows Deep Research as the active mode.
// Signals: a chip/pill/label/badge near the input mentioning "deep research",
// or the input placeholder changed, or a dedicated indicator element is present.

var signals = [
    // Chip/badge/pill — Gemini often adds a labelled chip above the input
    '[class*="chip"]', '[class*="Chip"]', '[class*="badge"]',
    '[class*="mode-indicator"]', '[class*="tool-indicator"]',
    // Research-specific containers that only appear in deep research mode
    '[class*="research-mode"]', '[class*="deep-research"]',
    // aria-label / data attribute on root toolbar
    '[aria-label*="deep research" i]', '[data-mode*="research" i]',
];

for (var i = 0; i < signals.length; i++) {
    var els = document.querySelectorAll(signals[i]);
    for (var j = 0; j < els.length; j++) {
        var txt = (els[j].textContent || els[j].getAttribute('aria-label') || '').toLowerCase();
        if (txt.indexOf('deep research') !== -1 || txt.indexOf('deep_research') !== -1) {
            return true;
        }
    }
}

// Check input placeholder for research hint
var inputs = document.querySelectorAll('[contenteditable="true"], textarea');
for (var i = 0; i < inputs.length; i++) {
    var ph = (
        inputs[i].getAttribute('placeholder') ||
        inputs[i].getAttribute('data-placeholder') ||
        inputs[i].getAttribute('aria-placeholder') || ''
    ).toLowerCase();
    if (ph.indexOf('research') !== -1) return true;
}

return false;
"""

_JS_FIND_PLAN_BUTTON = """
// Find the button that starts/approves the Deep Research plan.
// Gemini shows a plan outline after submitting and offers a single CTA button.
// Common labels: "Start research", "Start", "Approve plan", "Begin research".

var planKeywords = [
    'start research', 'begin research', 'approve plan', 'approve research',
    'run research', 'start deep research', 'start',
];

var buttons = document.querySelectorAll('button, [role="button"]');
var best = null, bestScore = 0;

for (var i = 0; i < buttons.length; i++) {
    var btn = buttons[i];
    if (!btn.offsetWidth || btn.disabled) continue;
    var label = (
        (btn.textContent || '') +
        (btn.getAttribute('aria-label') || '')
    ).trim().toLowerCase();

    var score = 0;
    for (var j = 0; j < planKeywords.length; j++) {
        if (label === planKeywords[j]) { score = 100 - j; break; }
        if (label.indexOf(planKeywords[j]) !== -1) { score = 50 - j; break; }
    }
    if (score > bestScore) { bestScore = score; best = btn; }
}

return bestScore > 0 ? best : null;
"""

_JS_FIND_VIEW_REPORT_BUTTON = """
// Find the 'View report' / 'View research' button that appears when Deep
// Research has finished.  Also returns truthy if the report is already inline.

var viewKeywords = [
    'view report', 'view research report', 'see report', 'open report',
    'view full report', 'read report',
];

var buttons = document.querySelectorAll('button, [role="button"], a');
for (var i = 0; i < buttons.length; i++) {
    var el = buttons[i];
    if (!el.offsetWidth) continue;
    var txt = (el.textContent || el.getAttribute('aria-label') || '').trim().toLowerCase();
    for (var j = 0; j < viewKeywords.length; j++) {
        if (txt === viewKeywords[j] || txt.indexOf(viewKeywords[j]) !== -1) {
            return el;
        }
    }
}
return null;
"""

_JS_IS_DR_STREAMING = """
// Return true if Deep Research is still actively working / streaming.
// Signals: visible spinner/progress, 'Searching' or 'Researching' text,
//          source-loading cards, or the stop-generation button.

// Spinner or progress indicator
var spinners = document.querySelectorAll(
    '[class*="spinner"], [class*="loading"], [class*="progress"], ' +
    '[class*="searching"], [class*="researching"], [class*="generating"], ' +
    '[aria-label*="loading" i], [aria-label*="searching" i]'
);
for (var i = 0; i < spinners.length; i++) {
    if (spinners[i].offsetWidth || spinners[i].offsetHeight) return true;
}

// Visible text that says "Searching" or "Researching…"
var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
var node;
while ((node = walker.nextNode())) {
    var val = node.nodeValue.trim().toLowerCase();
    if ((val.startsWith('searching') || val.startsWith('researching') ||
         val.startsWith('browsing') || val === 'stop') &&
        node.parentElement && node.parentElement.offsetWidth) {
        return true;
    }
}

// Stop-generation button
var buttons = document.querySelectorAll('button');
for (var i = 0; i < buttons.length; i++) {
    var txt = (
        (buttons[i].textContent || '') +
        (buttons[i].getAttribute('aria-label') || '')
    ).toLowerCase();
    if ((txt.indexOf('stop') !== -1 || txt.indexOf('cancel') !== -1) &&
        txt.indexOf('generat') !== -1 && buttons[i].offsetWidth) {
        return true;
    }
}

return false;
"""

_JS_EXTRACT_RESEARCH_REPORT = """
// Extract the Deep Research report text.
// The report is structurally different from a normal chat response:
//   • Much longer (thousands of words)
//   • Contains headings, bullet points, inline citations
//   • May appear in a dedicated research-report container
//   • Or inline as an unusually large model-response element

function getText(el) {
    return (el ? (el.innerText || el.textContent || '').trim() : '');
}

// Strategy 1: dedicated research-report container
var reportSelectors = [
    '[class*="research-report"]', '[class*="report-content"]',
    '[class*="research-result"]', '[class*="deep-research-result"]',
    'deep-research-report', 'research-report',
];
for (var i = 0; i < reportSelectors.length; i++) {
    var el = document.querySelector(reportSelectors[i]);
    if (el) {
        var t = getText(el);
        if (t.length > 200) return t;
    }
}

// Strategy 2: the longest model-response element (research reports are huge)
var responses = document.querySelectorAll(
    'model-response, message-content, [data-message-author-role="model"], ' +
    '[data-author-role="model"], [class*="model-text"], [class*="response-text"]'
);
var longest = '', longestLen = 0;
for (var i = 0; i < responses.length; i++) {
    var t = getText(responses[i]);
    if (t.length > longestLen) { longestLen = t.length; longest = t; }
}
if (longestLen > 500) return longest;

// Strategy 3: longest markdown block
var mdBlocks = document.querySelectorAll('.markdown, [class*="markdown"]');
var longest2 = '', longestLen2 = 0;
for (var i = 0; i < mdBlocks.length; i++) {
    var t = getText(mdBlocks[i]);
    if (t.length > longestLen2) { longestLen2 = t.length; longest2 = t; }
}
if (longestLen2 > 500) return longest2;

// Strategy 4: immersive / full-page report view
var immersive = document.querySelector(
    '[class*="immersive"], [class*="full-page"], [class*="report-view"]'
);
if (immersive) {
    var t = getText(immersive);
    if (t.length > 200) return t;
}

// Strategy 5: fallback to the standard last-response extractor
""" + _JS_EXTRACT_RESPONSE + """
"""


# ── GeminiSession (Python API) ──────────────────────────────────────────────

class GeminiSession:
    """
    Context-managed Gemini session for use as a Python library.

        with GeminiSession() as g:
            print(g.prompt("Hello"))
            print(g.prompt("Follow-up question"))
    """

    def __init__(
        self,
        cookie_file: str = DEFAULT_COOKIE_FILE,
        profile_dir: Optional[str] = None,
        chrome_major: Optional[int] = None,
        headless: bool = False,
        verbose: bool = False,
    ):
        self.cookie_file = cookie_file
        self.profile_dir = profile_dir
        self.chrome_major = chrome_major
        self.headless = headless
        self.verbose = verbose
        self._driver: Optional[uc.Chrome] = None
        # Index of the JS extractor strategy that returned the most recent
        # response (see _JS_EXTRACT_RESPONSE). -1 = unknown / no prompt yet.
        self.last_extractor_strategy: int = -1

    def __enter__(self) -> "GeminiSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        """Launch browser and log in."""
        if self.verbose:
            _enable_console_logging()
        self._driver = init_driver(
            chrome_major=self.chrome_major,
            profile_dir=self.profile_dir,
            headless=self.headless,
        )
        gemini_login(self._driver, cookie_file=self.cookie_file, verbose=self.verbose)

    def stop(self) -> None:
        """Close browser and clean up ephemeral profile."""
        if self._driver:
            quit_driver(self._driver)
            self._driver = None

    def prompt(self, text: str, new_chat: bool = True, files: list[str] = None) -> str:
        """
        Send a prompt and return the response text.

        Args:
            text:     The prompt string.
            new_chat: If True, navigate to a fresh Gemini chat first
                      (avoids context bleed between prompts).
            files:    Optional list of file paths to upload before sending.
        """
        if files is None:
            files = []

        if self._driver is None:
            raise RuntimeError("Session not started. Use `with GeminiSession()` or call .start().")
        if new_chat:
            try:
                self._driver.get(GEMINI_URL)
                time.sleep(1)
                for _ in range(20):
                    time.sleep(1)
                    if _find_input(self._driver):
                        time.sleep(0.5)
                        break
                else:
                    log.warning("Input not found after navigation - proceeding anyway")
            except Exception as exc:
                log.warning(f"Navigation to Gemini failed: {exc}, retrying...")
                time.sleep(2)
                try:
                    self._driver.get(GEMINI_URL)
                    time.sleep(2)
                except Exception as exc2:
                    log.error(f"Retry navigation also failed: {exc2}")
                    raise RuntimeError(f"Failed to navigate to Gemini: {exc2}")
        _response = send_prompt(self._driver, text, files=files)
        # Snapshot which JS extractor strategy returned the text (for
        # observability / DOM-drift detection — see Task B3, 2026-06-05).
        self.last_extractor_strategy = _LAST_EXTRACTOR_STRATEGY
        return _response

    def deep_research(self, prompt: str, timeout: int = 600) -> str:
        """
        Fully-automated Deep Research via Gemini's Tools menu.

        Flow:
          1. Navigate to a fresh Gemini chat.
          2. Open the Tools dropdown and click "Deep research".
          3. Verify the mode activated (chip / indicator appears).
          4. Submit the prompt using the existing clipboard-paste pipeline.
          5. Auto-approve the research plan ("Start research" button).
          6. Wait for the research to finish (progress indicators vanish,
             "View report" appears, or response text stabilises).
          7. Extract and return the full research report.

        Args:
            prompt:  The research question or task.
            timeout: Max seconds to wait for the report (default 600 = 10 min).
                     Deep Research typically takes 3-8 minutes.

        Returns:
            The full research report as a string.

        Raises:
            RuntimeError: If the session is not started or a critical step fails.
        """
        if self._driver is None:
            raise RuntimeError(
                "Session not started. Use `with GeminiSession()` or call .start()."
            )

        driver = self._driver
        _print_stderr("")
        _print_stderr("=" * 60)
        _print_stderr("  GEMINI DEEP RESEARCH")
        _print_stderr(f"  Query: {prompt[:70]}{'...' if len(prompt) > 70 else ''}")
        _print_stderr("=" * 60)

        # Phase 1: fresh chat
        _print_stderr("  [1/5] Opening fresh Gemini chat…")
        driver.get(GEMINI_URL)
        for _ in range(20):
            time.sleep(1)
            if _find_input(driver):
                break
        else:
            raise RuntimeError("Gemini chat input not ready after navigation.")

        # Phase 2: activate Deep Research via Tools menu
        _print_stderr("  [2/5] Activating Deep Research via Tools menu…")
        activated = self._activate_deep_research_mode(driver)
        if not activated:
            raise RuntimeError(
                "Could not activate Deep Research mode. "
                "The Tools menu or 'Deep research' option was not found. "
                "Check that your Gemini account has Deep Research access."
            )
        _print_stderr("  [2/5] Deep Research mode active.")

        # Phase 3: submit the prompt
        _print_stderr("  [3/5] Submitting prompt…")
        baseline = _count_responses(driver)
        # send_prompt handles clipboard paste + retry; we just need it to submit
        send_prompt(driver, prompt)
        _print_stderr("  [3/5] Prompt submitted.")

        deadline = time.time() + timeout

        # Phase 4: wait for the plan and auto-approve it
        _print_stderr("  [4/5] Waiting for research plan…")
        approved = self._dr_approve_plan(driver, deadline)
        if approved:
            _print_stderr("  [4/5] Plan approved — research running.")
        else:
            _print_stderr("  [4/5] Plan approval skipped / not found — continuing.")

        # Phase 5: wait for completion, extract report
        _print_stderr(f"  [5/5] Waiting for report (timeout={timeout}s)…")
        report = self._dr_wait_and_extract(driver, deadline)

        _print_stderr(f"  ✓ Report extracted: {len(report):,} chars")
        _print_stderr("=" * 60)
        return report

    # ── Deep Research helpers (private) ───────────────────────────────────────

    def _activate_deep_research_mode(self, driver) -> bool:
        """
        Open the Tools dropdown and click 'Deep research'.
        Returns True if the activation succeeded.

        Strategy:
          A. Find the Tools button by text/aria-label, click it.
          B. Find 'Deep research' in the resulting menu, click it.
          C. Verify activation via a DOM indicator (chip, label, or changed
             UI element that mentions "deep research").
        """
        # ── Step A: open the Tools menu ──────────────────────────────────────
        opened = driver.execute_script(_JS_OPEN_TOOLS_MENU)
        if not opened:
            log.warning("_activate_deep_research_mode: Tools button not found.")
            return False

        # Wait for menu to render
        for _ in range(8):
            time.sleep(0.4)
            option = driver.execute_script(_JS_FIND_DEEP_RESEARCH_OPTION)
            if option:
                break
        else:
            log.warning("_activate_deep_research_mode: 'Deep research' option not in menu.")
            # Close menu by pressing Escape before giving up
            try:
                from selenium.webdriver.common.keys import Keys as _Keys
                driver.find_element(By.TAG_NAME, "body").send_keys(_Keys.ESCAPE)
            except Exception:
                pass
            return False

        # ── Step B: click the Deep research option ───────────────────────────
        try:
            driver.execute_script("arguments[0].click();", option)
        except Exception:
            try:
                option.click()
            except Exception as exc:
                log.warning(f"_activate_deep_research_mode: click failed: {exc}")
                return False

        # ── Step C: verify the mode is now active ────────────────────────────
        for _ in range(10):
            time.sleep(0.5)
            if driver.execute_script(_JS_IS_DEEP_RESEARCH_ACTIVE):
                return True

        # Even if the indicator isn't found, the click may have worked.
        # Return True optimistically — the submit step will reveal any problem.
        log.info("_activate_deep_research_mode: activation indicator not confirmed, "
                 "proceeding optimistically.")
        return True

    def _dr_approve_plan(self, driver, deadline: float) -> bool:
        """
        Poll for the research-plan approval button and click it.
        Returns True if a button was found and clicked within the deadline.

        Gemini shows a research plan shortly after submitting in Deep Research
        mode.  The plan contains an outline of what it will investigate.
        There is a single call-to-action button: usually 'Start research'.
        """
        _PLAN_TIMEOUT = min(120.0, deadline - time.time())  # up to 2 min for plan
        plan_deadline = time.time() + _PLAN_TIMEOUT

        while time.time() < plan_deadline:
            btn = driver.execute_script(_JS_FIND_PLAN_BUTTON)
            if btn:
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    time.sleep(0.3)
                    driver.execute_script("arguments[0].click();", btn)
                    log.info("_dr_approve_plan: clicked plan button: '%s'",
                             (btn.text or "").strip()[:40])
                    return True
                except Exception as exc:
                    log.warning(f"_dr_approve_plan: click failed: {exc}")
            time.sleep(1.5)

        return False

    def _dr_wait_and_extract(self, driver, deadline: float) -> str:
        """
        Wait for Deep Research to finish, then extract the full report.

        Completion signals (any one is sufficient):
          • A 'View report' button appears.
          • The response text exceeds DR_MIN_REPORT_CHARS and has been stable
            for DR_STABLE_SECONDS consecutive seconds.
          • All streaming/searching indicators have vanished.

        After detecting completion, extracts the report using
        _JS_EXTRACT_RESEARCH_REPORT, which is tailored to the Deep Research
        result container rather than the normal chat response.
        """
        DR_MIN_REPORT_CHARS = 500
        DR_STABLE_SECONDS   = 6.0
        POLL_INTERVAL       = 3.0

        best_text      = ""
        stable_since   = None
        last_log_time  = time.time()
        start_time     = time.time()

        while time.time() < deadline:
            elapsed = int(time.time() - start_time)

            # Progress log every 30 seconds
            if time.time() - last_log_time >= 30:
                _print_stderr(
                    f"  … research running ({elapsed}s elapsed, "
                    f"{len(best_text):,} chars so far)"
                )
                last_log_time = time.time()

            # Check for completion button
            view_btn = driver.execute_script(_JS_FIND_VIEW_REPORT_BUTTON)
            if view_btn:
                _print_stderr(f"  'View report' button found ({elapsed}s) — clicking…")
                try:
                    driver.execute_script("arguments[0].click();", view_btn)
                    time.sleep(3)
                except Exception:
                    pass
                # Extract after clicking. The embedded _JS_EXTRACT_RESPONSE
                # fallback may return a {text, strategy} dict — unwrap to str.
                _rr = driver.execute_script(_JS_EXTRACT_RESEARCH_REPORT)
                report = (_rr.get("text", "") if isinstance(_rr, dict) else (_rr or ""))
                if report and len(report) >= DR_MIN_REPORT_CHARS:
                    return report

            # Extract current text
            try:
                _rr = driver.execute_script(_JS_EXTRACT_RESEARCH_REPORT)
                current = (_rr.get("text", "") if isinstance(_rr, dict) else (_rr or ""))
            except Exception:
                current = ""

            if len(current) > len(best_text):
                best_text = current
                stable_since = None  # reset stability clock on growth

            # Stability check: has content stopped growing?
            if best_text and len(best_text) >= DR_MIN_REPORT_CHARS:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= DR_STABLE_SECONDS:
                    # Check streaming indicators have stopped
                    still_streaming = driver.execute_script(_JS_IS_DR_STREAMING)
                    if not still_streaming:
                        _print_stderr(
                            f"  Research stable ({elapsed}s, "
                            f"{len(best_text):,} chars) — done."
                        )
                        return best_text
            else:
                stable_since = None

            time.sleep(POLL_INTERVAL)

        _print_stderr(
            f"  ⚠ Timeout ({int(deadline - time.time() + (deadline - time.time()))}s) "
            f"— returning best available text ({len(best_text):,} chars)."
        )
        return best_text or _extract_latest_response_text(driver) or ""


# ── Checkpoint (batch mode) ─────────────────────────────────────────────────

def load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"results": {}}


def save_checkpoint(results: dict) -> None:
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump({"results": results}, f, ensure_ascii=False, indent=2)


# ── Batch mode ───────────────────────────────────────────────────────────────

def run_batch(
    prompts_file: str,
    output_file: str,
    chrome_major: Optional[int] = None,
    resume: bool = True,
    profile_dir: Optional[str] = None,
    cookie_file: str = DEFAULT_COOKIE_FILE,
) -> None:
    _enable_console_logging()

    with open(prompts_file, "r", encoding="utf-8") as f:
        prompts = [line.strip() for line in f if line.strip()]

    log.info(f"Loaded {len(prompts)} prompts from {prompts_file}")

    checkpoint = load_checkpoint() if resume else {"results": {}}
    results: dict[str, str] = checkpoint.get("results", {})

    driver = init_driver(chrome_major=chrome_major, profile_dir=profile_dir)

    try:
        gemini_login(driver, cookie_file=cookie_file, verbose=True)

        for idx, prompt in enumerate(prompts):
            key = str(idx)
            if resume and key in results:
                log.info(f"[{idx+1}/{len(prompts)}] Skipping (cached): {prompt[:60]}...")
                continue

            log.info(f"[{idx+1}/{len(prompts)}] Sending: {prompt[:80]}...")

            driver.get(GEMINI_URL)
            time.sleep(2)

            response = send_prompt(driver, prompt)

            if response:
                log.info(f"  ✓ Got response ({len(response)} chars)")
            else:
                log.warning(f"  ✗ Empty response for prompt {idx+1}")

            results[key] = response
            save_checkpoint(results)
            _write_results(prompts, results, output_file)

            delay = random.uniform(BETWEEN_PROMPTS_MIN, BETWEEN_PROMPTS_MAX)
            time.sleep(delay)

    except KeyboardInterrupt:
        log.info("Interrupted. Progress saved — re-run with --resume.")
    finally:
        quit_driver(driver)

    _write_results(prompts, results, output_file)
    log.info(f"Done. Results saved to: {os.path.abspath(output_file)}")


def _write_results(prompts: list[str], results: dict[str, str], path: str) -> None:
    output = []
    for idx, prompt in enumerate(prompts):
        output.append({
            "index": idx,
            "prompt": prompt,
            "response": results.get(str(idx), ""),
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


# ── Single prompt (CLI callable) ─────────────────────────────────────────────

def run_single(
    prompt: str,
    chrome_major: Optional[int] = None,
    profile_dir: Optional[str] = None,
    cookie_file: str = DEFAULT_COOKIE_FILE,
    verbose: bool = False,
) -> str:
    """
    Send a single prompt, return the response text.

    In verbose mode, prints banners to stderr.
    Always prints the raw response to stdout (for capture by callers).
    """
    if verbose:
        _enable_console_logging()

    driver = init_driver(chrome_major=chrome_major, profile_dir=profile_dir)
    try:
        gemini_login(driver, cookie_file=cookie_file, verbose=verbose)

        log.info(f"Sending prompt: {prompt[:80]}...")
        response = send_prompt(driver, prompt)

        if response:
            sys.stdout.write(response)
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            if verbose:
                _print_stderr("  No response captured.")
            sys.exit(1)

        return response

    except KeyboardInterrupt:
        log.info("Interrupted.")
        sys.exit(130)
    finally:
        quit_driver(driver)


# ── Login-only mode ──────────────────────────────────────────────────────────

def run_login(
    chrome_major: Optional[int] = None,
    profile_dir: Optional[str] = None,
    cookie_file: str = DEFAULT_COOKIE_FILE,
    force: bool = False,
) -> None:
    """Open browser, wait for login, save cookies, then exit."""
    _enable_console_logging()

    if force and os.path.exists(cookie_file):
        os.remove(cookie_file)
        log.info(f"Removed old cookie file: {cookie_file}")

    # For login mode, use a persistent profile so the login sticks
    persistent_profile = profile_dir or os.path.join(
        os.path.expanduser("~"), ".gemini_tool_profile"
    )
    driver = init_driver(chrome_major=chrome_major, profile_dir=persistent_profile)
    try:
        gemini_login(driver, cookie_file=cookie_file, verbose=True)
        _print_stderr("")
        _print_stderr("  ✓ Session saved. You can now run prompts without logging in.")
        _print_stderr(f"    Cookie file: {cookie_file}")
        _print_stderr("")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ── Interactive REPL mode ─────────────────────────────────────────────────

DEFAULT_PERSISTENT_PROFILE = os.path.join(
    os.path.expanduser("~"), ".gemini_tool_profile"
)


def run_interactive(
    chrome_major: Optional[int] = None,
    profile_dir: Optional[str] = None,
    cookie_file: str = DEFAULT_COOKIE_FILE,
) -> None:
    """
    Interactive REPL: keeps the browser open across successive prompts.

    On first launch (no saved cookies), automatically opens the browser and
    prompts the user to log in.  Cookies are saved so future runs skip login.

    Special commands:
        /new   — navigate to a fresh Gemini chat
        /quit  — exit cleanly
        Ctrl+C — same as /quit
    """
    _enable_console_logging()

    # Use a persistent profile so the browser session survives between runs
    persistent_profile = profile_dir or DEFAULT_PERSISTENT_PROFILE

    # ── First-run detection ─────────────────────────────────────────
    cookie_path = Path(cookie_file)
    is_first_run = not cookie_path.exists()
    if is_first_run:
        _print_stderr("")
        _print_stderr("=" * 60)
        _print_stderr("  FIRST RUN — login required")
        _print_stderr("  A browser window will open.  Please log in to your")
        _print_stderr("  Google account.  Your session will be saved locally")
        _print_stderr(f"  to {cookie_file}")
        _print_stderr("  so you won't need to log in again.")
        _print_stderr("=" * 60)
        _print_stderr("")

    driver = init_driver(chrome_major=chrome_major, profile_dir=persistent_profile)

    try:
        gemini_login(driver, cookie_file=cookie_file, verbose=True)

        _print_stderr("")
        _print_stderr("  ✓ Gemini session ready.  Browser will stay open.")
        _print_stderr("    Type prompts below.  Commands: /new  /quit")
        _print_stderr("")

        prompt_number = 0
        while True:
            try:
                prompt = input("gemini> ").strip()
            except (EOFError, KeyboardInterrupt):
                _print_stderr("\n  Goodbye.")
                break

            if not prompt:
                continue

            # ── Special commands ────────────────────────────────────
            if prompt.lower() in ("/quit", "/exit", "/q"):
                _print_stderr("  Goodbye.")
                break
            if prompt.lower() in ("/new", "/reset", "/clear"):
                _print_stderr("  Starting a fresh chat...")
                driver.get(GEMINI_URL)
                time.sleep(2)
                _print_stderr("  ✓ New chat ready.")
                continue

            # ── Multi-line input: if the line ends with '\', keep reading
            while prompt.endswith("\\"):
                prompt = prompt[:-1] + "\n"
                try:
                    continuation = input("...    ")
                except (EOFError, KeyboardInterrupt):
                    break
                prompt += continuation

            prompt_number += 1
            log.info(f"[interactive #{prompt_number}] Sending: {prompt[:80]}...")

            response = send_prompt(driver, prompt)
            if response:
                # Print response to stdout (capturable) with a visual separator
                _print_stderr(f"  ── response ({len(response)} chars) ──")
                sys.stdout.write(response)
                sys.stdout.write("\n")
                sys.stdout.flush()
                _print_stderr("")
            else:
                _print_stderr("  ✗ No response captured.")
                _print_stderr("")

    except Exception as exc:
        log.error(f"Interactive session error: {exc}")
        traceback.print_exc(file=sys.stderr)
    finally:
        _print_stderr("  Closing browser...")
        quit_driver(driver)


# ── CLI ──────────────────────────────────────────────────────────────────────

def run_deep_research(
    prompt: str,
    timeout: int = 600,
    chrome_major: Optional[int] = None,
    profile_dir: Optional[str] = None,
    cookie_file: str = DEFAULT_COOKIE_FILE,
    output_file: Optional[str] = None,
) -> str:
    """
    Standalone deep-research runner for CLI use.
    Opens a GeminiSession, runs deep research, prints/saves the report.
    """
    _enable_console_logging()
    persistent_profile = profile_dir or os.path.join(
        os.path.expanduser("~"), ".gemini_tool_profile"
    )

    with GeminiSession(
        cookie_file=cookie_file,
        profile_dir=persistent_profile,
        chrome_major=chrome_major,
        verbose=True,
    ) as g:
        report = g.deep_research(prompt, timeout=timeout)

    if report:
        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(report)
            _print_stderr(f"  Report saved to: {output_file}")
        sys.stdout.write(report)
        sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        _print_stderr("  ✗ No report extracted.")
        sys.exit(1)

    return report


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Send prompts to Google Gemini and capture responses.",
        epilog=(
            "Examples:\n"
            "  python gemini_tool.py -i                          # interactive REPL\n"
            "  python gemini_tool.py --login                     # first-time login\n"
            "  python gemini_tool.py 'Explain quicksort'         # single prompt\n"
            "  python gemini_tool.py --batch prompts.txt         # batch mode\n"
            "  python gemini_tool.py --deep-research 'topic'     # Deep Research\n"
            "  python gemini_tool.py --deep-research 'topic' --dr-timeout 900\n"
            "  python gemini_tool.py --deep-research 'topic' --output report.md\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "prompt", nargs="?", default=None,
        help="Single prompt to send. Response is printed to stdout.",
    )
    p.add_argument(
        "--interactive", "-i", action="store_true",
        help="Interactive REPL: keep browser open and accept successive prompts.",
    )
    p.add_argument(
        "--login", action="store_true",
        help="Login-only mode: open browser, wait for login, save cookies, exit.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="With --login: delete existing cookies and force a fresh login.",
    )
    p.add_argument(
        "--batch", default=None,
        help="Path to a text file with one prompt per line.",
    )
    p.add_argument(
        "--output", default="gemini_results.json",
        help="Output JSON file for batch results.",
    )
    p.add_argument(
        "--chrome-major", type=int, default=None,
        help="Pin Chrome major version (e.g. 124).",
    )
    p.add_argument(
        "--resume", action="store_true", default=True,
        help="Resume from checkpoint in batch mode (default: on).",
    )
    p.add_argument(
        "--no-resume", dest="resume", action="store_false",
        help="Ignore checkpoint and start fresh.",
    )
    p.add_argument(
        "--profile-dir", default=None,
        help="Chrome profile directory. If omitted, an ephemeral temp dir is used.",
    )
    p.add_argument(
        "--cookie-file", default=DEFAULT_COOKIE_FILE,
        help=f"Path to cookie JSON file (default: {DEFAULT_COOKIE_FILE}).",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show status banners on stderr (auto-enabled for TTY).",
    )
    p.add_argument(
        "--stdin", action="store_true",
        help="Read prompt from stdin (avoids OS command-line length limits).",
    )
    p.add_argument(
        "--deep-research", "-dr", default=None, metavar="PROMPT",
        help=(
            "Run Gemini Deep Research for PROMPT. "
            "Opens the Tools menu, selects Deep Research, submits, "
            "auto-approves the plan, waits for the report, and prints it."
        ),
    )
    p.add_argument(
        "--dr-timeout", type=int, default=600, metavar="SECONDS",
        help="Max seconds to wait for the Deep Research report (default: 600).",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    verbose = args.verbose or _is_verbose_mode()

    # Resolve prompt: CLI arg, --stdin, or auto-detect piped stdin
    prompt = args.prompt
    if args.stdin or (not prompt and not args.login and not args.batch
                      and not args.interactive and not sys.stdin.isatty()):
        prompt = sys.stdin.read().strip()

    if args.deep_research:
        run_deep_research(
            prompt=args.deep_research,
            timeout=args.dr_timeout,
            chrome_major=args.chrome_major,
            profile_dir=args.profile_dir,
            cookie_file=args.cookie_file,
            output_file=getattr(args, "output", None),
        )
    elif args.interactive:
        run_interactive(
            chrome_major=args.chrome_major,
            profile_dir=args.profile_dir,
            cookie_file=args.cookie_file,
        )
    elif args.login:
        run_login(
            chrome_major=args.chrome_major,
            profile_dir=args.profile_dir,
            cookie_file=args.cookie_file,
            force=args.force,
        )
    elif args.batch:
        run_batch(
            prompts_file=args.batch,
            output_file=args.output,
            chrome_major=args.chrome_major,
            resume=args.resume,
            profile_dir=args.profile_dir,
            cookie_file=args.cookie_file,
        )
    elif prompt:
        run_single(
            prompt,
            chrome_major=args.chrome_major,
            profile_dir=args.profile_dir,
            cookie_file=args.cookie_file,
            verbose=verbose,
        )
    else:
        print("Provide a prompt, use -i (interactive), --login, or --batch. Run with -h for help.",
              file=sys.stderr)
        sys.exit(1)
