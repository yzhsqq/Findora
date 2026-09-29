# CJdropshipping 商品采集

使用官方[商品同步流程](https://developers.cjdropshipping.com/en/api/start/Products-Synchronization-Processing.html)：获取 Access Token、读取品类、调用 Product List V2、按需补商品详情。仓库原有 `data/catalog-v1.jsonl` 是冻结的模拟评测集；CJ 原始快照单独存放。

## 运行

本地 `.env` 中设置 `CJdropshipping_key`，然后在项目根目录运行：

```powershell
python scripts/sync_cj_catalog.py --phase list --target-list 10000
python scripts/sync_cj_catalog.py --phase detail --max-points 45000
python scripts/sync_cj_catalog.py --phase stock --max-stock 500 --max-points 50000
```

命令可重复运行：SQLite 会跳过已有详情或库存快照。若当天基础点数达到上限，任务暂停，次日再次执行即可。也可用 `--phase all` 连续执行三个阶段；默认给 500 件商品的库存请求预留 5,000 点。`--max-details N` 可控制详情总量，`--max-points N` 可设更低的当日总点数上限。输出位于被 Git 忽略的 `data/cj_catalog.sqlite3` 和 `data/cj-products.jsonl`。

## 额度和边界

- 官方[点数规则](https://developers.cjdropshipping.com/en/api/api2/standard/points.html)：基础 50,000 点/日；`listV2` 每次 50 点，`product/query` 和 `getInventoryByPid` 每次 10 点。程序以接口返回的 `pointsInfo.usedToday` 为准，最多使用 50,000 点，不使用交易形成的额外点数。
- 官方[商品接口](https://developers.cjdropshipping.com/en/api/api2/api/product.html)：`listV2` 每页最多 100 件，单一查询结果最多 6,000 件。程序按 CJ 第三级品类分区，均匀取相关大类，避免只采一个热门类别。
- 官方[频率限制](https://developers.cjdropshipping.com/en/api/api2/standard/limit.html)：免费账号通常不超过 1 请求/秒。列表请求串行发送；详情请求起始间隔至少 2 秒、最多 2 个在途请求，遇到限流会退避重试。
- `cj-products.jsonl` 每行包括来源、CJ 商品 ID、品类、采集时间、原始列表记录，以及可选的原始详情和库存记录。密钥和 Access Token 不写入数据或日志。
- `warehouseInventoryNum` 表示 CJ 仓库库存，不能据此断言某个目的国可配送或成交时仍有货；`sellPrice` 是 CJ 的美元商品报价，**不是含运费与税的到手价**。实际下单前要重新查询库存和物流报价。
- 采集是一次快照，不代表实时同步。后续接入 Agent 时，应将 CJ 视为单一供应商，保留来源与采集时间，建立状态更新与语义更新的分离流程，再做向量增量索引。

## 网页与 Agent 使用快照

在 `.env` 增加 `CATALOG_SOURCE=cj` 并重启 API。网页会显示「CJ 商品库」入口，商品卡和 Agent 检索均从 `data/cj_catalog.sqlite3` 读取；原有冻结模拟目录不会混入当前检索。可以用 `GET /commerce/catalog?page=1&page_size=24&query=backpack` 验证。

CJ 模式当前使用 SQLite 标题关键词检索，**不会在服务启动或每次搜索时对 1 万件商品向量化**。这是为了先让真实数据源可浏览，并避免把未核实的物流与库存当作交易事实；后续若要接入向量检索，应在语义字段变化时做增量索引。

采集程序可继续向同一个 SQLite 快照写入新详情；网页下一次请求即可读到最新已提交记录。列表价是 USD 参考价或区间；未查到详情的商品没有可售 SKU，库存快照也不等于实时可售。CJ 商品当前只支持浏览、收藏、比较、咨询和按需物流试算，不支持站内下单意向。

## 小范围详情与物流试算验收

这部分仅用于指定商品的人工验收。普通浏览和 Agent 关键词检索只读本地快照，不向 CJ 发起计点请求。打开 CJ 商品详情后，可按需获取缺失的规格详情，再选定 SKU 和目的国点击「查询 CJ 物流试算」。Agent 在用户明确询问指定商品的配送或费用时也可调用同一个服务。

服务通过 CJ `product/query` 获取规格及其商品价，通过 `getInventoryByPid` 核验该规格可用发货仓，再按发货仓、目的国和 variant ID 调 `freightCalculate`。不同 SKU 和目的国要分别试算。详情和库存最多复用 10 分钟，同一 SKU、目的国、数量的报价最多复用 5 分钟；缓存命中不会再次调用计点接口。服务按约 1.2 秒间隔串行请求，并在单进程试运行中设置本地 300 点/UTC 日上限。多进程部署需要改用共享限流器和共享额度账本。

为保留原有提示词注册表和会话数据，先将 `data/cj_catalog.sqlite3` **复制**到 `data/cj_pilot/cj_catalog.sqlite3`，再运行 `python scripts/run_cj_pilot.py`。试运行的商品补查、报价缓存、点数账本、提示词注册表和会话均写在 `data/cj_pilot/`。试运行脚本监听本机 8000 端口；前端仍按原方式启动。原始快照不会被试运行写入。

验收例子：商品 ID `2507170748351600700`、SKU `CJYD243282601AZ`、目的国 `CN`、数量 1。2026-09-29 的 CJ 返回该规格商品价 US$3.02、YTO China Domestic 运杂费 US$1.43、试算合计 US$4.45。这个值是查询当时的 CJ 试算，不是最终支付价；目的地实际税费、支付汇率和结算页费用仍须另行核实。供应商、品牌、生产国是不同字段：CJ 未提供品牌或生产国时显示未知，不能用供应商或发货仓代替。

相关接口：[CJ 商品详情与库存](https://developers.cjdropshipping.com/en/api/api2/api/product.html)、[CJ 物流试算](https://developers.cjdropshipping.com/en/api/api2/api/logistic.html)、[官方点数规则](https://developers.cjdropshipping.com/en/api/api2/standard/points.html)。

查看采集进度：

```powershell
python -c "import sqlite3; c=sqlite3.connect('data/cj_catalog.sqlite3'); print('list',c.execute('select count(*) from products').fetchone()[0],'detail',c.execute('select count(*) from products where detail_json is not null').fetchone()[0],'stock',c.execute('select count(*) from products where inventory_json is not null').fetchone()[0]); print('points',c.execute('select at,points_used_today,points_remaining from run_log order by id desc limit 1').fetchone())"
```
