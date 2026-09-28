# V2 购买决策单离线契约评测

`decision_cases.jsonl` 是五个固定场景，引用 `data/catalog-v1.jsonl` 的真实商品 ID：商品价预算、目的地与材质排除、到手价预算及缺报价、零命中、五件候选上限。`source_hit_ids` 表示送入决策服务的检索顺序；`quote_by_product` 是该场景固定的**规则估算报价输入**，不是实时运费或政策数据。

评测器只读取已经生成的 DecisionReport JSONL，不执行检索或模型调用。每行形式如下：

```json
{"case_id":"DWB-001","report":{"version":2,"request":{},"candidates":[]}}
```

运行：

```powershell
.venv\Scripts\python.exe -m scripts.eval.decision_quality --reports path\to\reports.jsonl
```

标准输出是逐案例 findings 和汇总。全部通过时退出码为 `0`，有违规时为 `1`，输入文件无效时为 `2`。检查范围包括候选数与顺序、库存/品类/配送/材质/预算硬条件、商品价与到手价口径、目录字段一致性、报价来源与算术、字段级证据引用，以及明显的实时数据或官方政策伪声明。

这是**决策单契约测试**，不能替代召回率、推荐相关性或线上任务成功率评测；它也不能从自然语言自动证明每条解释的语义正确。现有正式评测的结论不因这五个场景通过而改变。
