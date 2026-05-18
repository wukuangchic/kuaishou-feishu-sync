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
import csv
import datetime as dt
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from typing import Any


DEFAULT_TARGET_URL = "https://ugagent-partner.kuaishou.com/data/center/analyse/agent"
DIRECT_POST_ENDPOINT = "https://ugagent-partner.kuaishou.com/rest/n/agent/portalReport/downloadExcel"
DEFAULT_FEISHU_URL = "https://ujumedia.feishu.cn/wiki/SsVAwy1bSiDIaCkBt0ccDlftn0c?sheet=a0545c"
DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn"
EXCEL_PATTERNS = ("*.xlsx", "*.xls", "*.csv")
REALTIME_QUOTA_IDS = [11, 12, 13, 16]
EXPORT_HEADER_ALIASES = {
    "时间": "日期",
    "渠道": "渠道号",
}
SYNC_KEY_HEADERS = ("日期", "渠道号")
DATE_HEADERS = {"日期"}
TEXT_HEADERS = {"渠道号", "产品"}
SPREADSHEET_EPOCH = dt.datetime(1899, 12, 30)


class ChromeAutomationError(RuntimeError):
    pass


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


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


def expand_chrome_path(value: str) -> Path:
    return Path(value.replace("$HOME", str(Path.home()))).expanduser()


def guess_chrome_download_dir() -> Path:
    chrome_root = Path.home() / "Library/Application Support/Google/Chrome"
    local_state_path = chrome_root / "Local State"
    fallback = Path.home() / "Downloads"
    icloud_downloads = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/Downloads"
    try:
        local_state = json.loads(local_state_path.read_text())
        profile_name = (
            local_state.get("profile", {}).get("last_used")
            or local_state.get("profile", {}).get("last_active_profiles", ["Default"])[0]
            or "Default"
        )
        preferences_path = chrome_root / profile_name / "Preferences"
        preferences = json.loads(preferences_path.read_text())
        default_directory = preferences.get("download", {}).get("default_directory")
        if default_directory:
            chrome_download_dir = expand_chrome_path(default_directory)
            if chrome_download_dir.exists():
                return chrome_download_dir
    except Exception:
        pass
    if icloud_downloads.exists():
        return icloud_downloads
    return fallback


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
                if args.sync_feishu:
                    sync_export_file_to_feishu(downloaded, args)
            else:
                message = (
                    "POST export finished, but no new Excel/CSV file was detected before timeout. "
                    "Check Chrome's download bar or pass --download-dir if Chrome saves elsewhere."
                )
                if args.sync_feishu:
                    raise ChromeAutomationError(message)
                print(message)
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
    if args.sync_feishu:
        sync_export_file_to_feishu(output_path, args)
    return 0


def column_letter(index: int) -> str:
    if index < 1:
        raise ValueError("column index must be one-based")
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def is_ooxml_file(path: Path) -> bool:
    with path.open("rb") as file:
        return file.read(4) == b"PK\x03\x04"


def read_shared_strings(zip_file: zipfile.ZipFile) -> list[str]:
    try:
        raw = zip_file.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings: list[str] = []
    for item in root.findall("main:si", namespace):
        parts = [node.text or "" for node in item.findall(".//main:t", namespace)]
        strings.append("".join(parts))
    return strings


def first_sheet_path(zip_file: zipfile.ZipFile) -> str:
    try:
        workbook = ET.fromstring(zip_file.read("xl/workbook.xml"))
        rels = ET.fromstring(zip_file.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        return "xl/worksheets/sheet1.xml"

    ns_workbook = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    ns_rels = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
    first_sheet = workbook.find("main:sheets/main:sheet", ns_workbook)
    if first_sheet is None:
        return "xl/worksheets/sheet1.xml"
    rel_id = first_sheet.attrib.get(f"{{{ns_workbook['rel']}}}id")
    if not rel_id:
        return "xl/worksheets/sheet1.xml"
    for rel in rels.findall("rel:Relationship", ns_rels):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target", "worksheets/sheet1.xml")
            return "xl/" + target.lstrip("/")
    return "xl/worksheets/sheet1.xml"


def cell_column_index(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    index = 0
    for char in letters:
        index = index * 26 + ord(char) - 64
    return index


def read_xlsx_rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as zip_file:
        shared_strings = read_shared_strings(zip_file)
        sheet_xml = zip_file.read(first_sheet_path(zip_file))
    root = ET.fromstring(sheet_xml)
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[list[str]] = []
    for row in root.findall(".//main:sheetData/main:row", namespace):
        values: list[str] = []
        for cell in row.findall("main:c", namespace):
            ref = cell.attrib.get("r", "")
            col_idx = cell_column_index(ref)
            while len(values) < col_idx - 1:
                values.append("")
            cell_type = cell.attrib.get("t")
            if cell_type == "inlineStr":
                text = "".join(node.text or "" for node in cell.findall(".//main:t", namespace))
            else:
                value_node = cell.find("main:v", namespace)
                raw = value_node.text if value_node is not None else ""
                if cell_type == "s" and raw != "":
                    text = shared_strings[int(raw)]
                else:
                    text = raw or ""
            values.append(str(text))
        rows.append(values)
    return rows


def read_csv_rows(path: Path) -> list[list[str]]:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as file:
                return [[str(cell) for cell in row] for row in csv.reader(file)]
        except UnicodeDecodeError:
            continue
    raise ChromeAutomationError(f"Unable to decode CSV file: {path}")


def normalize_header(header: Any) -> str:
    text = str(header or "").strip()
    return EXPORT_HEADER_ALIASES.get(text, text)


def parse_datetime_text(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def datetime_to_spreadsheet_serial(value: dt.datetime) -> float | int:
    delta = value - SPREADSHEET_EPOCH
    serial = delta.days + (delta.seconds + delta.microseconds / 1_000_000) / 86400
    if abs(serial - round(serial)) < 1e-9:
        return int(round(serial))
    return round(serial, 10)


def snap_datetime_to_hour(value: dt.datetime) -> dt.datetime:
    base = value.replace(minute=0, second=0, microsecond=0)
    if value.minute >= 30 or (value.minute == 29 and value.second >= 30):
        base += dt.timedelta(hours=1)
    return base


def spreadsheet_serial_to_datetime(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        serial = float(text)
    except ValueError:
        return None
    whole_days = int(serial)
    seconds = round((serial - whole_days) * 86400)
    if seconds >= 86400:
        whole_days += 1
        seconds -= 86400
    return SPREADSHEET_EPOCH + dt.timedelta(days=whole_days, seconds=seconds)


def normalize_key_value(header: str, value: Any) -> str:
    if header in DATE_HEADERS:
        parsed = parse_datetime_text(value) or spreadsheet_serial_to_datetime(value)
        if parsed:
            parsed = snap_datetime_to_hour(parsed)
            if parsed.hour or parsed.minute or parsed.second:
                return parsed.strftime("%Y-%m-%d %H:%M")
            return parsed.strftime("%Y-%m-%d")
    return str(value or "").strip()


def coerce_feishu_cell_value(header: str, value: Any) -> Any:
    text = str(value or "").strip()
    if text == "":
        return text
    if header in DATE_HEADERS:
        parsed = parse_datetime_text(text)
        if parsed:
            return datetime_to_spreadsheet_serial(parsed)
        return text
    if header in TEXT_HEADERS:
        return text
    normalized = text.replace(",", "")
    if re.fullmatch(r"-?\d+", normalized):
        return int(normalized)
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", normalized):
        return float(normalized)
    return text


def read_export_table(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        raise ChromeAutomationError(f"Export file does not exist: {path}")
    rows = read_xlsx_rows(path) if is_ooxml_file(path) else read_csv_rows(path)
    rows = [row for row in rows if any(str(cell or "").strip() for cell in row)]
    if not rows:
        raise ChromeAutomationError(f"Export file has no rows: {path}")
    headers = [normalize_header(cell) for cell in rows[0]]
    if len(headers) != len(set(headers)):
        raise ChromeAutomationError(f"Export file has duplicate headers after normalization: {headers}")
    missing_keys = [header for header in SYNC_KEY_HEADERS if header not in headers]
    if missing_keys:
        raise ChromeAutomationError(f"Export file missing key headers: {', '.join(missing_keys)}")

    records: "OrderedDict[tuple[str, str], dict[str, str]]" = OrderedDict()
    for row in rows[1:]:
        values = [str(cell or "").strip() for cell in row]
        record = {header: values[index] if index < len(values) else "" for index, header in enumerate(headers)}
        key = tuple(record.get(header, "") for header in SYNC_KEY_HEADERS)
        if not all(key):
            continue
        records[key] = record
    return headers, list(records.values())


def parse_feishu_url(feishu_url: str) -> tuple[str, str | None, bool]:
    parsed = urllib.parse.urlparse(feishu_url)
    query = urllib.parse.parse_qs(parsed.query)
    sheet_id = (query.get("sheet") or [None])[0]
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[-2] == "wiki":
        return path_parts[-1], sheet_id, True
    if len(path_parts) >= 2 and path_parts[-2] in {"sheets", "sheet"}:
        return path_parts[-1], sheet_id, False
    return feishu_url, sheet_id, False


class FeishuSheetClient:
    def __init__(self, args: argparse.Namespace):
        self.base_url = args.feishu_base_url.rstrip("/")
        self.app_id = args.feishu_app_id or os.environ.get("FEISHU_APP_ID")
        self.app_secret = args.feishu_app_secret or os.environ.get("FEISHU_APP_SECRET")
        self._tenant_access_token = args.feishu_tenant_access_token or os.environ.get(
            "FEISHU_TENANT_ACCESS_TOKEN"
        )
        if args.feishu_insecure:
            self.ssl_context = ssl._create_unverified_context()
        elif args.feishu_ca_file:
            self.ssl_context = ssl.create_default_context(cafile=args.feishu_ca_file)
        else:
            try:
                import certifi  # type: ignore

                self.ssl_context = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                self.ssl_context = None

    def tenant_access_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        if not self.app_id or not self.app_secret:
            raise ChromeAutomationError(
                "Feishu sync needs FEISHU_APP_ID and FEISHU_APP_SECRET environment variables."
            )
        payload = self.request_json(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            body={"app_id": self.app_id, "app_secret": self.app_secret},
            auth=False,
        )
        token = payload.get("tenant_access_token")
        if not token:
            raise ChromeAutomationError("Feishu auth returned no tenant_access_token.")
        self._tenant_access_token = token
        return token

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if auth:
            headers["Authorization"] = f"Bearer {self.tenant_access_token()}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60, context=self.ssl_context) as response:
                response_data = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ChromeAutomationError(f"Feishu HTTP {exc.code}: {detail[:1200]}") from exc
        except urllib.error.URLError as exc:
            raise ChromeAutomationError(f"Feishu request failed: {exc}") from exc
        payload = json.loads(response_data or "{}")
        if payload.get("code", 0) != 0:
            raise ChromeAutomationError(f"Feishu API error: {json.dumps(payload, ensure_ascii=False)}")
        return payload

    def resolve_spreadsheet(self, feishu_url: str) -> tuple[str, str]:
        token, sheet_id, is_wiki = parse_feishu_url(feishu_url)
        spreadsheet_token = token
        if is_wiki:
            payload = self.request_json(
                "GET",
                "/open-apis/wiki/v2/spaces/get_node",
                query={"token": token},
            )
            node = payload.get("data", {}).get("node", {})
            spreadsheet_token = node.get("obj_token") or spreadsheet_token
        sheets = self.query_sheets(spreadsheet_token)
        if sheet_id:
            return spreadsheet_token, sheet_id
        if not sheets:
            raise ChromeAutomationError("Feishu spreadsheet has no sheets.")
        return spreadsheet_token, sheets[0]["sheet_id"]

    def query_sheets(self, spreadsheet_token: str) -> list[dict[str, Any]]:
        payload = self.request_json(
            "GET",
            f"/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query",
        )
        return payload.get("data", {}).get("sheets", [])

    def get_sheet_info(self, spreadsheet_token: str, sheet_id: str) -> dict[str, Any]:
        sheets = self.query_sheets(spreadsheet_token)
        for sheet in sheets:
            if sheet.get("sheet_id") == sheet_id:
                return sheet
        raise ChromeAutomationError(f"Sheet id not found in spreadsheet: {sheet_id}")

    def read_values(self, spreadsheet_token: str, sheet_id: str, cols: int, rows: int) -> list[list[str]]:
        safe_cols = max(cols, 26)
        safe_rows = max(rows, 200)
        read_range = f"{sheet_id}!A1:{column_letter(safe_cols)}{safe_rows}"
        encoded_range = urllib.parse.quote(read_range, safe="")
        payload = self.request_json(
            "GET",
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{encoded_range}",
        )
        values = payload.get("data", {}).get("valueRange", {}).get("values") or []
        return [[str(cell or "").strip() for cell in row] for row in values]

    def insert_dimension(
        self,
        spreadsheet_token: str,
        sheet_id: str,
        major_dimension: str,
        start_index: int,
        end_index: int,
    ) -> None:
        body = {
            "dimension": {
                "sheetId": sheet_id,
                "majorDimension": major_dimension,
                "startIndex": start_index,
                "endIndex": end_index,
            },
            "inheritStyle": "BEFORE",
        }
        self.request_json(
            "POST",
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/insert_dimension_range",
            body=body,
        )

    def batch_update_values(
        self,
        spreadsheet_token: str,
        value_ranges: list[dict[str, Any]],
    ) -> None:
        if not value_ranges:
            return
        for index in range(0, len(value_ranges), 50):
            chunk = value_ranges[index : index + 50]
            self.request_json(
                "POST",
                f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_batch_update",
                body={"valueRanges": chunk},
            )

    def append_values(
        self,
        spreadsheet_token: str,
        value_range: dict[str, Any],
    ) -> None:
        self.request_json(
            "POST",
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_append",
            body={"valueRange": value_range},
        )

    def set_cell_style(
        self,
        spreadsheet_token: str,
        sheet_range: str,
        style: dict[str, Any],
    ) -> None:
        self.request_json(
            "PUT",
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/style",
            body={"appendStyle": {"range": sheet_range, "style": style}},
        )


def get_grid_count(sheet_info: dict[str, Any], name: str, default: int) -> int:
    grid = sheet_info.get("grid_properties") or sheet_info.get("gridProperties") or {}
    return int(grid.get(name) or grid.get(name.replace("_", "")) or default)


def normalize_existing_headers(existing_values: list[list[str]]) -> list[str]:
    if not existing_values:
        return []
    return [str(cell or "").strip() for cell in existing_values[0]]


def trim_trailing_empty_rows(values: list[list[str]]) -> list[list[str]]:
    trimmed = values[:]
    while trimmed and not any(str(cell or "").strip() for cell in trimmed[-1]):
        trimmed.pop()
    return trimmed


def build_existing_index(existing_values: list[list[str]], headers: list[str]) -> dict[tuple[str, str], int]:
    index: dict[tuple[str, str], int] = {}
    if not headers:
        return index
    for row_offset, row in enumerate(existing_values[1:], start=2):
        record = {header: row[col] if col < len(row) else "" for col, header in enumerate(headers)}
        key = tuple(normalize_key_value(header, record.get(header, "")) for header in SYNC_KEY_HEADERS)
        if all(key):
            index[key] = row_offset
    return index


def compact_update_ranges(
    sheet_id: str,
    max_col: int,
    row_values: list[tuple[int, list[str]]],
) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    if not row_values:
        return ranges
    sorted_rows = sorted(row_values, key=lambda item: item[0])
    block_start = sorted_rows[0][0]
    block_rows = [sorted_rows[0][1]]
    previous_row = block_start
    for row_number, values in sorted_rows[1:]:
        if row_number == previous_row + 1:
            block_rows.append(values)
        else:
            end_row = previous_row
            ranges.append(
                {
                    "range": f"{sheet_id}!A{block_start}:{column_letter(max_col)}{end_row}",
                    "values": block_rows,
                }
            )
            block_start = row_number
            block_rows = [values]
        previous_row = row_number
    ranges.append(
        {
            "range": f"{sheet_id}!A{block_start}:{column_letter(max_col)}{previous_row}",
            "values": block_rows,
        }
    )
    return ranges


def apply_date_column_styles(
    client: FeishuSheetClient,
    spreadsheet_token: str,
    sheet_id: str,
    headers: list[str],
    max_row: int,
) -> None:
    if max_row < 2:
        return
    for header in DATE_HEADERS:
        if header not in headers:
            continue
        col = column_letter(headers.index(header) + 1)
        try:
            client.set_cell_style(
                spreadsheet_token,
                f"{sheet_id}!{col}2:{col}{max_row}",
                {"formatter": "yyyy/MM/dd HH:mm:ss"},
            )
        except ChromeAutomationError as exc:
            print(f"WARNING: date style update failed for {header}: {exc}")


def sync_export_file_to_feishu(path: Path, args: argparse.Namespace) -> dict[str, int]:
    export_headers, export_records = read_export_table(path)
    client = FeishuSheetClient(args)
    spreadsheet_token, sheet_id = client.resolve_spreadsheet(args.feishu_url)
    sheet_info = client.get_sheet_info(spreadsheet_token, sheet_id)
    row_count = get_grid_count(sheet_info, "row_count", 200)
    col_count = get_grid_count(sheet_info, "column_count", 26)
    existing_values = trim_trailing_empty_rows(client.read_values(spreadsheet_token, sheet_id, col_count, row_count))
    existing_headers = normalize_existing_headers(existing_values)
    existing_headers = [header for header in existing_headers if header]
    final_headers = existing_headers[:] if existing_headers else []
    missing_headers = [header for header in export_headers if header not in final_headers]
    final_headers.extend(missing_headers)
    for key_header in SYNC_KEY_HEADERS:
        if key_header not in final_headers:
            final_headers.append(key_header)

    max_col = len(final_headers)
    existing_index = build_existing_index(existing_values, final_headers)
    updates: list[tuple[int, list[str]]] = []
    appends: list[list[str]] = []
    append_start_row = max(len(existing_values) + 1, 2)

    for record in export_records:
        key = tuple(normalize_key_value(header, record.get(header, "")) for header in SYNC_KEY_HEADERS)
        values = [
            coerce_feishu_cell_value(header, record.get(header, ""))
            for header in final_headers
        ]
        if key in existing_index:
            updates.append((existing_index[key], values))
        else:
            appends.append(values)

    summary = {
        "source_rows": len(export_records),
        "missing_headers": len(missing_headers),
        "updates": len(updates),
        "appends": len(appends),
    }
    print("Feishu sync plan:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Feishu target: spreadsheet={spreadsheet_token} sheet={sheet_id}")
    if args.sync_dry_run:
        print("Sync dry run finished. No Feishu changes were made.")
        return summary

    if max_col > col_count:
        client.insert_dimension(spreadsheet_token, sheet_id, "COLUMNS", col_count, max_col)

    value_ranges = [
        {"range": f"{sheet_id}!A1:{column_letter(max_col)}1", "values": [final_headers]},
    ]
    value_ranges.extend(compact_update_ranges(sheet_id, max_col, updates))
    client.batch_update_values(spreadsheet_token, value_ranges)
    if appends:
        append_end_row = append_start_row + len(appends) - 1
        client.append_values(
            spreadsheet_token,
            {
                "range": f"{sheet_id}!A{append_start_row}:{column_letter(max_col)}{append_end_row}",
                "values": appends,
            },
        )
    final_data_row = max(
        [row_number for row_number, _ in updates] + ([append_start_row + len(appends) - 1] if appends else [1])
    )
    apply_date_column_styles(client, spreadsheet_token, sheet_id, final_headers, final_data_row)
    print("Feishu sync finished.")
    return summary


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
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Refresh Kuaishou agent analysis in Chrome and export the realtime table."
    )
    parser.add_argument("--url", default=DEFAULT_TARGET_URL, help="target Chrome tab URL substring")
    parser.add_argument(
        "--download-dir",
        default=None,
        help="directory where Chrome saves exported files; defaults to Chrome's configured download folder",
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
        "--sync-feishu",
        action="store_true",
        help="sync the exported file to the configured Feishu sheet after download",
    )
    parser.add_argument(
        "--sync-file",
        help="sync an existing Kuaishou export file to Feishu and skip downloading",
    )
    parser.add_argument(
        "--sync-dry-run",
        action="store_true",
        help="build the Feishu sync plan without writing to Feishu",
    )
    parser.add_argument(
        "--feishu-url",
        default=os.environ.get("FEISHU_KS_URL") or os.environ.get("FEISHU_URL", DEFAULT_FEISHU_URL),
        help="target Feishu wiki/sheet URL",
    )
    parser.add_argument("--feishu-app-id", help="Feishu app id; defaults to FEISHU_APP_ID")
    parser.add_argument("--feishu-app-secret", help="Feishu app secret; defaults to FEISHU_APP_SECRET")
    parser.add_argument(
        "--feishu-tenant-access-token",
        help="optional pre-fetched tenant access token; defaults to FEISHU_TENANT_ACCESS_TOKEN",
    )
    parser.add_argument("--feishu-base-url", default=DEFAULT_FEISHU_BASE_URL, help="Feishu OpenAPI base URL")
    parser.add_argument("--feishu-ca-file", help="custom CA bundle for Feishu HTTPS requests")
    parser.add_argument(
        "--feishu-insecure",
        action="store_true",
        help="disable Feishu HTTPS certificate verification; use only for local certificate debugging",
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
    download_dir = Path(args.download_dir).expanduser().resolve() if args.download_dir else guess_chrome_download_dir()
    delay_after_query = max(2.0, args.delay_after_query)

    if not download_dir.exists():
        raise ChromeAutomationError(f"Download directory does not exist: {download_dir}")
    try:
        dt.datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError as exc:
        raise ChromeAutomationError("--date must be in YYYY-MM-DD format") from exc
    if args.login_retries < 0:
        raise ChromeAutomationError("--login-retries must be zero or greater")

    if args.sync_file:
        sync_export_file_to_feishu(Path(args.sync_file).expanduser().resolve(), args)
        return 0

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
