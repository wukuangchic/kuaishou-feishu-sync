# Tencent_web_data

从已登录的 Chrome 腾讯广告账户页拉取当天分页账户数据，并同步到飞书表格。

首次使用时，请先在 Chrome 里手动登录腾讯广告账户页 `https://ad.qq.com/cm/account`。

目标飞书表格通过 `.env` 中的 `FEISHU_TENCENT_URL` 配置，仓库不写死具体链接。

## 运行

先确保 Chrome 已打开腾讯广告账户页，并已登录。Chrome 需要开启：

```text
查看 -> 开发者 -> 允许 Apple 事件中的 JavaScript
```

执行：

```bash
cd <repo-root>
./Tencent_web_data/tencent_web_data.py
```

脚本默认行为：

- 查找 URL 以 `https://ad.qq.com/cm/account` 开头的 Chrome tab
- 在该 tab 中用浏览器登录态 POST 获取账户分页数据
- 按脚本内置字段配置获取腾讯账户数据
- 拉取全部分页
- 同步到 `FEISHU_TENCENT_URL`

同步唯一键为：

```text
日期 + 账户ID
```

飞书中已有相同唯一键时覆盖该行；没有时追加到表尾。缺少表头会自动新增列。

## 配置

`.env` 中建议包含，或直接复制根目录的 `.env.example`：

```bash
FEISHU_TENCENT_URL='腾讯数据目标飞书表格链接'
FEISHU_APP_ID='飞书应用 App ID'
FEISHU_APP_SECRET='飞书应用 App Secret'
```

`.env` 已被 `.gitignore` 忽略，不会提交到 GitHub。

## 常用命令

只拉取并生成同步计划，不写飞书：

```bash
./Tencent_web_data/tencent_web_data.py --dry-run
```

只拉取数据，不同步飞书：

```bash
./Tencent_web_data/tencent_web_data.py --no-sync
```

指定日期：

```bash
./Tencent_web_data/tencent_web_data.py --date 2026-05-18
```

保存腾讯接口原始数据：

```bash
./Tencent_web_data/tencent_web_data.py --output-json ./tencent_raw.json
```

## 已知约束

- 依赖 Chrome 当前登录态，不读取或导出 Cookie 明文。
- 登录态失效时，脚本会打开对应 tab，等待使用者手动登录后重试。
- 默认按 `日期 + 账户ID` 去重覆盖。
- `日期` 写入飞书为表格日期序列数字，并设置日期格式；账户 ID 作为文本写入。
- 如果腾讯接口字段或页面自定义列结构变化，需要更新字段映射。
