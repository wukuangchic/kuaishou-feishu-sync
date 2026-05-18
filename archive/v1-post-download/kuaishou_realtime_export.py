#!/usr/bin/env python3
"""Export the realtime Kuaishou agent analysis table from an existing Chrome tab.

This script can either click through an existing Chrome tab, ask a logged-in
Chrome tab to POST the export without reading cookies, or direct-POST with a
manually provided Cookie header.

Prerequisite in Chrome on macOS:
  View -> Developer -> Allow JavaScript from Apple Events
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_TARGET_URL = "https://ugagent-partner.kuaishou.com/data/center/analyse/agent"
DIRECT_POST_ENDPOINT = "https://ugagent-partner.kuaishou.com/rest/n/agent/portalReport/downloadExcel"
EXCEL_PATTERNS = ("*.xlsx", "*.xls", "*.csv")
REALTIME_QUOTA_IDS = [11, 12, 13, 16]


class ChromeAutomationError(RuntimeError):
    pass


def run_osascript(script: str, args: list[str] | None = None) -> str:
    cmd = ["osascript", "-"]
    if args:
        cmd.extend(args)

    proc = subprocess.run(
        cmd,
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ChromeAutomationError(detail or "osascript failed")
    return proc.stdout.strip()


def find_tab(
    target_url: str,
    *,
    activate: bool = False,
    open_if_missing: bool = False,
) -> tuple[int, int, str]:
    script = r'''
on run argv
  set targetUrl to item 1 of argv
  set shouldActivate to ((item 2 of argv) is "1")
  set shouldOpen to ((item 3 of argv) is "1")
  tell application "Google Chrome"
    if not (exists window 1) then
      if shouldOpen then
        make new window
      else
        error "Google Chrome is not running."
      end if
    end if

    set foundWindow to 0
    set foundTab to 0
    set foundUrl to ""

    repeat with w from 1 to count windows
      repeat with t from 1 to count tabs of window w
        set tabUrl to URL of tab t of window w
        if tabUrl contains targetUrl then
          set foundWindow to w
          set foundTab to t
          set foundUrl to tabUrl
          exit repeat
        end if
      end repeat
      if foundWindow is not 0 then exit repeat
    end repeat

    if foundWindow is 0 then
      if shouldOpen then
        set foundWindow to 1
        set newTab to make new tab at end of tabs of window foundWindow with properties {URL:targetUrl}
        set foundTab to count tabs of window foundWindow
        set foundUrl to URL of newTab
      else
        error "Target tab not found: " & targetUrl
      end if
    end if

    if shouldActivate then
      set active tab index of window foundWindow to foundTab
      set index of window foundWindow to 1
      activate
    end if
    return (foundWindow as text) & linefeed & (foundTab as text) & linefeed & foundUrl
  end tell
end run
'''
    out = run_osascript(script, [target_url, "1" if activate else "0", "1" if open_if_missing else "0"])
    parts = out.splitlines()
    if len(parts) < 3:
        raise ChromeAutomationError(f"Unexpected tab lookup result: {out!r}")
    return int(parts[0]), int(parts[1]), parts[2]


def find_and_activate_tab(target_url: str) -> tuple[int, int, str]:
    return find_tab(target_url, activate=True, open_if_missing=False)


def activate_tab(window_index: int, tab_index: int) -> None:
    script = r'''
on run argv
  set windowIndex to item 1 of argv as integer
  set tabIndex to item 2 of argv as integer
  tell application "Google Chrome"
    set active tab index of window windowIndex to tabIndex
    set index of window windowIndex to 1
    activate
  end tell
end run
'''
    run_osascript(script, [str(window_index), str(tab_index)])


def reload_tab(window_index: int, tab_index: int) -> None:
    script = r'''
on run argv
  set windowIndex to item 1 of argv as integer
  set tabIndex to item 2 of argv as integer
  tell application "Google Chrome"
    reload tab tabIndex of window windowIndex
  end tell
end run
'''
    run_osascript(script, [str(window_index), str(tab_index)])


def execute_js(window_index: int, tab_index: int, js_code: str) -> str:
    script = r'''
on run argv
  set windowIndex to item 1 of argv as integer
  set tabIndex to item 2 of argv as integer
  set jsCode to item 3 of argv
  tell application "Google Chrome"
    tell tab tabIndex of window windowIndex
      return execute javascript jsCode
    end tell
  end tell
end run
'''
    return run_osascript(script, [str(window_index), str(tab_index), js_code])


def wait_for_page_ready(window_index: int, tab_index: int, timeout: float) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            state = execute_js(window_index, tab_index, "document.readyState")
            if state in {"interactive", "complete"}:
                return
        except ChromeAutomationError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise ChromeAutomationError(
        f"Timed out waiting for page readiness. Last error: {last_error}"
    )


def text_probe_js(label: str) -> str:
    return f"""
(() => {{
  const label = {json.dumps(label, ensure_ascii=False)};
  const target = label.replace(/\\s+/g, '');
  const selectors = [
    'button',
    '[role="button"]',
    '.ant-radio-button-wrapper',
    '.ant-btn',
    'label',
    'a',
    'span',
    'div'
  ];
  const normalize = (value) => (value || '').replace(/\\s+/g, '').trim();
  const isVisible = (el) => {{
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && Number(style.opacity || 1) !== 0
      && rect.width > 0
      && rect.height > 0
      && rect.bottom >= 0
      && rect.right >= 0
      && rect.top <= window.innerHeight
      && rect.left <= window.innerWidth;
  }};
  const nodes = Array.from(document.querySelectorAll(selectors.join(',')));
  const matches = nodes
    .filter(isVisible)
    .map((el) => {{
      const text = normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || el.title || '');
      const aria = normalize(el.getAttribute('aria-label') || '');
      let score = -1;
      if (text === target || aria === target) score = 100;
      else if (text.includes(target) && text.length <= target.length + 8) score = 70;
      else if (aria.includes(target)) score = 60;
      const className = String(el.className || '');
      if (score >= 0) {{
        if (el.tagName.toLowerCase() === 'button') score += 15;
        if (el.getAttribute('role') === 'button') score += 10;
        if (className.includes('ant-btn')) score += 8;
        if (className.includes('ant-radio')) score += 8;
        if (el.matches('[disabled], .disabled, .ant-btn-disabled, [aria-disabled="true"]')) score -= 1000;
      }}
      return {{
        el,
        score,
        text,
        tag: el.tagName.toLowerCase(),
        className: className.slice(0, 120)
      }};
    }})
    .filter((item) => item.score >= 0)
    .sort((a, b) => b.score - a.score);

  const best = matches[0];
  if (!best) {{
    return JSON.stringify({{ ok: false, label, reason: 'not_found' }});
  }}
  return JSON.stringify({{
    ok: true,
    label,
    text: best.text,
    tag: best.tag,
    className: best.className,
    score: best.score
  }});
}})()
"""


def click_text_js(label: str) -> str:
    return f"""
(() => {{
  const label = {json.dumps(label, ensure_ascii=False)};
  const target = label.replace(/\\s+/g, '');
  const selectors = [
    'button',
    '[role="button"]',
    '.ant-radio-button-wrapper',
    '.ant-btn',
    'label',
    'a',
    'span',
    'div'
  ];
  const normalize = (value) => (value || '').replace(/\\s+/g, '').trim();
  const isVisible = (el) => {{
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && Number(style.opacity || 1) !== 0
      && rect.width > 0
      && rect.height > 0
      && rect.bottom >= 0
      && rect.right >= 0
      && rect.top <= window.innerHeight
      && rect.left <= window.innerWidth;
  }};
  const scoreElement = (el) => {{
    const text = normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || el.title || '');
    const aria = normalize(el.getAttribute('aria-label') || '');
    let score = -1;
    if (text === target || aria === target) score = 100;
    else if (text.includes(target) && text.length <= target.length + 8) score = 70;
    else if (aria.includes(target)) score = 60;

    const className = String(el.className || '');
    if (score >= 0) {{
      if (el.tagName.toLowerCase() === 'button') score += 15;
      if (el.getAttribute('role') === 'button') score += 10;
      if (className.includes('ant-btn')) score += 8;
      if (className.includes('ant-radio')) score += 8;
      if (el.matches('[disabled], .disabled, .ant-btn-disabled, [aria-disabled="true"]')) score -= 1000;
    }}
    return {{
      el,
      score,
      text,
      tag: el.tagName.toLowerCase(),
      className: className.slice(0, 120)
    }};
  }};

  const best = Array.from(document.querySelectorAll(selectors.join(',')))
    .filter(isVisible)
    .map(scoreElement)
    .filter((item) => item.score >= 0)
    .sort((a, b) => b.score - a.score)[0];

  if (!best) {{
    return JSON.stringify({{ ok: false, label, reason: 'not_found' }});
  }}

  best.el.scrollIntoView({{ block: 'center', inline: 'center' }});
  const rect = best.el.getBoundingClientRect();
  const eventBase = {{
    bubbles: true,
    cancelable: true,
    view: window,
    clientX: rect.left + rect.width / 2,
    clientY: rect.top + rect.height / 2
  }};
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {{
    const EventClass = type.startsWith('pointer') && window.PointerEvent ? window.PointerEvent : window.MouseEvent;
    best.el.dispatchEvent(new EventClass(type, eventBase));
  }}
  if (typeof best.el.click === 'function') best.el.click();

  return JSON.stringify({{
    ok: true,
    label,
    clickedText: best.text,
    tag: best.tag,
    className: best.className,
    score: best.score
  }});
}})()
"""


def parse_json_result(raw: str, action: str) -> dict:
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChromeAutomationError(f"{action} returned non-JSON result: {raw!r}") from exc
    if not result.get("ok"):
        raise ChromeAutomationError(f"{action} failed: {result}")
    return result


def wait_for_text(
    window_index: int,
    tab_index: int,
    label: str,
    timeout: float,
) -> dict:
    deadline = time.time() + timeout
    last_result = ""
    while time.time() < deadline:
        try:
            raw = execute_js(window_index, tab_index, text_probe_js(label))
            result = json.loads(raw)
            if result.get("ok"):
                return result
            last_result = raw
        except (ChromeAutomationError, json.JSONDecodeError) as exc:
            last_result = str(exc)
        time.sleep(0.5)
    raise ChromeAutomationError(f'Timed out waiting for "{label}". Last result: {last_result}')


def click_text(window_index: int, tab_index: int, label: str, timeout: float) -> dict:
    wait_for_text(window_index, tab_index, label, timeout)
    raw = execute_js(window_index, tab_index, click_text_js(label))
    return parse_json_result(raw, f'Click "{label}"')


def start_post_download_js(payload: dict, filename: str) -> str:
    return f"""
(() => {{
  const payload = {json.dumps(payload, ensure_ascii=False)};
  const filename = {json.dumps(filename, ensure_ascii=False)};
  window.__kuaishouPostExportStatus = {{
    ok: false,
    done: false,
    startedAt: new Date().toISOString(),
    filename,
    payload
  }};

  (async () => {{
    try {{
      const response = await fetch('/rest/n/agent/portalReport/downloadExcel', {{
        method: 'POST',
        credentials: 'include',
        headers: {{
          'Accept': 'application/json, text/plain, */*',
          'Content-Type': 'application/json'
        }},
        body: JSON.stringify(payload)
      }});

      const contentType = response.headers.get('content-type') || '';
      const contentDisposition = response.headers.get('content-disposition') || '';
      const blob = await response.blob();
      if (!response.ok) {{
        let detail = '';
        try {{ detail = await blob.text(); }} catch (_) {{}}
        throw new Error(`HTTP ${{response.status}} ${{response.statusText}} ${{detail}}`);
      }}
      if (/json|html/i.test(contentType)) {{
        let detail = '';
        try {{ detail = await blob.text(); }} catch (_) {{}}
        throw new Error(`Expected file blob, got ${{contentType || 'unknown content'}}: ${{detail.slice(0, 1000)}}`);
      }}

      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      anchor.download = filename;
      anchor.style.display = 'none';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 40000);
      window.__kuaishouPostExportStatus = {{
        ok: true,
        done: true,
        status: response.status,
        contentType,
        contentDisposition,
        size: blob.size,
        filename,
        finishedAt: new Date().toISOString()
      }};
    }} catch (error) {{
      window.__kuaishouPostExportStatus = {{
        ok: false,
        done: true,
        filename,
        error: String(error && error.message ? error.message : error),
        finishedAt: new Date().toISOString()
      }};
    }}
  }})();

  return JSON.stringify({{ ok: true, started: true, filename, payload }});
}})()
"""


def get_post_status_js() -> str:
    return """
(() => JSON.stringify(window.__kuaishouPostExportStatus || {
  ok: false,
  done: false,
  error: 'post export has not been started'
}))()
"""


def wait_for_post_status(window_index: int, tab_index: int, timeout: float) -> dict:
    deadline = time.time() + timeout
    last_status: dict | str = ""
    while time.time() < deadline:
        raw = execute_js(window_index, tab_index, get_post_status_js())
        try:
            status = json.loads(raw)
        except json.JSONDecodeError:
            status = {"ok": False, "done": False, "raw": raw}
        last_status = status
        if status.get("done"):
            if status.get("ok"):
                return status
            raise ChromeAutomationError(f"POST export failed: {status.get('error') or status}")
        time.sleep(0.5)
    raise ChromeAutomationError(f"Timed out waiting for POST export. Last status: {last_status}")


def build_realtime_post_payload(args: argparse.Namespace) -> dict:
    payload: dict = {
        "startTime": args.date,
        "endTime": args.date,
        "realTimeDetailAggr": 1 if args.detail == "all" else 0,
        "timeType": 1,
        "quotaIdList": REALTIME_QUOTA_IDS,
        "dataType": 3,
    }
    optional_filters = {
        "productIdList": args.product_id,
        "mediaList": args.media,
        "channelList": args.channel,
        "subchannel": args.subchannel,
        "adid": args.adid,
    }
    for key, value in optional_filters.items():
        if value:
            payload[key] = value
    return payload


def run_post_export(
    window_index: int,
    tab_index: int,
    args: argparse.Namespace,
    download_dir: Path,
) -> int:
    payload = build_realtime_post_payload(args)
    mmdd = dt.datetime.strptime(args.date, "%Y-%m-%d").strftime("%m%d")
    filename = args.filename or f"实时{mmdd}_{args.detail}.csv"

    if args.dry_run:
        print("POST endpoint: /rest/n/agent/portalReport/downloadExcel")
        print("POST payload:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("Dry run finished. No POST request was sent.")
        return 0

    max_attempts = max(1, args.login_retries + 1)
    for attempt in range(1, max_attempts + 1):
        try:
            wait_for_page_ready(window_index, tab_index, args.timeout)
            started_at = time.time()
            result = parse_json_result(
                execute_js(window_index, tab_index, start_post_download_js(payload, filename)),
                "Start POST export",
            )
            print(f"Started POST export: {result.get('filename')}")

            status = wait_for_post_status(window_index, tab_index, args.download_timeout)
            print(
                "POST export response: "
                f"{status.get('status')} {status.get('contentType')} {status.get('size')} bytes"
            )

            downloaded = wait_for_download(download_dir, started_at, args.download_timeout)
            if downloaded:
                print(f"Download detected: {downloaded}")
            else:
                print(
                    "POST export finished, but no new Excel/CSV file was detected before timeout. "
                    "Check Chrome's download bar or pass --download-dir if Chrome saves elsewhere."
                )
            return 0
        except ChromeAutomationError as exc:
            if args.no_login_prompt or attempt >= max_attempts:
                raise
            print(f"POST export did not produce a file: {exc}")
            print("Opening the Kuaishou tab. Please log in there, then press Enter here to retry.")
            activate_tab(window_index, tab_index)
            try:
                input()
            except EOFError as input_exc:
                raise ChromeAutomationError("Login prompt could not read Enter from stdin.") from input_exc
            reload_tab(window_index, tab_index)

    raise ChromeAutomationError("POST export retry loop ended unexpectedly.")


def normalize_cookie_header(value: str) -> str:
    cookie = value.strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()
    return cookie


def get_direct_cookie(args: argparse.Namespace) -> str:
    if args.cookie:
        return normalize_cookie_header(args.cookie)
    if args.cookie_file:
        return normalize_cookie_header(Path(args.cookie_file).expanduser().read_text().strip())
    env_cookie = os.environ.get("KUAISHOU_COOKIE", "")
    if env_cookie:
        return normalize_cookie_header(env_cookie)
    raise ChromeAutomationError(
        "Direct POST needs a Cookie header. Set KUAISHOU_COOKIE or pass --cookie-file."
    )


def unique_output_path(download_dir: Path, filename: str) -> Path:
    path = download_dir / filename
    if not path.exists():
        return path
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.stem}-{stamp}{path.suffix}")


def run_direct_post_export(args: argparse.Namespace, download_dir: Path) -> int:
    payload = build_realtime_post_payload(args)
    mmdd = dt.datetime.strptime(args.date, "%Y-%m-%d").strftime("%m%d")
    filename = args.filename or f"实时{mmdd}_{args.detail}.csv"

    print(f"POST endpoint: {DIRECT_POST_ENDPOINT}")
    print("POST payload:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("Dry run finished. No POST request was sent.")
        return 0

    cookie = get_direct_cookie(args)
    if not cookie:
        raise ChromeAutomationError("Cookie header is empty.")

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        DIRECT_POST_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": cookie,
            "Origin": "https://ugagent-partner.kuaishou.com",
            "Referer": DEFAULT_TARGET_URL,
            "User-Agent": args.user_agent,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            content_type = response.headers.get("content-type", "")
            data = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ChromeAutomationError(f"HTTP {exc.code}: {detail[:1200]}") from exc
    except urllib.error.URLError as exc:
        raise ChromeAutomationError(f"Direct POST request failed: {exc}") from exc

    if "json" in content_type.lower() or "html" in content_type.lower():
        detail = data.decode("utf-8", errors="replace")
        raise ChromeAutomationError(f"Expected file response, got {content_type}: {detail[:1200]}")

    output_path = unique_output_path(download_dir, filename)
    output_path.write_bytes(data)
    print(f"POST response: {status} {content_type} {len(data)} bytes")
    print(f"Saved: {output_path}")
    return 0


def recent_excel_files(download_dir: Path, started_at: float) -> list[Path]:
    files: list[Path] = []
    for pattern in EXCEL_PATTERNS:
        files.extend(download_dir.glob(pattern))
    return sorted(
        {
            path
            for path in files
            if path.is_file() and path.stat().st_mtime >= started_at - 1
        },
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def wait_for_download(download_dir: Path, started_at: float, timeout: float) -> Path | None:
    deadline = time.time() + timeout
    latest: Path | None = None
    while time.time() < deadline:
        recent = recent_excel_files(download_dir, started_at)
        if recent:
            latest = recent[0]
            partials = [
                path
                for path in download_dir.glob("*.crdownload")
                if path.stat().st_mtime >= started_at - 1
            ]
            if not partials:
                return latest
        time.sleep(1)
    return latest


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh Kuaishou agent analysis in Chrome and export the realtime table."
    )
    parser.add_argument("--url", default=DEFAULT_TARGET_URL, help="target Chrome tab URL substring")
    parser.add_argument(
        "--download-dir",
        default=str(Path.home() / "Downloads"),
        help="directory where Chrome saves exported files",
    )
    parser.add_argument("--timeout", type=positive_float, default=60.0, help="page/button wait timeout")
    parser.add_argument(
        "--download-timeout",
        type=positive_float,
        default=120.0,
        help="how long to wait for a new Excel/CSV file after clicking download",
    )
    parser.add_argument(
        "--delay-after-query",
        type=positive_float,
        default=2.0,
        help="seconds to wait after clicking 查询; minimum is forced to 2 seconds",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="find the tab and verify buttons without refreshing, querying, or downloading",
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="export by POSTing in a logged-in Chrome tab without reading cookies",
    )
    parser.add_argument(
        "--direct-post",
        action="store_true",
        help="export by direct Python POST without opening or controlling Chrome",
    )
    parser.add_argument(
        "--date",
        default=dt.date.today().isoformat(),
        help="realtime report date for --post, in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--detail",
        choices=("hour", "all"),
        default="hour",
        help="realtime detail mode for --post: hour=按小时, all=汇总",
    )
    parser.add_argument("--filename", help="download filename for --post")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="activate the Chrome tab before --post; otherwise it stays in the background unless login is needed",
    )
    parser.add_argument(
        "--login-retries",
        type=int,
        default=1,
        help="for --post, prompt for manual login and retry this many times after a failed export",
    )
    parser.add_argument(
        "--no-login-prompt",
        action="store_true",
        help="for --post, fail immediately instead of opening Chrome for manual login",
    )
    parser.add_argument(
        "--cookie",
        help="Cookie header for --direct-post; prefer KUAISHOU_COOKIE or --cookie-file",
    )
    parser.add_argument("--cookie-file", help="file containing the Cookie header for --direct-post")
    parser.add_argument(
        "--user-agent",
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        help="User-Agent for --direct-post",
    )
    parser.add_argument(
        "--product-id",
        action="append",
        type=int,
        help="optional productIdList filter for --post; can be repeated",
    )
    parser.add_argument(
        "--media",
        action="append",
        help="optional mediaList filter for --post; can be repeated",
    )
    parser.add_argument(
        "--channel",
        action="append",
        help="optional channelList filter for --post; can be repeated",
    )
    parser.add_argument(
        "--subchannel",
        action="append",
        help="optional subchannel filter for --post; can be repeated",
    )
    parser.add_argument(
        "--adid",
        action="append",
        help="optional adid filter for --post; can be repeated",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    download_dir = Path(args.download_dir).expanduser().resolve()
    delay_after_query = max(2.0, args.delay_after_query)

    if not download_dir.exists():
        raise ChromeAutomationError(f"Download directory does not exist: {download_dir}")
    try:
        dt.datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError as exc:
        raise ChromeAutomationError("--date must be in YYYY-MM-DD format") from exc
    if args.login_retries < 0:
        raise ChromeAutomationError("--login-retries must be zero or greater")

    if args.direct_post:
        return run_direct_post_export(args, download_dir)

    if args.post:
        window_index, tab_index, found_url = find_tab(
            args.url,
            activate=args.foreground,
            open_if_missing=True,
        )
        print(f"Found Chrome tab: {found_url}")
        return run_post_export(window_index, tab_index, args, download_dir)

    window_index, tab_index, found_url = find_and_activate_tab(args.url)
    print(f"Found Chrome tab: {found_url}")

    if args.dry_run:
        wait_for_page_ready(window_index, tab_index, args.timeout)
        for label in ("实时", "查询", "下载表格"):
            result = wait_for_text(window_index, tab_index, label, args.timeout)
            print(f"OK: found {label} -> {result.get('tag')} {result.get('text')}")
        print("Dry run finished. No page actions were performed.")
        return 0

    print("Refreshing page...")
    reload_tab(window_index, tab_index)
    wait_for_page_ready(window_index, tab_index, args.timeout)

    for label in ("实时", "查询"):
        result = click_text(window_index, tab_index, label, args.timeout)
        print(f"Clicked {label}: {result.get('clickedText')}")
        if label == "查询":
            time.sleep(delay_after_query)

    started_at = time.time()
    result = click_text(window_index, tab_index, "下载表格", args.timeout)
    print(f"Clicked 下载表格: {result.get('clickedText')}")

    downloaded = wait_for_download(download_dir, started_at, args.download_timeout)
    if downloaded:
        print(f"Download detected: {downloaded}")
    else:
        print(
            "Clicked download, but no new Excel/CSV file was detected before timeout. "
            "Check Chrome's download bar or pass --download-dir if Chrome saves elsewhere."
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ChromeAutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if "--direct-post" not in sys.argv:
            print(
                "Tip: in Chrome, enable View -> Developer -> Allow JavaScript from Apple Events.",
                file=sys.stderr,
            )
        raise SystemExit(1)
