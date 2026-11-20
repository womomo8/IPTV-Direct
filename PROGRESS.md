
# IPTV-Direct 开发进度

施工依据：MASTER_PLAN.md v3.9.5。

MASTER_PLAN.md 是项目唯一架构和规则依据。
本文件只记录当前仓库状态、施工进度和验收结果。
如有冲突，以 MASTER_PLAN.md 为准。

## 当前仓库模块

- main.py：全链路唯一总调度
- collector.py：来源管理和原始内容采集
- normalizer.py：解析、频道匹配、URL 规范化和去重
- checker.py：本地检测
- publisher.py：本地质量优选
- globalping.py：远程检测、正式准入和安全发布
- tests/test_globalping.py：Globalping 综合回归测试
- tests/test_globalping_admission.py：远程准入门槛专项测试

## 已完成的工作

- [x] 六阶段程序架构已建立。
- [x] main.py 负责唯一总调度和阶段交接。
- [x] publisher.py 独立负责本地质量优选。
- [x] globalping.py 接收完整优选队列。
- [x] DIRECT_TS 远程只执行普通 HTTP。
- [x] DIRECT_TS 远程 IPv4 状态为 N/A。
- [x] DIRECT_TS 远程 HLS 状态为 N/A。
- [x] DIRECT_TS 总体状态只依据 HTTP。
- [x] Globalping 额度按探针执行次数扣减，测量次数与探针数量分别统计。
- [x] HLS 第二阶段按第一阶段实际探针数量核对额度。
- [x] DIRECT_TS 专项回归测试。
- [x] Globalping 测量额度回归测试。
- [x] HLS 多阶段探针连续性及身份核对测试。
- [x] 预算边界和创建异常统计测试。
- [x] 发布状态未知恢复测试。
- [x] 旧订阅保护及回滚故障测试。
- [x] 远程适用检测项为 INCOMPLETE 时拒绝准入的专项测试。

## 当前修复和验收任务

- [ ] 完成完整集成测试，验证六阶段真实数据交接和全链路结果。
- [ ] 对照 MASTER_PLAN.md 第14章逐项完成最终验收。

## 当前验收状态

项目仍处于开发和修复阶段。

当前专项回归测试及测试模式完整 dry-run 已通过。
这不等于完整集成验收通过，也不代表正式运行和正式发布已完成验证。

在主计划规定的完整集成测试和最终验收完成之前，
不得标记为正式完成。
