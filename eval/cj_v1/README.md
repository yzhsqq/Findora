# CJ 商品检索与报价证据基线 v1

这组基线回答两个范围明确的问题：**当前 CJ 快照检索能否找回指定商品**，以及**既有 CJ 报价缓存在展示前能否保留金额与来源边界**。它不是完整 Agent 任务成功率，也不是实时跨境履约证明。

## 输入与隔离

- 检索：`retrieval_cases.jsonl` 共 40 题，29 题 `dev`，11 题 `release`。36 道正例由人工核对快照商品标题并指定一个锚点 ID；4 道无结果题单独计分。`lexical` 题故意含有标题词，用于冒烟；`paraphrase` 题检验中文改写；`exact` 题检验 ID；`empty` 题检验误召回。
- 报价：`quote_cases.jsonl` 共 8 条，取自冻结快照里先前真实 CJ 调用形成的成功报价缓存。回放强制禁止网络调用，检查 SKU、目的国、加法、报价来源与工厂/CJ 仓标记。7 条同国、1 条跨境；没有失败报价缓存，因此不测“无线路”处理率。
- 数据：`data/cj_eval/v1/catalog.sqlite3` 是从本机试运行库用 SQLite backup 冻结的副本，**被 Git 忽略**。`snapshot.json` 记录 SHA-256、10,007 件列表商品、763 件详情、164 件库存、27 条报价缓存。运行器在评测前后检查哈希；不匹配则拒绝生成可比结果。不要用会继续更新的 `data/cj_pilot/` 直接代替。
- 原始 CJ 数据不随此仓库分发。没有这份已冻结的本地文件，其他机器不能复现**同一个数值**；可审阅标注、运行器和报告，但复现数值需要同一数据工件。未来若允许分发数据工件，应单独核对数据使用条款。

## 运行

在项目根目录：

```powershell
.venv\Scripts\python.exe -m scripts.eval.run_cj_baseline --split all
.venv\Scripts\python.exe -m scripts.eval.run_cj_baseline --split dev
.venv\Scripts\python.exe -m scripts.eval.run_cj_baseline --split release
.venv\Scripts\python.exe -m scripts.eval.run_cj_quote_replay --split all
```

结果在 `eval/verification/cj-v1/`。运行器只读本地商品快照；报价回放即使缓存过期也只读取历史记录，并将 CJ 客户端替换为会直接报错的桩。上述命令 **CJ 计点 0、LLM token 0**。本地检索耗时不能代替网页到 Agent 的端到端延迟。

## 首轮结果

| 范围 | 基线结果 |
| --- | ---: |
| 已知商品 Hit@5 | 30/36，83.3% |
| 锚点 MRR | 0.7755 |
| 标题词题 | 24/24 |
| 中文改写题 | 3/8 |
| 精确 ID 题 | 3/4 |
| 无结果题 | 3/4 |
| 既有报价缓存回放 | 8/8 契约检查通过 |

失败主要表现为中文改写无候选或锚点未进入前五、UUID 商品 ID 不能精确查回，以及多个关键词按“任一命中”召回不相关商品。逐题返回与失败类型见 [`baseline-all.md`](../verification/cj-v1/baseline-all.md)。

**指标边界：**每道正例只有一个锚点，其他可能相关的商品尚未穷举，因此这里叫“已知商品 Hit@5”，不叫完整 Recall@5，也不计算 Precision 或 NDCG。测试问题来自项目人员围绕真实商品构造，尚无真实用户查询日志。`release` 是预先划分的固定选集，但本报告已公开其结果，后续调参需要新的未见过的留出题。报价回放只验证缓存读路径与既有数值，没有重新请求 CJ；即使显示 8/8，也不证明当前可发货、税费完整或最终支付价正确。

## 下一轮单变量实验

先保持同一快照和 40 题不变，单独比较 CJ 现有标题 `LIKE` 与更明确的 ID 查询、英文全文检索/排序。中文改写若仍差，再比较低成本的查询改写与语义召回。记录每种策略的命中、误召回、本地延迟及新增模型成本。检索组件证明有效后，再用独立 Agent 用例测答案证据、报价成功率、token、CJ 点数和工具失败率；不能把本轮组件得分冒充 Agent 成绩。

第一项单变量改进已完成：为 CJ 目录增加 UUID 商品 ID 精确查询，与数字 ID 使用同一查询路径。同一 40 题和同一快照上，已知商品 Hit@5 从 **30/36 提升到 31/36**，精确 ID 题从 **3/4 提升到 4/4**，本批次未通过用例从 **7/40 降至 6/40**；只有 `CJ-R035` 的结果发生变化，无退化用例。中文改写仍是 **3/8**，无结果题仍是 **3/4**。这里的未通过率不是在线接口故障率。这是一个有限的准确性改进，不代表整体选品质量已经达标。对比证据见 [`uuid-fix/comparison.md`](../verification/cj-v1/uuid-fix/comparison.md)。复跑命令为：

```powershell
.venv\Scripts\python.exe -m scripts.eval.run_cj_baseline --split all --output-dir eval/verification/cj-v1/uuid-fix
.venv\Scripts\python.exe -m scripts.eval.compare_cj_baseline --before eval/verification/cj-v1/baseline-all.json --after eval/verification/cj-v1/uuid-fix/baseline-all.json --output eval/verification/cj-v1/uuid-fix/comparison.json
```

对比脚本在计算差异前核对快照、题集、选集、题序和金标；任一不一致则拒绝输出“改进”结论。
