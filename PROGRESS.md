# IPTV-Direct 开发进度

施工依据：`MASTER_PLAN.md` v3.9.5。本文只记录当前分支现状和施工状态，不改变主计划定义。

## 当前分支盘点

- 当前保留：`collector.py`、`normalizer.py`、`checker.py`、`publisher.py`、`settings.json`、`sources.json`、`channels.txt`、`manual.txt`、`tools/globalping_api_test.py`、现有 GitHub Actions 工作流。
- 当前缺失：`main.py`、`globalping.py`、`tests/`。
- 当前开发按现存模块接口衔接，不恢复历史版本文件。

## 本轮文件状态

- [x] 新建 `main.py`：唯一总调度、阶段交接和候选身份核对。
- [x] 新建 `globalping.py`：远程检测、逐候选准入、频道名额控制和发布保护初版。
- [x] 新建当前回归测试：Globalping 聚合、普通 HTTP/HLS 的 206 完整性、主播放清单子清单提取、测试模式和发布输入保护。
- [x] 调整现有工作流：测试阶段只执行当前存在的测试文件，保留正式、测试、诊断入口和正式发布并发组。
- [ ] 与仓库现存模块做完整集成测试。
- [ ] 补齐 Globalping HLS 多阶段探针连续性、预算边界和异常统计验收。
- [ ] 补齐发布状态未知恢复、旧订阅保护及回滚故障注入测试。
- [ ] 对照 `MASTER_PLAN.md` 第十四章逐项验收。

## 当前验证结果

- `main.py`、`globalping.py`、`tests/test_globalping_current.py` Python 语法检查通过。
- 当前回归测试：9 项通过。
- 尚未执行真实 Globalping 测量；尚未执行完整 `main.py --mode test` 集成流程。
- 本轮文件是开发中的第一批，不代表主计划验收完成。
