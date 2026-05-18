#!/usr/bin/env python3
"""Fetch Tencent Ads account data from a logged-in Chrome tab and sync to Feishu.

The script asks the existing Chrome tab to issue same-origin POST requests, so
Chrome carries the live login state without this program exporting cookie text.

Prerequisite in Chrome on macOS:
  View -> Developer -> Allow JavaScript from Apple Events
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kuaishou_realtime_export import (  # noqa: E402
    ChromeAutomationError,
    FeishuSheetClient,
    activate_tab,
    column_letter,
    datetime_to_spreadsheet_serial,
    execute_js,
    find_tab,
    get_grid_count,
    load_dotenv,
    normalize_existing_headers,
    parse_datetime_text,
    spreadsheet_serial_to_datetime,
    trim_trailing_empty_rows,
    wait_for_page_ready,
    reload_tab,
)


DEFAULT_TARGET_URL = "https://ad.qq.com/cm/account"
DEFAULT_FEISHU_URL = "https://ujumedia.feishu.cn/wiki/SsVAwy1bSiDIaCkBt0ccDlftn0c?sheet=i7McmQ"
DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn"
DEFAULT_DYNAMIC_FIELDS = [
    "marketing_content",
    "status_text",
    "operation",
    "cost_price",
    "view_count",
    "valid_click_count",
    "cost",
    "conversions_count",
    "activated_count",
    "activated_cost",
    "retention_count",
    "is_rta",
    "account_alias",
]
SYNC_KEY_HEADERS = ("日期", "账户ID")
DATE_HEADERS = {"日期"}
TEXT_HEADERS = {
    "账户ID",
    "账户名称",
    "账户标签",
    "备注",
    "主体名称",
    "业务单元",
    "账户状态",
    "账户运营方",
    "账户是否启用",
    "RTA",
    "账户健康度",
}

BASE_HEADERS = [
    "日期",
    "账户ID",
    "账户名称",
    "账户标签",
    "备注",
    "主体ID",
    "主体名称",
    "业务单元ID",
    "业务单元",
    "代理商ID",
    "账户状态",
    "账户运营方",
    "账户是否启用",
    "日预算",
    "共享钱包余额",
    "曝光次数",
    "点击次数",
    "花费",
    "目标转化量",
    "APP激活次数",
    "APP激活成本",
    "次日留存次数",
    "RTA",
    "账户健康度",
]

DYNAMIC_FIELD_HEADERS = {
    "account_alias": "账户标签",
    "activated_cost": "APP激活成本",
    "activated_count": "APP激活次数",
    "comment": "备注",
    "conversions_count": "目标转化量",
    "cost": "花费",
    "cost_price": "出价",
    "is_rta": "RTA",
    "marketing_content": "账户名称",
    "operation": "操作",
    "retention_count": "次日留存次数",
    "status_text": "账户状态",
    "valid_click_count": "点击次数",
    "view_count": "曝光次数",
}

OPERATION_MODE = {
    0: "未设置",
    1: "客户自运营",
    2: "代理商代运营",
}

ENABLE_STATUS = {
    0: "启用中",
    1: "未启用",
}


def start_tencent_fetch_js(args: argparse.Namespace) -> str:
    return f"""
(() => {{
  const options = {{
    date: {json.dumps(args.date)},
    pageSize: {int(args.page_size)},
    maxPages: {int(args.max_pages)},
    dynamicFieldsFallback: {json.dumps(DEFAULT_DYNAMIC_FIELDS)}
  }};

  window.__tencentWebDataRows = [];
  window.__tencentWebDataStatus = {{
    done: false,
    ok: false,
    startedAt: new Date().toISOString()
  }};

  const fetchJson = async (basePath, path, body, method = 'POST') => {{
    const url = `${{location.protocol}}//${{location.host}}${{basePath}}${{path}}`;
    const response = await fetch(url, {{
      method,
      credentials: 'include',
      headers: {{
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json; charset=utf-8'
      }},
      body: method === 'GET' ? undefined : JSON.stringify(body || {{}})
    }});
    const text = await response.text();
    let payload;
    try {{
      payload = JSON.parse(text);
    }} catch (error) {{
      throw new Error(`Non-JSON response from ${{path}}: ${{text.slice(0, 500)}}`);
    }}
    if (!response.ok) {{
      throw new Error(`HTTP ${{response.status}} from ${{path}}: ${{text.slice(0, 500)}}`);
    }}
    if (payload.code !== undefined && payload.code !== 0) {{
      throw new Error(payload.message || payload.msg || `API code ${{payload.code}} from ${{path}}`);
    }}
    return payload.data || {{}};
  }};
  const parseListParams = () => {{
    try {{
      const params = new URLSearchParams(location.search);
      return JSON.parse(params.get('list_params') || '{{}}') || {{}};
    }} catch (error) {{
      return {{}};
    }}
  }};
  const toMillis = (date, endOfDay) => {{
    const suffix = endOfDay ? 'T23:59:59.000+08:00' : 'T00:00:00.000+08:00';
    return new Date(`${{date}}${{suffix}}`).getTime();
  }};
  const buildBody = (filterData, page, pageSize, dynamicFields, columnConfigId, userId) => {{
    const order = filterData.order || {{}};
    const dateRange = filterData.dateRange || [options.date, options.date];
    const body = {{ ...filterData }};
    delete body.account_id;
    delete body.order;
    delete body.keyAccount;
    delete body.dateRange;
    delete body.caliberType;
    body.page = page;
    body.page_size = pageSize;
    body.start_date_millons = toMillis(dateRange[0], false);
    body.end_date_millons = toMillis(dateRange[1], true);
    body.time_line = filterData.caliberType || 'REQUEST_TIME';
    if (filterData.keyAccount) body.is_top = 1;
    body.new_source = 1;
    if (order.type) body.sort_seq = String(order.type).toLowerCase();
    if (order.field) body.sort_field = order.field;
    body.dynamic_field_list = dynamicFields;
    body.columnConfigId = columnConfigId;
    body.user_id = userId;
    return body;
  }};

  (async () => {{
    try {{
      const loginInfo = await fetchJson('/tap/v1', '/login/login_info', {{}});
      const userId = loginInfo.user_id || loginInfo.login_user_id;
      if (!userId) throw new Error('腾讯登录态无有效 user_id，请在 Chrome 中重新登录。');

      const fields = options.dynamicFieldsFallback;
      const columnConfigId = 0;

      const pageParams = parseListParams();
      const dateRange = [options.date, options.date];
      const filterData = {{
        account_source: 'GDT_PLATFROM',
        filter_empty_data: 1,
        data_version: 'VERSION_ALL',
        keyAccount: false,
        caliberType: 'REQUEST_TIME',
        ...pageParams,
        dateRange
      }};
      const rows = [];
      const firstBody = buildBody(filterData, 1, options.pageSize, fields, columnConfigId, userId);
      const firstData = await fetchJson('/tap/v1', '/account_daily_report/account_list', firstBody);
      rows.push(...(firstData.list || []));
      const pageInfo = firstData.page_info || {{}};
      const totalPages = Number(pageInfo.total_page || 1);
      const cappedPages = Math.min(totalPages, options.maxPages);
      for (let page = 2; page <= cappedPages; page += 1) {{
        const body = buildBody(filterData, page, options.pageSize, fields, columnConfigId, userId);
        const data = await fetchJson('/tap/v1', '/account_daily_report/account_list', body);
        rows.push(...(data.list || []));
      }}
      window.__tencentWebDataRows = rows;
      window.__tencentWebDataStatus = {{
        done: true,
        ok: true,
        rowCount: rows.length,
        pageInfo,
        totalPages,
        fetchedPages: cappedPages,
        dateRange,
        columnConfigId,
        columnConfigName: 'default_fields',
        dynamicFields: fields,
        finishedAt: new Date().toISOString()
      }};
    }} catch (error) {{
      window.__tencentWebDataRows = [];
      window.__tencentWebDataStatus = {{
        done: true,
        ok: false,
        error: String(error && error.message ? error.message : error),
        finishedAt: new Date().toISOString()
      }};
    }}
  }})();

  return JSON.stringify({{ ok: true, started: true }});
}})()
"""


def poll_tencent_status(window_index: int, tab_index: int, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_status: Any = None
    while time.time() < deadline:
        raw = execute_js(
            window_index,
            tab_index,
            "JSON.stringify(window.__tencentWebDataStatus || {done:false, ok:false})",
        )
        try:
            status = json.loads(raw)
        except json.JSONDecodeError:
            status = {"done": False, "ok": False, "raw": raw}
        last_status = status
        if status.get("done"):
            if status.get("ok"):
                return status
            raise ChromeAutomationError(status.get("error") or json.dumps(status, ensure_ascii=False))
        time.sleep(0.5)
    raise ChromeAutomationError(f"Timed out waiting for Tencent data. Last status: {last_status}")


def collect_rows(window_index: int, tab_index: int, row_count: int, chunk_size: int = 100) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for start in range(0, row_count, chunk_size):
        js = (
            "JSON.stringify((window.__tencentWebDataRows || [])"
            f".slice({start}, {start + chunk_size}))"
        )
        chunk = json.loads(execute_js(window_index, tab_index, js) or "[]")
        rows.extend(chunk)
    return rows


def fetch_tencent_rows(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    window_index, tab_index, found_url = find_tab(
        args.url,
        activate=args.foreground,
        open_if_missing=True,
    )
    print(f"Found Chrome tab: {found_url}")
    max_attempts = max(1, args.login_retries + 1)
    for attempt in range(1, max_attempts + 1):
        try:
            wait_for_page_ready(window_index, tab_index, args.timeout)
            started = json.loads(execute_js(window_index, tab_index, start_tencent_fetch_js(args)))
            if not started.get("ok"):
                raise ChromeAutomationError(f"Unable to start Tencent fetch: {started}")
            status = poll_tencent_status(window_index, tab_index, args.timeout)
            rows = collect_rows(window_index, tab_index, int(status.get("rowCount") or 0))
            return rows, status
        except ChromeAutomationError as exc:
            if args.no_login_prompt or attempt >= max_attempts:
                raise
            print(f"Tencent fetch failed: {exc}")
            print("Opening the Tencent tab. Please log in there, then press Enter here to retry.")
            activate_tab(window_index, tab_index)
            try:
                input()
            except EOFError as input_exc:
                raise ChromeAutomationError("Login prompt could not read Enter from stdin.") from input_exc
            reload_tab(window_index, tab_index)
    raise ChromeAutomationError("Tencent fetch retry loop ended unexpectedly.")


def nested_value(row: dict[str, Any], key: str) -> Any:
    if key in row and row.get(key) is not None:
        return row.get(key)
    report = row.get("daily_report") or {}
    if key in report and report.get(key) is not None:
        return report.get(key)
    return ""


def to_number_or_text(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if text == "":
        return ""
    normalized = text.replace(",", "")
    if re.fullmatch(r"-?\d+", normalized):
        return int(normalized)
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", normalized):
        return float(normalized)
    return text


def daily_budget_yuan(value: Any) -> Any:
    number = to_number_or_text(value)
    if isinstance(number, (int, float)) and not isinstance(number, bool):
        return round(number / 100, 2)
    return number


def status_text(row: dict[str, Any]) -> str:
    value = nested_value(row, "status_text")
    if value:
        return str(value)
    merged_status = row.get("merged_status")
    if merged_status == 1:
        return "有效"
    return "" if merged_status in (None, "") else str(merged_status)


def normalize_record(row: dict[str, Any], report_date: str, dynamic_fields: list[str]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "日期": datetime_to_spreadsheet_serial(dt.datetime.strptime(report_date, "%Y-%m-%d")),
        "账户ID": str(row.get("account_id") or ""),
        "账户名称": row.get("corporation_name") or row.get("account_name") or "",
        "账户标签": row.get("account_alias") or "",
        "备注": row.get("comment") or nested_value(row, "comment"),
        "主体ID": row.get("business_id") or row.get("cm_id") or "",
        "主体名称": row.get("cm_name") or row.get("corporation_name") or "",
        "业务单元ID": row.get("business_unit_id") or "",
        "业务单元": row.get("business_unit_name") or "",
        "代理商ID": row.get("agency_id") or nested_value(row, "agency_account_id"),
        "账户状态": status_text(row),
        "账户运营方": OPERATION_MODE.get(row.get("operation_mode"), row.get("operation_mode") or ""),
        "账户是否启用": ENABLE_STATUS.get(
            row.get("forbidden_create_ad_status"),
            row.get("forbidden_create_ad_status") if row.get("forbidden_create_ad_status") is not None else "",
        ),
        "日预算": daily_budget_yuan(row.get("daily_budget")),
        "共享钱包余额": to_number_or_text(row.get("wallet_balance")),
        "曝光次数": to_number_or_text(nested_value(row, "view_count")),
        "点击次数": to_number_or_text(nested_value(row, "valid_click_count")),
        "花费": to_number_or_text(nested_value(row, "cost")),
        "目标转化量": to_number_or_text(nested_value(row, "conversions_count")),
        "APP激活次数": to_number_or_text(nested_value(row, "activated_count")),
        "APP激活成本": to_number_or_text(nested_value(row, "activated_cost")),
        "次日留存次数": to_number_or_text(nested_value(row, "retention_count")),
        "RTA": str(nested_value(row, "is_rta") or ""),
        "账户健康度": str(row.get("account_health_level") or ""),
    }
    for field in dynamic_fields:
        header = DYNAMIC_FIELD_HEADERS.get(field)
        if not header or header in record:
            continue
        record[header] = to_number_or_text(nested_value(row, field))
    return record


def build_records(rows: list[dict[str, Any]], status: dict[str, Any], args: argparse.Namespace) -> tuple[list[str], list[dict[str, Any]]]:
    dynamic_fields = [str(item) for item in status.get("dynamicFields") or []]
    headers = BASE_HEADERS[:]
    for field in dynamic_fields:
        header = DYNAMIC_FIELD_HEADERS.get(field)
        if header and header not in headers:
            headers.append(header)
    records = [normalize_record(row, args.date, dynamic_fields) for row in rows]
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        key = tuple(normalize_key_value(header, record.get(header, "")) for header in SYNC_KEY_HEADERS)
        if all(key):
            deduped[key] = record
    return headers, list(deduped.values())


def normalize_key_value(header: str, value: Any) -> str:
    if header in DATE_HEADERS:
        parsed = parse_datetime_text(value) or spreadsheet_serial_to_datetime(value)
        if parsed:
            return parsed.strftime("%Y-%m-%d")
    return str(value or "").strip()


def coerce_feishu_value(header: str, value: Any) -> Any:
    if value is None:
        return ""
    if header in DATE_HEADERS:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        parsed = parse_datetime_text(value)
        if parsed:
            return datetime_to_spreadsheet_serial(parsed)
        return value
    if header in TEXT_HEADERS:
        return str(value or "")
    return to_number_or_text(value)


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


def compact_update_ranges(sheet_id: str, max_col: int, row_values: list[tuple[int, list[Any]]]) -> list[dict[str, Any]]:
    if not row_values:
        return []
    ranges: list[dict[str, Any]] = []
    sorted_rows = sorted(row_values, key=lambda item: item[0])
    block_start = sorted_rows[0][0]
    block_rows = [sorted_rows[0][1]]
    previous_row = block_start
    for row_number, values in sorted_rows[1:]:
        if row_number == previous_row + 1:
            block_rows.append(values)
        else:
            ranges.append(
                {
                    "range": f"{sheet_id}!A{block_start}:{column_letter(max_col)}{previous_row}",
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


def apply_date_style(client: FeishuSheetClient, spreadsheet_token: str, sheet_id: str, headers: list[str], max_row: int) -> None:
    if max_row < 2 or "日期" not in headers:
        return
    col = column_letter(headers.index("日期") + 1)
    try:
        client.set_cell_style(
            spreadsheet_token,
            f"{sheet_id}!{col}2:{col}{max_row}",
            {"formatter": "yyyy/MM/dd"},
        )
    except ChromeAutomationError as exc:
        print(f"WARNING: date style update failed: {exc}")


def sync_records_to_feishu(headers: list[str], records: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, int]:
    client = FeishuSheetClient(args)
    spreadsheet_token, sheet_id = client.resolve_spreadsheet(args.feishu_url)
    sheet_info = client.get_sheet_info(spreadsheet_token, sheet_id)
    row_count = get_grid_count(sheet_info, "row_count", 200)
    col_count = get_grid_count(sheet_info, "column_count", 26)
    existing_values = trim_trailing_empty_rows(client.read_values(spreadsheet_token, sheet_id, col_count, row_count))
    existing_headers = [header for header in normalize_existing_headers(existing_values) if header]
    final_headers = existing_headers[:] if existing_headers else []
    missing_headers = [header for header in headers if header not in final_headers]
    final_headers.extend(missing_headers)
    for key_header in SYNC_KEY_HEADERS:
        if key_header not in final_headers:
            final_headers.append(key_header)

    max_col = len(final_headers)
    existing_index = build_existing_index(existing_values, final_headers)
    updates: list[tuple[int, list[Any]]] = []
    appends: list[list[Any]] = []
    append_start_row = max(len(existing_values) + 1, 2)
    for record in records:
        key = tuple(normalize_key_value(header, record.get(header, "")) for header in SYNC_KEY_HEADERS)
        values = [coerce_feishu_value(header, record.get(header, "")) for header in final_headers]
        if key in existing_index:
            updates.append((existing_index[key], values))
        else:
            appends.append(values)

    summary = {
        "source_rows": len(records),
        "missing_headers": len(missing_headers),
        "updates": len(updates),
        "appends": len(appends),
    }
    print("Feishu sync plan:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Feishu target: spreadsheet={spreadsheet_token} sheet={sheet_id}")
    if args.sync_dry_run or args.dry_run:
        print("Sync dry run finished. No Feishu changes were made.")
        return summary

    if max_col > col_count:
        try:
            client.insert_dimension(spreadsheet_token, sheet_id, "COLUMNS", col_count, max_col)
        except ChromeAutomationError as exc:
            print(f"WARNING: column insert failed, will try direct wide-range write: {exc}")

    value_ranges = [{"range": f"{sheet_id}!A1:{column_letter(max_col)}1", "values": [final_headers]}]
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
    apply_date_style(client, spreadsheet_token, sheet_id, final_headers, final_data_row)
    print("Feishu sync finished.")
    return summary


def save_json(path: str, rows: list[dict[str, Any]], status: dict[str, Any]) -> None:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"status": status, "rows": rows}, ensure_ascii=False, indent=2))
    print(f"Saved raw Tencent data: {output}")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Fetch Tencent Ads account data from Chrome and sync it to Feishu."
    )
    parser.add_argument("--url", default=DEFAULT_TARGET_URL, help="target Chrome tab URL substring")
    parser.add_argument("--date", default=dt.date.today().isoformat(), help="report date in YYYY-MM-DD")
    parser.add_argument("--page-size", type=positive_int, default=50, help="Tencent API page size")
    parser.add_argument("--max-pages", type=positive_int, default=200, help="safety cap for pagination")
    parser.add_argument("--timeout", type=positive_float, default=90.0, help="page/API wait timeout")
    parser.add_argument("--foreground", action="store_true", help="activate Chrome before fetching")
    parser.add_argument("--login-retries", type=int, default=1, help="manual-login retry count")
    parser.add_argument("--no-login-prompt", action="store_true", help="fail instead of prompting for login")
    parser.add_argument("--output-json", help="optional path to save raw API rows")
    parser.add_argument("--dry-run", action="store_true", help="fetch and build Feishu plan without writing")
    parser.add_argument("--sync-dry-run", action="store_true", help="build Feishu plan without writing")
    parser.add_argument("--no-sync", action="store_true", help="only fetch Tencent data; do not sync Feishu")
    parser.add_argument(
        "--feishu-url",
        default=os.environ.get("FEISHU_TENCENT_URL", DEFAULT_FEISHU_URL),
        help="target Feishu wiki/sheet URL; defaults to FEISHU_TENCENT_URL",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        dt.datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError as exc:
        raise ChromeAutomationError("--date must be in YYYY-MM-DD format") from exc
    if args.login_retries < 0:
        raise ChromeAutomationError("--login-retries must be zero or greater")

    rows, status = fetch_tencent_rows(args)
    print(
        "Tencent fetch finished: "
        f"{len(rows)} rows, pages {status.get('fetchedPages')}/{status.get('totalPages')}, "
        f"fields {status.get('columnConfigName') or status.get('columnConfigId')}"
    )
    if args.output_json:
        save_json(args.output_json, rows, status)

    headers, records = build_records(rows, status, args)
    print(f"Prepared {len(records)} deduped Feishu rows with key 日期 + 账户ID.")
    if records:
        sample = {key: records[0].get(key, "") for key in headers[:10]}
        print("Sample row:")
        print(json.dumps(sample, ensure_ascii=False, indent=2))

    if args.no_sync:
        print("No-sync mode finished. Feishu was not changed.")
        return 0
    sync_records_to_feishu(headers, records, args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ChromeAutomationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "Tip: in Chrome, enable View -> Developer -> Allow JavaScript from Apple Events.",
            file=sys.stderr,
        )
        raise SystemExit(1)
