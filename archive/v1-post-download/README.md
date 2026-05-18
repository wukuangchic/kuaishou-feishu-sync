# 快手趋势分析实时表格导出脚本

当前基线版本脚本：`kuaishou_realtime_export.py`

这个脚本用于导出快手信息流代理平台「趋势分析」页面的实时表格数据。当前推荐模式是让已登录的 Chrome 页面在后台发起同源 POST 下载，脚本不读取 Chrome Cookie 明文；如果登录态失效，再把目标页打开到前台，由使用者手动登录后重试。

## 当前推荐用法

```bash
cd <repo-root>
./kuaishou_realtime_export.py --post
```

推荐模式行为：

- 自动查找或创建目标 Chrome tab：`https://ugagent-partner.kuaishou.com/data/center/analyse/agent`
- 在该 tab 的登录上下文里请求下载接口
- 默认导出当天「实时 / 按小时」数据
- 正常情况下不主动切到 Chrome 前台
- 如果登录态失效，会打开目标页到前台，等待使用者登录后按回车，再自动重试

## 运行前准备

Chrome 需要开启：

```text
查看 -> 开发者 -> 允许 Apple 事件中的 JavaScript
```

脚本无第三方 Python 依赖，使用系统自带 Python 标准库即可。

## 常用命令

只查看将要发送的 POST 参数，不下载：

```bash
./kuaishou_realtime_export.py --post --dry-run
```

导出「实时 / 汇总」：

```bash
./kuaishou_realtime_export.py --post --detail all
```

指定日期：

```bash
./kuaishou_realtime_export.py --post --date 2026-05-18
```

增加筛选条件：

```bash
./kuaishou_realtime_export.py --post --channel 渠道号 --subchannel 子渠道 --adid 123
```

如果希望一开始就把 Chrome tab 拉到前台：

```bash
./kuaishou_realtime_export.py --post --foreground
```

## 模式说明

### `--post`

推荐模式。脚本通过 AppleScript 在 Chrome tab 内执行同源请求，让浏览器自动携带登录态。脚本不会读取 Cookie 明文。

下载接口：

```text
POST /rest/n/agent/portalReport/downloadExcel
```

默认 payload：

```json
{
  "startTime": "2026-05-18",
  "endTime": "2026-05-18",
  "realTimeDetailAggr": 0,
  "timeType": 1,
  "quotaIdList": [11, 12, 13, 16],
  "dataType": 3
}
```

### `--direct-post`

纯 Python 直连模式，不打开也不控制 Chrome。这个模式需要手动提供 Cookie：

```bash
export KUAISHOU_COOKIE='从浏览器复制出来的 Cookie 字符串'
./kuaishou_realtime_export.py --direct-post
```

也可以使用文件：

```bash
./kuaishou_realtime_export.py --direct-post --cookie-file kuaishou_cookie.txt
```

这个模式适合后续做服务器定时任务，但 Cookie 会过期，需要额外维护登录态。

### 默认点击模式

不加 `--post` 或 `--direct-post` 时，脚本会按 UI 流程操作：

1. 刷新页面
2. 点击「实时」
3. 点击「查询」
4. 等待至少 2 秒
5. 点击「下载表格」

这个模式更接近人工操作，但也最依赖页面结构。

## 参数速查

- `--date YYYY-MM-DD`：导出日期，默认当天
- `--detail hour|all`：`hour` 为按小时，`all` 为汇总
- `--download-dir PATH`：下载目录，默认 `~/Downloads`
- `--filename NAME`：指定下载文件名
- `--product-id ID`：产品筛选，可重复
- `--media NAME`：媒体筛选，可重复
- `--channel VALUE`：渠道号筛选，可重复
- `--subchannel VALUE`：子渠道筛选，可重复
- `--adid VALUE`：adid 筛选，可重复
- `--login-retries N`：登录失效后提示手动登录并重试的次数，默认 `1`
- `--no-login-prompt`：登录失效时直接失败，不打开 Chrome 等待登录

## 已知约束

- `--post` 依赖 Chrome 的 AppleScript JavaScript 权限。
- 脚本不会自动提取 Chrome Cookie 明文。
- 如果平台接口字段变化，需要同步更新 payload 构造逻辑。
- 如果 Chrome 下载目录不是 `~/Downloads`，请传入 `--download-dir`。

## 后续开发方向

- 增加配置文件，保存常用筛选条件。
- 增加定时运行入口。
- 增加下载文件自动重命名和归档。
- 增加数据文件完整性校验。
- 在确认接口稳定后，抽出请求层，支持更多报表类型。
