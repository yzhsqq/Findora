# CJ 中文需求首次检索：第二次评测

## 结论

提示词候选改善了首次召回，但**未通过预注册的留出集发布门槛，保持未发布**。
留出集 `CJ-V2H003` 的候选检索把目标商品排第 1，最终回答也列出该商品，但漏写 `product_id`；
基线回答写出了该 ID，因此按预先定义的最终锚点 ID 退化判失败。不能在已打开的 10 题上修补后
把它们再次当独立留出集。

## 版本与有效性

- 基线 commit：`30b9a4f7e22cfe42c234ba0dc0449d717e870aac`；候选 commit：`101f319924b20d795a8987da195fb16e4a2925d6`。
- 基线提示词版本：`p-3fcc119e8e48706581eb2321c981c6a07115bd8b12b97c47b7547a887f0ae2bc`；候选提示词版本：`p-08a9e060bafc5e0bf7c417211300ea515d0d8d365efa6ed825ae6926acdf70c4`。两个工作树的代码差异仅 `app/application/prompts/globex.yml`。
- CJ 快照 SHA-256：`f29e0e107143422f80ca6ac2d4c74c88e9b577ec6f9ca3176c05dfd93bd9928d`；已知题/留出题哈希均在 [freeze.json](freeze.json)。
- 两侧为独立进程、数据目录、会话与 prompt registry；固定 `deepseek-v4-flash`，关闭语义缓存、队列与 Redis；成对顺序按固定种子随机。
- 已知集 18/18 对、留出集 10/10 对有效；已知集有 1 次基线详情工具错误，按协议重跑整对后有效；留出集无效尝试为 0。正式集合无网关错误、模型回退或 usage 缺失。
  [工具错误审计](../verification/cj-crosslang-v2/tool_error_audit.json)保留了原报告的哈希和首次失败 trace。
- 上一版提示词 `c357b76` 在已知集仅净增 3/18，且运行器出现一次传输缓冲错误；该探索版报告单独保存在
  [known.c357b76](../verification/cj-crosslang-v2/known.c357b76/report.json)，未接触 v2 留出集。

## 预注册指标

**已知集（8 道原开发题 + 10 道已公开 v1 留出题）：**

| 指标 | 基线 | 候选 | 净增 |
| --- | ---: | ---: | ---: |
| 首次工具 Hit@5 | 5/18 | 16/18 | +11 |
| 任一轮 Hit@5 | 16/18 | 18/18 | +2 |
| 最终答案包含锚点 ID | 16/18 | 18/18 | +2 |

首次 Hit@5 净增 11/18 ≥ 预注册门槛 5/18；三项指标均无逐题退化。原 40 题组件回归
34/40 → 34/40，逐题候选和排名相同。

**v2 封存留出集：**

| 指标 | 基线 | 候选 | 净增 |
| --- | ---: | ---: | ---: |
| 首次工具 Hit@5 | 3/10 | 8/10 | +5 |
| 任一轮 Hit@5 | 9/10 | 10/10 | +1 |
| 最终答案包含锚点 ID | 9/10 | 9/10 | +0 |

首次 Hit@5 净增 5/10 ≥ 门槛 2/10；最终答案 ID 退化 `CJ-V2H003` 违反零退化条件，
即使最终命中总数持平也不算通过。

## 调用与成本

| 集合 | 侧别 | 模型调用 | 输入 token | 输出 token | 合计 token |
| --- | --- | ---: | ---: | ---: | ---: |
| 已知 18 | baseline | 60 | 609868 | 27848 | 637716 |
| 已知 18 | candidate | 46 | 436421 | 19837 | 456258 |
| 留出 10 | baseline | 33 | 359327 | 14394 | 373721 |
| 留出 10 | candidate | 26 | 253127 | 10165 | 263292 |
| 两集合 | baseline | 93 | 969195 | 42242 | 1011437 |
| 两集合 | candidate | 72 | 689548 | 30002 | 719550 |

上表仅列最终有效配对。实际执行还包含 `CJ-H003` 的一次无效配对；以下是**全部尝试**的真实用量：

| 集合 | 侧别 | 尝试次数 | 模型调用 | 输入 token | 输出 token |
| --- | --- | ---: | ---: | ---: | ---: |
| 已知 18 | baseline | 20 | 71 | 726339 | 31411 |
| 已知 18 | candidate | 20 | 56 | 557369 | 24982 |
| 留出 10 | baseline | 10 | 33 | 359327 | 14394 |
| 留出 10 | candidate | 10 | 26 | 253127 | 10165 |

两表的 token 均来自完整 `usage.summary`，没有把工具错误或 usage 缺失记为零。模型网关未给出可确认的价格，
所以不换算美元费用；品类知识库启动时的 embedding 请求不在逐题 usage 中，端到端启动成本尚未完整计量。
已知集 CJ 详情工具调用 3 次、物流试算工具 0 次；
留出集分别 0 / 0 次。两侧 `cj_pilot_calls`
账本仍与冻结快照同为 100 条、1000 点，
因此本轮新增 CJ 点数为 0；这是实测结果，不是验收门槛。

## 失败归因与下一轮

首次仍未命中的 4 题（已知 `CJ-H001`、`CJ-H003`；留出 `CJ-V2H004`、`CJ-V2H007`）
在**保持候选英文 query 不变、只关 category** 的本地快照回放里都将目标排第 1；
首个故障层为 `category`。`CJ-V2H003` 首次排名第 1，答案未写商品 ID，故障层为 `answer`。
这两类问题应分开实验。下一轮可先在已知集测 category 处理；把这 10 道留出题升级为已知集，
另建新的封存留出集后才能再作泛化判断。当前不得发布这版提示词。
旧词典候选 `693963a` 在主工作树现由默认关闭的 `CJ_EXPERIMENTAL_LEXICON` 控制（防护 commit `6a5cf82`）；
默认关闭后原 40 题与 `30b9a4f` 逐题候选完全相同，见
[对照](../verification/cj-crosslang-v2/default-off40/comparison.json)。当前运行中的本机服务未被本次评测重启。

## 逐题证据

逐题首次改写、category、候选 ID、排名、完整 usage 与 Agent trace 见
[已知集 JSON](../verification/cj-crosslang-v2/known/report.json)、
[留出集 JSON](../verification/cj-crosslang-v2/holdout/report.json) 和
[只读归因回放](../verification/cj-crosslang-v2/diagnosis.json)。
下面每题可点开候选侧完整压缩 trace；基线 trace 路径在 JSON 中。

### 已知集

| 用例 | 首次排名 B→C | 首次 query B→C | 任一轮 B→C | 答案 ID B→C | 首个故障层 |
| --- | ---: | --- | --- | --- | --- |
| [CJ-R025](../verification/cj-crosslang-v2/known/traces/CJ-R025.1.candidate.jsonl.gz) | —→1 | 绿色檀木 梳头用 梳子<br>→ green sandalwood hair comb | 1→1 | 1→1 | — |
| [CJ-R026](../verification/cj-crosslang-v2/known/traces/CJ-R026.1.candidate.jsonl.gz) | 1→1 | ORICO G11-H4 笔记本 USB 扩展坞 USB接口<br>→ USB hub ORICO G11-H4 | 1→1 | 1→1 | — |
| [CJ-R027](../verification/cj-crosslang-v2/known/traces/CJ-R027.1.candidate.jsonl.gz) | —→2 | Type-C 转千兆有线网卡 适配器 USB-C 千兆以太网转换器 笔记本电脑<br>→ USB-C to Gigabit Ethernet adapter | 1→1 | 1→1 | — |
| [CJ-R028](../verification/cj-crosslang-v2/known/traces/CJ-R028.1.candidate.jsonl.gz) | —→1 | 防水 双屏 数码相机<br>→ waterproof dual screen digital camera | 0→1 | 0→1 | — |
| [CJ-R029](../verification/cj-crosslang-v2/known/traces/CJ-R029.1.candidate.jsonl.gz) | 2→2 | 宠物狗 夜跑 反光 牵引绳 5英尺 dog reflective leash 5ft<br>→ reflective dog leash 5 ft night running | 1→1 | 1→1 | — |
| [CJ-R030](../verification/cj-crosslang-v2/known/traces/CJ-R030.1.candidate.jsonl.gz) | —→1 | 户外登山 双肩 战术包 背包<br>→ tactical backpack outdoor hiking | 0→1 | 0→1 | — |
| [CJ-R031](../verification/cj-crosslang-v2/known/traces/CJ-R031.1.candidate.jsonl.gz) | 1→1 | 发光猫耳朵耳机 LED 猫耳头戴耳机<br>→ light up cat ear headphones | 1→1 | 1→1 | — |
| [CJ-R032](../verification/cj-crosslang-v2/known/traces/CJ-R032.1.candidate.jsonl.gz) | —→1 | 长方形 铁皮 收纳盒 带拉扣 扣锁<br>→ rectangular metal tin storage box with latch | 1→1 | 1→1 | — |
| [CJ-H001](../verification/cj-crosslang-v2/known/traces/CJ-H001.1.candidate.jsonl.gz) | —→— | 记忆棉 U形护颈枕 飞机 睡眠 便携<br>→ memory foam U-shaped neck pillow travel sleep | 1→1 | 1→1 | category |
| [CJ-H002](../verification/cj-crosslang-v2/known/traces/CJ-H002.1.candidate.jsonl.gz) | —→1 | 宠物梳毛刷 猫狗 喷水 除浮毛 去浮毛<br>→ pet grooming brush spray water deshedding cat dog | 1→1 | 1→1 | — |
| [CJ-H004](../verification/cj-crosslang-v2/known/traces/CJ-H004.1.candidate.jsonl.gz) | —→1 | 狗 宠物雨衣 防水 带帽 披风<br>→ dog raincoat waterproof hooded cape | 1→1 | 1→1 | — |
| [CJ-H005](../verification/cj-crosslang-v2/known/traces/CJ-H005.1.candidate.jsonl.gz) | —→3 | 双头 Type-C 数据线 100W 快充 手机<br>→ USB C to USB C 100W fast charging cable | 1→1 | 1→1 | — |
| [CJ-H006](../verification/cj-crosslang-v2/known/traces/CJ-H006.1.candidate.jsonl.gz) | 1→1 | 有线游戏键盘 RGB背光 手机支架<br>→ wired gaming keyboard RGB backlight phone holder | 1→1 | 1→1 | — |
| [CJ-H007](../verification/cj-crosslang-v2/known/traces/CJ-H007.1.candidate.jsonl.gz) | 1→1 | USB 自动清洗化妆刷 清洗机<br>→ USB automatic makeup brush cleaner machine | 1→1 | 1→1 | — |
| [CJ-H008](../verification/cj-crosslang-v2/known/traces/CJ-H008.1.candidate.jsonl.gz) | —→1 | 玄关换鞋凳 三层鞋架 带软垫 带抽屉<br>→ 3-tier shoe rack bench with cushion and drawer entryway | 1→1 | 1→1 | — |
| [CJ-H009](../verification/cj-crosslang-v2/known/traces/CJ-H009.1.candidate.jsonl.gz) | —→1 | 出差 旅行包 防水 干湿分离 可放鞋 独立鞋仓<br>→ waterproof travel duffel bag shoe compartment wet dry separation | 1→1 | 1→1 | — |
| [CJ-H010](../verification/cj-crosslang-v2/known/traces/CJ-H010.1.candidate.jsonl.gz) | —→2 | 遛狗便携水杯 储水 狗粮二合一 户外宠物饮水杯<br>→ dog water bottle with food storage portable walking | 1→1 | 1→1 | — |
| [CJ-H003](../verification/cj-crosslang-v2/known/traces/CJ-H003.3.candidate.jsonl.gz) | —→— | 电动研磨机 咖啡豆 香料 小型<br>→ electric coffee bean and spice grinder small | 1→1 | 1→1 | category |

### v2 留出集

| 用例 | 首次排名 B→C | 首次 query B→C | 任一轮 B→C | 答案 ID B→C | 首个故障层 |
| --- | ---: | --- | --- | --- | --- |
| [CJ-V2H001](../verification/cj-crosslang-v2/holdout/traces/CJ-V2H001.1.candidate.jsonl.gz) | —→1 | 婴儿背带 纯棉 前后两用 肩带 背宝宝<br>→ baby carrier cotton front back | 1→1 | 1→1 | — |
| [CJ-V2H002](../verification/cj-crosslang-v2/holdout/traces/CJ-V2H002.1.candidate.jsonl.gz) | 1→1 | 演示遥控器 激光笔 无线鼠标 二合一 演示器<br>→ presentation clicker laser pointer wireless mouse remote | 1→1 | 1→1 | — |
| [CJ-V2H003](../verification/cj-crosslang-v2/holdout/traces/CJ-V2H003.1.candidate.jsonl.gz) | —→1 | 宠物户外便携饮水杯 不锈钢 小狗 饮水<br>→ dog outdoor portable water bottle stainless steel | 1→1 | 1→0 | answer |
| [CJ-V2H004](../verification/cj-crosslang-v2/holdout/traces/CJ-V2H004.1.candidate.jsonl.gz) | —→— | H形充气护颈枕 可折叠 便携 旅行 飞机 打盹<br>→ foldable H-shaped inflatable neck pillow travel nap | 1→1 | 1→1 | category |
| [CJ-V2H005](../verification/cj-crosslang-v2/holdout/traces/CJ-V2H005.1.candidate.jsonl.gz) | —→1 | 螺旋形 LED 圣诞树 氛围灯 桌面摆件<br>→ spiral LED Christmas tree ambient lamp tabletop | 1→1 | 1→1 | — |
| [CJ-V2H006](../verification/cj-crosslang-v2/holdout/traces/CJ-V2H006.1.candidate.jsonl.gz) | —→1 | 猫指甲剪 防滑 带定位圆孔 宠物指甲剪<br>→ cat nail clipper anti-slip positioning hole | 0→1 | 0→1 | — |
| [CJ-V2H007](../verification/cj-crosslang-v2/holdout/traces/CJ-V2H007.1.candidate.jsonl.gz) | —→— | 小型厨房勺子秤 称量干湿食材 电子勺秤<br>→ small kitchen spoon scale digital weighing dry and wet ingredients | 1→1 | 1→1 | category |
| [CJ-V2H008](../verification/cj-crosslang-v2/holdout/traces/CJ-V2H008.1.candidate.jsonl.gz) | 3→1 | 蓝牙5.0 飞鼠键盘 遥控 空中鼠标<br>→ bluetooth 5.0 air mouse remote keyboard | 1→1 | 1→1 | — |
| [CJ-V2H009](../verification/cj-crosslang-v2/holdout/traces/CJ-V2H009.1.candidate.jsonl.gz) | —→1 | TA-102P 户外360度双频天线 带黑色支架<br>→ outdoor 360 degree dual band antenna black bracket TA-102P | 1→1 | 1→1 | — |
| [CJ-V2H010](../verification/cj-crosslang-v2/holdout/traces/CJ-V2H010.1.candidate.jsonl.gz) | 1→1 | 家用迷你 LED 口袋投影仪 USB HDMI<br>→ mini LED pocket projector USB HDMI | 1→1 | 1→1 | — |

`—` 表示锚点未进入首次前五；`任一轮` 与 `答案 ID` 以 1/0 表示真/假。
每题只核验一个人工指定的锚点，这些数字是已知商品命中率，不是完整 Recall。
