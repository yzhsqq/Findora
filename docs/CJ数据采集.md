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

采集程序可继续向同一个 SQLite 快照写入新详情；网页下一次请求即可读到最新已提交记录。列表价是 USD 参考价或区间；未查到详情的商品没有可售 SKU，库存快照也不等于实时可售。CJ 商品当前只支持浏览、收藏、比较和咨询，不支持站内下单意向或计算到手价。

查看采集进度：

```powershell
python -c "import sqlite3; c=sqlite3.connect('data/cj_catalog.sqlite3'); print('list',c.execute('select count(*) from products').fetchone()[0],'detail',c.execute('select count(*) from products where detail_json is not null').fetchone()[0],'stock',c.execute('select count(*) from products where inventory_json is not null').fetchone()[0]); print('points',c.execute('select at,points_used_today,points_remaining from run_log order by id desc limit 1').fetchone())"
```
