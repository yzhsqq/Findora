# CJ + Amazon + eBay 商品快照

当前支持 CJ、Amazon 美国站与 eBay 美国站联合检索、平台筛选、收藏、按规格加入比较及跳转各平台原页面。跨平台条目属于同类候选，尚未建立确认同款的映射，不据此宣称“同款最低价”。

## 导入与运行

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts\import_ebay_catalog.py --input D:\download\products_filtered.json --output data\ebay_catalog.sqlite3
```

导入器默认整批替换快照。后续新增批次要加 `--merge`，它会先读出快照里已有的原始记录再并入本次文件，避免丢掉此前批次。大批量文件如果混入缺少商品编号或标题的记录，再加 `--skip-invalid` 跳过这些记录（`skipped_invalid` 是跳过条数）；不加则整批校验失败、快照保持不变。

eBay 采集文件通常**不带逐条采集时间**，导入器以文件修改时间（UTC）作为整批的 `updated_at`；记录里若有带时区的 `timestamp` 则优先使用。

如需中文展示，导入后运行 `.\.venv\Scripts\python.exe scripts\localize_ebay_catalog.py`，用法与 Amazon 完全一致（同一套可续跑、可校验的流水线，见 `scripts/localization_pipeline.py`）。中文标题、摘要、规格与报价条件存入独立的 `data/ebay_localization.sqlite3`，个别条目反复未通过事实检查会保留英文原文并在末尾输出 `skipped=N`。

在本地 `.env` 配置并重启后端：

```ini
CATALOG_SOURCE=cj
AMAZON_CATALOG_PATH=./data/amazon_catalog.sqlite3
EBAY_CATALOG_PATH=./data/ebay_catalog.sqlite3
```

两者留空即回到 CJ 单平台，只配其中一个则只联合该平台。导入器只写独立 eBay SQLite 文件，不修改来源 JSON、CJ 快照或 CJ/Amazon 向量集合。同一批重复商品编号保留较新的记录；整批校验通过后事务替换 eBay 快照，重复导入不会增加商品数（结果中的 `reused_existing` 是本次保留的已有商品数）。原始 JSON 也保存在数据库中，`--merge` 正是从这份原始记录合并新批次。页面和检索读取本地数据，不实时访问 eBay。

Docker 可在基础 `docker-compose.yaml`、`docker-compose.cj.yaml`、`docker-compose.amazon.yaml` 之上再叠加 `docker-compose.ebay.yaml`（eBay 文件需先导入，以只读方式挂载）。

## 数据边界

已进入快照的数据来自 `products_filtered.json`：**40 个不同商品编号、40 个有页面报价**，合计 365 个规格（含变体选项）。品类为玩具与爱好 39、收藏品与艺术 1（卡牌、游戏配件、快餐赠品为主）。同批交付的 `sd_muwlfq8f2hdc6fpizv.json` 是 934 条采集失败记录（404 死链，`error_code=dead_page`），不是商品数据，导入器会按无效记录拒绝（需要继续导入同批其他文件时才用 `--skip-invalid`）。

`price` 仅为采集时页面报价，`sale_price` 更低时记为待核实促销；卖家优惠、运费与最终结算价需再核对。页面的 `ships_to`（预估送达）与 `excludes_shipping`（不配送地区）只是页面文案，未解析为可配送国家列表，因此配送至中国等跨境目的地的范围、运费、税费、时效仍未取得。`availability=in_stock` / `is_sold` 只记录采集时页面状态；`quantity_available`、`available_count` 是页面可售数量，**不作为库存暴露**（与 Amazon 一致：`stock_known=false`、规格库存 0）。缺失报价展示“报价待核实”，不显示为免费；未返回价格的变体不补造价格。原产地取自 `item_location` 的末段国家，缺失时保持未知。

商品编号使用 `ebay:us:<商品编号>`，与 CJ（`数字`）、Amazon（`amazon:us:<ASIN>`）分开路由；`canonical_product_id` 目前保留来源编号。后续需要根据 GTIN/MPN、品牌型号、规格件数建立人工核验的同款关系，取得同目的地费用后才能比较最终到手价。

## 检索与重排

`GET /commerce/catalog` 支持 `platform=cj`、`platform=amazon`、`platform=ebay` 或留空联合浏览。直接浏览和商品编号查询不调用 rerank。Agent 搜索时三个平台走同样的两阶段召回：各自在自己的 Qdrant 集合做 dense + 服务端 BM25 的库内 RRF（`globex_products_cj_hybrid_cj_snapshot`、`..._amazon_snapshot`、`..._ebay_snapshot`），再按平台内名次合并；配置精排后仅调用一次跨平台 rerank。

eBay 索引与其快照一一对应：启动时按检索文本指纹增量同步（新增或文本变化才请求 embedding，删除的商品同步移除），索引文本是**原始英文**（`card_json` 里的标题、品牌、类目路径、页面成色、要点与描述前 600 字符），中文只是展示层投影，不写入向量库。向量检索不可用时 eBay 回退本快照关键词召回并在结果里标记 `degraded_from`；商品编号查询始终走精确匹配，不进向量召回。

不同来源的原始分数不直接比较。展示至少 2 件且各平台均有满足已知价格约束的候选时，会保留各平台排名最高的一件，再按统一排序填满其余位置。分类筛选沿用页面现有分类名，eBay 一级类目经映射归入（如 Toys & Hobbies、Dolls & Bears、Baby 归「Toys, Kids & Babies」，Home & Garden、Crafts 归「Home, Garden & Furniture」）；未映射的类目只在不限分类时出现。精排失败保留合并顺序；某个平台检索故障会返回 `partial_results` 与 `source_status`（cj / amazon / ebay），不把故障伪装成没有商品。

决策报告保留报价来源和未核实条件，不用样例关税/物流规则生成 eBay 到手价；eBay 额外展示页面成色。eBay 不调用 CJ 详情或运费接口，也不生成站内订单。

## 中文展示

页面、收藏与 Agent 检索使用中文展示，详情页保留英文原文与页面成色原文。价格、商品编号、采集时间和页面状态不由翻译修改；直接检索不调用翻译服务或 rerank。
