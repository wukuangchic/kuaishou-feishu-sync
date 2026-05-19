# 广告数据同步脚本

这个仓库包含两个示例程序：

```text
kuaishou_realtime_export.py
Tencent_web_data/tencent_web_data.py
```

`.env.example` 给出了需要的环境变量模板，复制后按需填写本机配置。

腾讯程序说明见：

```text
Tencent_web_data/README.md
```

v1 已留档在：

```text
archive/v1-post-download/
```

## 首次使用

第一次运行前，按你的运行平台准备登录态：

- macOS 上，先在 Chrome 里手动登录这两个站点，脚本会复用当前登录态
  - 快手趋势分析页：`https://ugagent-partner.kuaishou.com/data/center/analyse/agent`
  - 腾讯广告账户页：`https://ad.qq.com/cm/account`
- Linux / Windows 上，快手可用直连 Cookie 模式，腾讯也可以用直连 Cookie 模式；这时不需要控制 Chrome，但需要提供有效 Cookie

如果使用 Chrome tab 模式，登录失效后再回到对应页面重新登录即可。

如果你是在 OpenClaw 或其他远端环境里运行，下载目录必须是那个环境里 Chrome 真正能写入的本地路径，不是你这台 Mac 的 `~/Downloads`。

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

## v2：同步到飞书表格

目标飞书表格通过 `FEISHU_KS_URL` 配置，不在仓库中写死。

同步逻辑：

- 读取导出文件表头，并把快手导出的 `时间` 拆成飞书列 `日期` 和 `小时`，`渠道` 映射为 `渠道号`
- 飞书表头缺少导出文件中的列时，自动在表尾新增列
- 用 `日期 + 小时 + 渠道号` 做唯一键
- 飞书已有相同唯一键时，覆盖该行数据
- 飞书没有相同唯一键时，追加到表格尾部
- `渠道号`、`产品` 保持文本；`日期` 写入为日期序列数字，`小时` 写入 0-23 的数字；其他纯数字指标列按数字写入飞书

飞书应用凭证请使用环境变量，不要写进代码或 README。复制 `.env.example` 为 `.env` 后再填写：

```bash
export FEISHU_APP_ID='你的飞书应用 App ID'
export FEISHU_APP_SECRET='你的飞书应用 App Secret'
export FEISHU_KS_URL='快手数据目标飞书表格链接'
export FEISHU_IMAGE_URL='需要截图输出的飞书表格链接'
```

先用本地已下载文件做同步预演：

```bash
./kuaishou_realtime_export.py \
  --sync-file "./data/实时0518_hour.csv" \
  --sync-dry-run
```

确认后真正写入飞书：

```bash
./kuaishou_realtime_export.py \
  --sync-file "./data/实时0518_hour.csv"
```

下载后自动同步飞书：

```bash
./kuaishou_realtime_export.py --post --sync-feishu
```

把 `FEISHU_IMAGE_URL` 指向的工作表有效数据区域输出为 PNG：

```bash
./kuaishou_realtime_export.py --feishu-image
```

脚本会自动扫描非空单元格，得到类似 `A1:F8` 的范围后截图输出；如需指定文件名：

```bash
./kuaishou_realtime_export.py --feishu-image --feishu-image-output ./feishu_image.png
```

当前验证结果：

- 本地导出文件可解析；虽然扩展名是 `.csv`，实际是 xlsx 结构
- 读取到 636 条有效数据
- 飞书 dry-run 已通过，可解析 wiki 链接到 spreadsheet token 和 sheet `a0545c`
- 首次实际同步成功：补齐表头后追加 636 行
- 二次 dry-run 验证成功：更新 636 行、追加 0 行，说明 `日期 + 小时 + 渠道号` 去重覆盖逻辑生效
- 数字格式验证成功：飞书原始 API 返回指标列为数字类型，包含 `0`
- 日期数字格式验证成功：飞书原始 API 返回 `日期` 为日期序列值，`小时` 为 0-23 的数字

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
- `--download-dir PATH`：下载目录；默认使用 Chrome 当前机器上的配置目录
- `--filename NAME`：指定下载文件名
- `--sync-feishu`：下载完成后同步到飞书
- `--sync-file PATH`：跳过下载，直接把本地导出文件同步到飞书
- `--sync-dry-run`：只生成同步计划，不写入飞书
- `--feishu-url URL`：目标飞书表格链接，默认读取 `FEISHU_KS_URL`；兼容旧变量 `FEISHU_URL`
- `--feishu-image`：读取 `FEISHU_IMAGE_URL`，把有效数据区域输出为 PNG
- `--feishu-image-output PATH`：指定 `--feishu-image` 的 PNG 输出路径
- `--feishu-ca-file PATH`：指定 Feishu HTTPS CA bundle
- `--feishu-insecure`：关闭 Feishu HTTPS 证书校验，仅用于本地证书排查
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
- 飞书凭证只从环境变量或命令参数读取，不会写入项目文件。
- 飞书应用需要具备表格读写权限，并且目标文档需要允许该应用访问。
- 脚本默认会尝试使用本机 `certifi` CA bundle 访问 Feishu OpenAPI。
- 如果平台接口字段变化，需要同步更新 payload 构造逻辑。
- 脚本会尝试读取 Chrome 配置中的默认下载目录；如果你在 OpenClaw 或其他远端环境里运行，请把 `--download-dir` 显式指向那个环境里可写的路径，不要写你本机 Mac 的 `~/Downloads`。

## 后续开发方向

- 增加配置文件，保存常用筛选条件。
- 增加定时运行入口。
- 增加下载文件自动重命名和归档。
- 增加数据文件完整性校验。
- 在确认接口稳定后，抽出请求层，支持更多报表类型。
