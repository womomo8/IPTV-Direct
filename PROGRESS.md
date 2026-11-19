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
- tests/test_globalping.py：Globalping 回归测试

## 已完成的工作

- [x] 六阶段程序架构已建立。
- [x] main.py 负责唯一总调度和阶段交接。
- [x] publisher.py 独立负责本地质量优选。
- [x] globalping.py 接收完整优选队列。
- [x] DIRECT_TS 远程只执行普通 HTTP。
- [x] DIRECT_TS 远程 IPv4 状态为 N/A。
- [x] DIRECT_TS 远程 HLS 状态为 N/A。
- [x] DIRECT_TS 总体状态只依据 HTTP。

## 当前修复和验收任务

- [ ] 修复 Globalping 测量额度扣减口径。
- [ ] 测量创建额度与探针数量分开计算。
- [ ] 修复 HLS 第二阶段额度判断。
- [ ] 增加 DIRECT_TS 专项回归测试。
- [ ] 增加测量额度回归测试。
- [ ] 完成 HLS 多阶段探针连续性测试。
- [ ] 完成预算边界和异常统计测试。
- [ ] 完成发布状态未知恢复测试。
- [ ] 完成旧订阅保护及回滚故障测试。
- [ ] 完成完整集成测试。
- [ ] 对照 MASTER_PLAN.md 规定的验收章节逐项验收。

## 当前验收状态

项目仍处于开发和修复阶段。

局部代码修改完成，不代表测试已经通过。
单元测试通过，也不代表完整集成验收通过。

在所有主计划规定的测试和验收项目完成之前，
不得标记为正式完成，也不得执行正式发布。
