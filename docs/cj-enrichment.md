# CJ 商品补全

商品详情保存在 `data/cj_catalog.sqlite3`。前端详情页展示“前往商品购买页面”，新窗口打开 CJ，不显示地址文本。中文摘要与 CJ 原始详情同时保留。旧的评测快照不会被采集程序修改。

## 启动与进度

商品详情的“加入待购记录”将商品及已选择的规格保存到当前买家下，然后打开“我的订单”。未选择规格或未补齐购买链接的商品也可保存；相同商品与规格重复保存只保留一条。状态为“待购买”，可查看商品、跳转 CJ 购买或移除记录。跳转后状态不自动变为已购买。

记录通过 `/commerce/purchase-records` 维护，独立保存在运行目录的 `purchase_records.sqlite3`，刷新和应用重启后仍保留。保存只读取本地 CJ 商品和已有规格详情，不调用 CJ API、不创建 CJ 订单、不扣库存、不付款，也不写入原有演示订单账本。展示时重新读取当前购买链接；后来补齐链接会出现购买按钮，接口标记不可用或移除的链接会被隐藏。

目前应用采用本机 Python 后端（8000）和 Node/Vite 前端（5173），启动命令为 `.\scripts\start_local.ps1`。运行数据在 `data/native_runtime`，已从旧应用容器备份会话、收藏、偏好及提示词数据库；原容器与数据卷仍保留。向量索引继续使用 `.env` 配置的本机 Qdrant 服务，应用切换不会重建该索引。Python/Node 的日志和进程编号在 `data/local_services`。

在项目根目录运行：

```powershell
# 前台试跑 20 件（上限是本次请求商品数，不是累计详情数）
.\.venv\Scripts\python.exe scripts/enrich_cj_catalog.py --limit 20 --verify-pages

# 隐藏窗口持续运行，等待积分恢复并跨日续采
.\scripts\start_cj_enrichment.ps1
Get-Content data\cj_enrichment\progress.json

# 请求停止：当前网络请求或最长 55 秒等待结束后退出
New-Item data\cj_enrichment\STOP -ItemType File
# 重新运行前移除 STOP
Remove-Item -LiteralPath data\cj_enrichment\STOP
```

后台进程需要电脑保持运行。断电、进程退出后不会自行启动，重新执行启动命令会接着缺失详情采集。一个数据库只允许一个采集进程；账户认证失败、缺少积分计数或连续三个商品接口错误会停止，并在进度文件记录原因。API Key 从本地 `.env` 读取，不进入日志。

每天最多预留 45,000 积分；同时遵守账户 `usedToday` 与当前 `remaining`。详情请求预记 10 分，即使网络超时也不重复当作零消耗。积分不足时等待，UTC 换日后重新读取额度。HTTP 限流会退避。库存与目的地运费按用户操作实时查询，不做全量预采。

## 链接可信度

| 状态 | 来源 | 显示购买按钮 |
| --- | --- | --- |
| `derived` | 标题与 PID 构造的候选地址 | 否 |
| `observed` | CJ 官方商品页索引，SKU/PID 与本地商品对应 | 是 |
| `page_verified` | 实时页面正文包含对应 SKU，并保留相同商品 PID | 是 |

`scripts/cj_observed_product_links.json` 保留已找到的官方页面地址及索引新旧程度；索引证据不代表当天网页可访问或可下单。API 返回正确 PID 只证明详情对应商品，不升级链接状态。遇到 CJ 人机验证后，当前运行停止网页检查，API 详情继续采集；下次启动会再次尝试。不会绕过验证页。记录在 `source_url_page_status`，HTTP 200 的验证页不算通过。

网页检查遇到验证页时，仍可通过搜索引擎继续发现官方商品页。检索 `site:cjdropshipping.com/product "商品 SPU"`，仅接受正式官网结果；商品页 URL 的 PID 和索引中的 SKU/SPU 必须同时匹配本地商品。测试域名、其他商品、缺少 SKU 的结果不入库。已观察地址的证据保存在同一 JSON 文件。

```powershell
# 将搜索结果的精简证据导入：pid、url、spus、crawl_recency、query_evidence
.\.venv\Scripts\python.exe -X utf8 scripts/cj_link_discovery.py --results data/cj_link_discovery/results-01.json
```

导入本身不访问 CJ，不消耗 CJ API 积分。成功导入后自动发布新的只读快照，应用下一次查询即可读取新链接。搜索结果还不能证明当前库存或结算可用；继续保留 `observed` 状态，不标记为实时网页核验通过。

接口只公开已观察或页面核验通过的官方 HTTPS 商品地址，校验域名和 PID，前端再次检查。CJ 接口明确报告不可用的商品不显示购买按钮。进度文件分别报告详情、已观察链接、待核验候选及验证页，不能把候选数当作真实链接完成数。

数据库备份使用 SQLite backup，包含 WAL 中已提交数据。实时详情服务读取补全后的主库详情与链接，避免已有的物流工作副本覆盖更新。新增描述直接作为文字展示，保持未知库存、税费与配送信息不被补写。

发布快照变化时，实时物流副本可以重建，但每日积分调用记录会保留，避免应用重启后把同一天已用的试运行积分清零。

采集同时每两分钟向 `data/cj_published/` 发布带时间戳的独立只读快照（启动、首五件、结束和主动停止时也发布）。Docker CJ 配置挂载整个发布目录，通过 `CJ_CATALOG_PATH` 指定逻辑文件名，应用自动读取最新已完成的 `cj_catalog.snapshot.*.sqlite3`。快照不依赖源库的 WAL/SHM 文件；下一次查询即可读取新版本。保留最近三份，仍被读者占用的旧文件在下次发布时重试清理，超过十份未释放会安全停止。首次使用先运行 `--prepare-only` 生成发布文件，再启动 Compose 服务。
