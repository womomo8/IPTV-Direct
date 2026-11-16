# IPTV-Direct — PROGRESS.md

版本：4.2
更新日期：2026-11-17

## 一、项目当前状态

项目最终目标：

自动筛选真正可用的 IPv4 IPTV 直播源，并持续输出一份可用订阅。

正式标准以 `MASTER_PLAN.md` 为唯一依据。

当前进度：

- 六个核心程序的架构和职责已经确定。
- checker 已具备本地检测质量观测数据。
- main 已实现多来源候选汇集、统一本地检测和同频道质量排序。
- Globalping 已接入实时额度检查和测量预算保护。
- publisher 继续独占正式准入、线路保留和订阅发布。
- 最新手动测试 Run #69 已成功，测试对应 SHA 与 main 分支一致。

当前工作重点：

1. 核查 publisher 是否正确承接优选检测结果。
2. 核查 normalizer 的候选发现覆盖，避免过早截断。
3. 完成端到端验收，重点检查频道数量和最终订阅质量。

本文件只记录真实进度、测试证据和下一步工作，不另行制定正式标准。

## 二、固定架构

| 程序 | 固定职责 |
|---|---|
| `main.py` | 总入口、流程调度、候选池组织和阶段交接 |
| `collector.py` | 来源管理、内容下载和索引链接提取 |
| `normalizer.py` | 内容解析、频道匹配、URL 归一和去重 |
| `checker.py` | 本地 IPv4、HTTP、HLS、TS 检测 |
| `globalping.py` | Globalping API、额度管理和外部检测 |
| `publisher.py` | 正式准入、线路统计、订阅生成和发布保护 |

固定原则：

1. 每个频道最多保留 2 条合格线路。
2. 不使用加权评分系统。
3. 不把 Globalping 的 INCOMPLETE 改成 PASS 或 FAIL。
4. 不把云端检测冒充中国大陆家庭网络实播。
5. 不新增复杂历史数据库和持久黑名单。
6. 单个来源或候选失败不能无故阻断其他候选。
7. publisher 独占最终准入。

## 三、最新测试证据

### Run #69：最新手动测试

- Workflow：IPTV-Direct Unified Workflow
- Run：#69
- 运行 ID：43936399504
- 测试 Job：133542025126
- 测试结果：success
- 测试提交 SHA：

`a0a02141c47d552cd15d0e589ee26d6e3f18ef81`

核对结果：

- 当前 main 分支 SHA 与测试 SHA 一致。
- 手动测试任务成功。
- Globalping 诊断任务按预期跳过。
- 正式运行任务按预期跳过。
- 没有创建真实 Globalping 测量。
- 没有消耗 Globalping 额度。
- 没有覆盖正式订阅。

本次测试覆盖：

1. Python 语法检查。
2. checker 专项测试。
3. Globalping 专项测试。
4. main 与 Globalping 交接测试。
5. publisher 专项测试。
6. main 跨来源候选池、批次和停止条件测试。
7. main 异常退出测试。
8. main 测试模式运行。

这次结果证明当前提交版本的自动化测试通过，不代表已经完成中国大陆家庭网络实播验证。

## 四、当前代码基线

本次核对的 main 分支文件：

| 文件 | 当前 SHA |
|---|---|
| `main.py` | `46999adc5f372690c581c6dc37db294115ce1faa` |
| `checker.py` | `4b1f68ff33079cfe2bd95f10367e2a0bf69e1a4f` |
| `globalping.py` | `492d0ccb7a8254081bf1392d423b1f21e4b5bad6` |
| `publisher.py` | `88ae8de081d2f78061baf8803d73d549bbd417ed` |
| `normalizer.py` | `bdbef8afcf246949961a4d397fedc963d67624a9` |
| `settings.json` | `03ab4ccb98de98b9d12946e355b5386e3df99820` |
| `tests/test_main_collection.py` | `e7e8dd6b3309cbef143b7a0a7d6065be3a836f18` |
| `tests/test_main_globalping.py` | `cc3b23280bf3e114c6e9acc199db14e3c89056fc` |

## 五、已完成的功能核对

### 5.1 checker 质量观测

当前 checker 已记录：

- 请求数量。
- 请求成功和失败数量。
- 超时次数。
- 实际读取字节数。
- 请求总耗时。
- 内容读取耗时。
- HLS 分片总数和成功数量。
- 请求样本数据。

这些数据只用于质量比较和诊断，不改变原有检测结论。

checker 的质量观测修改已经通过后续自动化测试。

不重复修改已经通过的质量观测部分。

### 5.2 main 候选汇集

当前 main 已实现：

1. 先处理人工来源，再处理自动来源。
2. 逐来源采集和解析。
3. 全局 URL 去重。
4. 形成完整候选池。
5. 所有来源处理结束后，统一执行本地检测。
6. 本地检测只调用一次统一流程。
7. publisher 在整轮候选检测完成后统一处理。

测试已经覆盖：

- 多来源全部处理后才开始检测。
- 不因单个来源失败而中断后续来源。
- 来源数量上限。
- 总候选数量上限。
- 统一检测和统一准入。

### 5.3 main 同频道质量排序

当前 main 已实现：

- 按频道分组。
- 按媒体类型分别比较。
- 使用 checker 的真实质量数据。
- 不计算综合评分。
- 缺失质量数据不伪装成零。
- 同等条件保持原始顺序。
- 不同频道和不同媒体类型不直接混排。
- 各频道、媒体类型组按优选顺序轮流交接。

### 5.4 Globalping 额度保护

当前 Globalping 已实现：

1. 每轮读取实时剩余额度。
2. 每次测量最多使用配置允许的探针数，最高不超过 3。
3. HLS 按最多两个检测阶段预留额度。
4. 直接 TS 按一个检测阶段预留额度。
5. 剩余额度不足以完成候选检测时，不创建该候选的新测量。
6. 额度不足的候选保留 INCOMPLETE。
7. HTTP 429 后停止后续测量创建。
8. 不使用 Globalping 验证 TS 二进制同步结构。

当前 main 会把本地全部 PASS 候选按优选顺序交给 Globalping 检测入口；实际测量由 Globalping 的实时额度预算控制。

不能把进入检测入口的数量等同于实际创建的测量数量。

### 5.5 publisher 正式准入

当前 publisher 已核对：

- 人工来源优先。
- 按来源优先级和来源原始顺序处理。
- 检查本地适用检测结果。
- 检查 Globalping PASS、FAIL、INCOMPLETE。
- 允许符合既定规则的 INCOMPLETE 降级准入。
- 每频道最多保留 2 条。
- 独占最终准入和订阅生成。

当前 publisher 不直接按质量数据重新排序。

质量数据用于安排 Globalping 检测优先顺序；正式准入仍按已经确认的来源优先级政策执行。

## 六、当前完整流程

```text
人工来源
    ↓
自动来源
    ↓
collector 采集
    ↓
normalizer 解析、频道匹配、去重
    ↓
形成完整候选池
    ↓
checker 统一本地检测
    ↓
记录真实质量数据
    ↓
main 按频道和媒体类型优选
    ↓
Globalping 按优选顺序检测
    ↓
实时额度控制
    ↓
publisher 正式准入
    ↓
人工来源优先
    ↓
每频道最多保留 2 条
    ↓
生成 M3U / TXT
    ↓
正式发布保护
