# CJ + Amazon 商品快照

当前支持 CJ 与 Amazon 美国站联合检索、平台筛选、收藏、按规格加入比较及跳转 Amazon 原页面。跨平台条目属于同类候选，尚未建立确认同款的映射，不据此宣称“同款最低价”。新增的 eBay 美国站平台（同等待遇：独立快照、独立向量集合、中文展示、平台筛选）见 `docs/ebay-catalog.md`。

## 导入与运行

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts\import_amazon_catalog.py --input D:\sd_muwfzxms2jl3b7wlwd.json --output data\amazon_catalog.sqlite3
```

导入器默认整批替换快照。后续新增批次要加 `--merge`，它会先读出快照里已有的原始记录再并入本次文件，避免丢掉此前批次。大批量文件如果混入缺少 ASIN 或标题的记录，再加 `--skip-invalid` 跳过这些记录（`skipped_invalid` 是跳过条数）；不加则整批校验失败、快照保持不变：

```powershell
.\.venv\Scripts\python.exe scripts\import_amazon_catalog.py --input D:\sd_muwio0wr9ko37d41h.json --output data\amazon_catalog.sqlite3 --merge
```

如需中文展示，导入后运行 `.\.venv\Scripts\python.exe scripts\localize_amazon_catalog.py`。中文标题、摘要、规格与优惠条件存入独立的 `data/amazon_localization.sqlite3`，支持中断后继续和重新导入后的增量更新。个别条目若反复未通过事实检查（数字/型号被改写、缺少中文），会单条重试后跳过、保留英文原文，末尾输出 `skipped=N`，不会让整批失败。页面、收藏及 Agent 检索使用中文展示，详情页保留英文原文。价格、ASIN、采集时间和库存状态不由翻译修改；直接检索不调用翻译服务或 rerank。Docker Amazon 叠加配置需要先生成这个文件。

在本地 `.env` 配置并重启后端：

```ini
CATALOG_SOURCE=cj
AMAZON_CATALOG_PATH=./data/amazon_catalog.sqlite3
```

留空 `AMAZON_CATALOG_PATH` 即恢复 CJ 单平台。导入器只写独立 Amazon SQLite 文件，不修改来源 JSON、CJ 快照或 CJ 向量集合。同一批重复 ASIN 保留最新采集记录；整批验证通过后事务替换 Amazon 快照，重复导入不会增加商品数（结果中的 `reused_existing` 是本次保留的已有商品数）。原始 JSON 也保存在数据库中，便于后续核对型号和条码，`--merge` 正是从这份原始记录合并新批次。页面和检索读取本地数据，不实时访问 Amazon。

Docker 使用三份配置：基础 `docker-compose.yaml`、`docker-compose.cj.yaml`、`docker-compose.amazon.yaml`；Amazon 文件需先导入。此叠加配置将 Amazon 文件以只读方式挂载到应用。

## 数据边界

已进入快照的数据分三批导入：首批 143 个 ASIN（狗玩具、家居装饰、灯泡等），第二批 40 个 ASIN（牙线、漱口水、湿巾等口腔与婴儿护理），第三批 2162 个有效 ASIN（服饰鞋包、健康家居、家居厨房、电子、宠物、美妆个护为主，另有食品杂货、玩具、办公、收藏品、乐器、软件、汽车用品、游戏、手机配件、图书、影视等），合计 **2320 个不同 ASIN、2201 个有商品报价**；第三批原始文件另有 109 条缺少 ASIN 或标题的记录，导入时以 `--skip-invalid` 跳过，未入库。采集时间均为 2026-10-06，报价配送邮编在美国。品类分布为未分类 475、服饰鞋包 174、家居厨房 156、健康家居 156、电子产品 135、宠物用品 118、美妆个护 104、工具与家装 98、食品与杂货 88、玩具与游戏 88、办公用品 76、庭院园艺 61、母婴用品 59、艺术手工与缝纫 58、收藏品与艺术品 58、乐器 57、软件 52、汽车用品 48、电子游戏 47、手机与配件 44、Kindle 电子书 33、图书 27、运动与户外 24、影视 24、音乐唱片 21、工业与科研 15、家电 14，其余小家电配件、厨房与餐桌、鞋履珠宝手表配件、食品储藏各 1–6 件（未分类是原始记录未返回品类，不是新类目）。`final_price` 仅为采集时页面商品价，促销、优惠券、Prime 资格及最终结算价格需再核对。配送至中国等跨境目的地的范围、运费、税费、时效尚未取得。

`max_quantity_available` 是购买数量限制，不是库存。`is_available` 只记录采集时页面状态，不能代表实时库存。缺失报价展示“报价待核实”，不显示为免费；未返回价格的变体不补造价格，变体也不展开为新商品。原产地缺失时保持未知，不按站点或仓库所在地推断。

商品编号使用 `amazon:us:<ASIN>`，与 CJ 分开；`canonical_product_id` 目前保留来源编号。后续需要根据 GTIN/UPC、品牌型号、规格件数建立人工核验的同款关系，取得同目的地费用后才能比较最终到手价。

## 检索与重排

`GET /commerce/catalog` 支持 `platform=cj`、`platform=amazon` 或留空联合浏览。直接浏览和商品编号查询不调用 rerank。Agent 搜索时两个平台走同样的两阶段召回：各自在自己的 Qdrant 集合做 dense + 服务端 BM25 的库内 RRF（`globex_products_cj_hybrid_cj_snapshot` 与 `globex_products_cj_hybrid_amazon_snapshot`），再按平台内名次合并；配置精排后仅调用一次跨平台 rerank。

Amazon 索引与其快照一一对应：启动时按检索文本指纹增量同步（2320 条，新增或文本变化才请求 embedding，删除的商品同步移除），索引文本是**原始英文**（`card_json` 里的标题、品牌、部门、要点与描述前 600 字符），中文只是展示层投影，不写入向量库。向量检索不可用（Qdrant 或 embedding 故障）时 Amazon 回退本快照关键词召回并在结果里标记 `degraded_from`，不再返回空结果；商品编号查询始终走精确匹配，不进向量召回。不同来源的原始分数不直接比较。展示至少 2 件且两边均有满足已知价格约束的候选时，会保留各平台排名最高的一件，再按统一排序填满其余位置，防止某个平台的全部候选被挤出展示。分类筛选沿用页面现有分类名，Amazon 部门名经映射归入（如 Health & Household、Beauty & Personal Care 归「Health, Beauty & Hair」，Tools & Home Improvement 归「Home, Garden & Furniture」）；未映射的部门（如 Arts, Crafts & Sewing、Clothing, Shoes & Jewelry）只在不限分类时出现。精排失败保留合并顺序；某个平台检索故障会返回 `partial_results` 与 `source_status`，不把故障伪装成没有商品。

决策报告保留报价来源和未核实条件，不用样例关税/物流规则生成 Amazon 到手价。Amazon 不调用 CJ 详情或运费接口，也不生成站内订单。
