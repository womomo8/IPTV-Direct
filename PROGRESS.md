
# IPTV-Direct — PROGRESS.md

版本：2.0

项目状态：第4、5、6阶段专项验收已通过；第7阶段 Globalping 模块及专项模拟测试已完成，主程序接入和完整交接验收尚未完成。

记录原则：只记录仓库实际状态和真实测试结果。代码已建立不代表功能验收通过。

---

## 一、当前项目状态

项目正式标准以 `MASTER_PLAN.md` 为准。
程序执行流程以 `CORE_FLOW.md` 为准。
本文件只记录实际施工进度。

六个正式业务程序当前状态：

| 程序 | 当前状态 |
|---|---|
| main.py | 已建立；基础配置、来源采集和内容解析调度已实现；尚未接入 checker、Globalping 和 publisher 的完整业务流程 |
| collector.py | 已建立；来源整理、下载、索引链接发现和分批采集已实现；完整业务链路验收未完成 |
| normalizer.py | 已建立；内容解析、频道匹配、URL归一和去重已实现；第5阶段专项验收通过 |
| checker.py | 已建立；本地 IPv4、HTTP、HLS、TS 检测已实现；第6阶段专项验收通过 |
| globalping.py | 已建立；Globalping API、额度、探针、测量和结果整理已实现；34项专项模拟测试通过；尚未接入 main.py |
| publisher.py | 尚未建立 |

当前系统尚未形成完整的 IPTV 自动筛选和订阅发布闭环。

---

## 二、当前仓库文件情况

| 文件或目录 | 当前状态 | 说明 |
|---|---|---|
| MASTER_PLAN.md | 已建立 | 项目唯一正式标准依据 |
| CORE_FLOW.md | 已建立 | 核心执行流程文件 |
| PROGRESS.md | 已更新 | 本文件 |
| README.md | 已建立 | 简要项目说明 |
| settings.json | 已建立 | 正式运行参数配置 |
| channels.txt | 已建立 | 目前没有实际目标频道 |
| manual.txt | 已建立 | 目前没有实际来源 |
| sources.json | 已建立 | sources 当前为空数组 |
| main.py | 已建立 | 配置读取、来源采集和解析调度 |
| collector.py | 已建立 | 来源整理、下载和索引提取 |
| normalizer.py | 已建立 | 内容解析、频道匹配、归一和去重 |
| checker.py | 已建立 | 本地 IPv4、HTTP、HLS、TS 检测 |
| globalping.py | 已建立 | Globalping 辅助检测模块 |
| publisher.py | 尚未建立 | 正式准入、订阅生成和发布程序 |
| tests/test_checker.py | 已建立 | checker 专项测试 |
| tests/test_checker_limits.py | 已建立 | checker 并发和队列限制测试 |
| tests/test_globalping.py | 已建立 | Globalping 专项模拟测试 |
| .github/workflows/iptv-direct.yml | 已建立 | 定时及三种手动运行入口 |
| tools/globalping_api_test.py | 已建立 | 独立 Globalping 诊断工具 |
| output/iptv.m3u | 未生成 | 正式 M3U 订阅 |
| output/iptv.txt | 未生成 | 正式 TXT 订阅 |

---

## 三、实施阶段进度

| 阶段 | 施工内容 | 当前状态 |
|---|---|---|
| 1 | 项目结构与统一配置 | 基础文件及配置读取已建立，完整验收未完成 |
| 2 | 频道配置 | 读取、检查和精确匹配代码已建立，完整验收未完成 |
| 3 | 人工来源和自动来源 | 来源配置读取、校验和整理已实现，完整验收未完成 |
| 4 | 来源下载和解析 | 已通过 Run #12 专项模拟 HTTP 测试 |
| 5 | 线路归一和去重 | 已通过 Run #12 专项测试 |
| 6 | 本地检测 | 已通过 Run #26，30项专项测试全部通过 |
| 7 | Globalping | 模块已建立，Run #27 的34项专项模拟测试通过；main.py 接入及完整交接验收未完成 |
| 8 | 线路筛选和订阅生成 | 尚未开始 |
| 9 | 发布保护 | 尚未开始 |
| 10 | 自动化和整体验收 | 工作流已建立，完整运行验收未完成 |

说明：

1. 代码建立不等于阶段验收通过。
2. 当前没有完整业务链路的成功运行记录。
3. collector、normalizer、checker 和 Globalping 的专项测试通过，不代表完整业务流程已经验收。
4. Globalping 专项模拟测试不代表真实 Globalping 测量或中国移动网络实播通过。
5. 当前没有实际目标频道和来源配置，无法进行完整 IPTV 运行验收。
6. 不得提前进入阶段8；必须先完成阶段7主程序接入和数据交接验收。

---

## 四、程序实际实现情况

### 4.1 main.py

已实现：

- 正式运行和测试运行参数入口。
- 读取四个根目录配置文件。
- 检查配置文件结构和正式参数。
- 读取目标频道配置。
- 调用 collector.py 整理人工和自动来源。
- 调用 normalizer.py 解析来源内容。
- 汇总已识别来源、有效来源、候选数量和重复数量。
- 执行现有来源数量及候选数量停止判断。
- 输出来源处理结果和异常信息。
- 阶段4来源下载与解析专项测试入口。
- 阶段5线路归一和去重专项测试入口。
- 模拟检测结果交接测试。

尚未实现：

- 按正式流程分批采集、检测和准入。
- 调度 checker.py 完成本地检测。
- 调度 globalping.py 完成辅助检测。
- 调度 publisher.py 完成正式准入和频道线路统计。
- 根据各频道有效线路数量决定是否继续采集。
- 最终订阅生成和发布调度。
- 完整测试运行流程。

当前运行状态：

- 正式运行目前只执行配置读取、来源采集和内容解析，随后安全结束并返回未完成状态。
- 测试运行已覆盖阶段4、阶段5专项测试及模拟检测结果交接。
- 当前测试运行已执行 checker 和 Globalping 专项测试，但尚未形成 Globalping 与 main.py 的完整模拟数据交接测试。
- 配置文件没有实际目标频道时，会在配置检查阶段停止。

### 4.2 collector.py

已实现：

- 读取和校验 manual.txt。
- 读取和校验 sources.json。
- 人工来源优先、自动来源随后。
- 检查来源 URL、ID、名称、启用状态和模式。
- 跳过禁用来源。
- 隔离无效来源配置。
- 支持 playlist 和 index 两种来源模式。
- 下载来源内容。
- 限制下载超时、下载大小和重定向次数。
- 限制 HLS 清单读取大小。
- 解析索引页面并提取播放列表链接。
- 按配置限制索引页面提取数量。
- 下载索引页面发现的播放列表。
- 对来源 URL 去重。
- 使用迭代方式交接来源内容。
- 已通过 Run #12 本地模拟 HTTP 专项测试。

尚未完成：

- 与全部后续业务程序的数据交接验收。
- 真实网络来源下载测试。
- 真实索引页面提取测试。
- 完整业务链路验收。

本地模拟 HTTP 测试不代表真实公网 IPTV 来源兼容性已经验证。

### 4.3 normalizer.py

已实现：

- 读取 channels.txt。
- 忽略空行和注释行。
- 检查重复频道和空频道配置。
- 频道标准名称精确匹配。
- 支持调用方明确提供的别名匹配。
- 识别和解析 TXT、M3U、HLS 等支持的来源内容。
- 提取 M3U 频道信息及直播 URL。
- 解析 TXT 中的频道名称和 URL。
- 对无法确认频道信息的线路拒绝强行匹配。
- 规范化 HTTP/HTTPS URL。
- 去除 URL 片段和默认端口。
- 执行整轮 URL 去重。
- 执行单来源候选数量限制。
- 执行本轮候选剩余额度限制。
- 返回候选数量、目标频道匹配数量、重复数量和解析错误。
- 已通过 Run #12 阶段5专项测试。

频道匹配规则：

- channels.txt 只填写用户确定的标准频道名称。
- 正式流程以标准频道名称精确匹配为基础。
- 只有存在可靠、明确的别名对应关系时，才允许别名映射。
- 不把 tvg-id 自动当成频道别名。
- 不使用字符串相似度猜测频道归属。
- 不新增独立别名配置文件。

尚未完成：

- 完整候选数据交接验收。
- 与 checker.py 的集成验收。
- 真实 IPTV 来源内容测试。

### 4.4 checker.py

已实现：

- IPv4 DNS 解析和实际 IPv4 连接。
- HTTP 和 HTTPS 请求。
- HTTPS SNI 和证书验证。
- 重定向处理。
- HTTP 200 和 HTTP 206 响应处理。
- HLS 播放清单识别和解析。
- HLS 媒体资源检查。
- TS 媒体数据及同步结构检查。
- 检测并发、排队和时间预算控制。
- 按实际媒体内容区分 HLS、直接 TS 和未知类型。
- 检测结果返回 media_type 字段。
- 直接 TS 的 HLS 检测状态为 N/A。
- 异常结果的媒体类型标记为 UNKNOWN。

第6阶段专项验收：

- Run #26。
- 30项 checker 和并发/队列专项测试全部通过。
- 具体记录见第七章。

尚未完成：

- 与 globalping.py 的完整数据交接验收。
- 与 publisher.py 的完整数据交接验收。
- 真实公网 IPTV 来源兼容性测试。
- 中国移动国内实播验证。

### 4.5 globalping.py

已建立。

当前代码已实现：

- Globalping API 通信。
- API JSON 对象和数组响应处理。
- HTTP 304 响应处理。
- HTTP 429 限流处理。
- 实时额度查询和预算控制。
- 中国大陆 IPv4 eyeball-network 探针选择。
- 中国移动探针优先。
- 单次测量最多使用3个探针。
- 测量任务创建和结果轮询。
- ETag 和 304 结果处理。
- HLS 主清单、媒体子清单和 TS Range 分阶段检测。
- 直接 TS 的实际 TS 数据检测。
- 未知或不支持媒体类型拒绝通过。
- Globalping HTTP rawBody 不完整时标记 INCOMPLETE。
- 区分 PASS、FAIL、INCOMPLETE 和 N/A。
- 返回结构化检测结果。
- 对 API、额度、探针和测量异常进行处理。
- 提供 check_candidates(candidates, settings) 接口。

专项模拟测试：

- tests/test_globalping.py 已提交。
- 本地模拟测试共34项，全部通过。
- GitHub Actions Run #27 中 Globalping 专项测试全部通过。

当前尚未完成：

- main.py 尚未导入和调用 globalping.py。
- 尚未将 checker.py 的本地检测结果与 Globalping 检测结果完整交接。
- 尚未验证 main.py 能否正确接收并保留每条线路的 Globalping 状态和失败原因。
- 尚未完成 Globalping INCOMPLETE 降级处理的主流程集成验收。
- 尚未进行真实 Globalping API 和真实探针测量验收。

重要限制：

Run #27 使用模拟 API 和模拟网络响应，不代表真实 Globalping 测量已成功，也不代表中国移动网络实播通过。

阶段7当前只能标记为：

**Globalping 模块及专项模拟测试已完成；主程序接入和完整阶段验收未完成。**

### 4.6 publisher.py

尚未建立。

需要实现：

- 按 MASTER_PLAN.md 执行正式线路准入。
- 统计各频道累计有效线路数量。
- 每个频道最多保留规定数量的合格线路。
- 生成 M3U 和 TXT 订阅。
- 检查两份订阅格式及内容一致性。
- 执行发布前质量保护。
- 正式文件替换。
- 发布后重新读取核验。
- 发布失败时恢复上一份有效订阅。

阶段7完成验收之前，不进入 publisher.py 开发。

---

## 五、配置文件状态

- channels.txt：目前没有实际目标频道。
- manual.txt：目前没有人工来源。
- sources.json：sources 当前为空数组。
- settings.json：已建立，配置结构已由 main.py 检查。

当前没有实际目标频道和来源，因此不能完成完整订阅生成验收。

---

## 六、GitHub Actions 状态

工作流文件：

`.github/workflows/iptv-direct.yml`

已建立：

- 每天北京时间 06:17 自动运行入口。
- 手动正式运行入口。
- 手动测试运行入口。
- 手动诊断入口。
- 正式运行提交订阅文件的步骤。
- 不同运行模式对应的权限设置。

最新工作流测试：

- Run #27。
- 手动测试运行成功。
- checker 专项测试步骤成功。
- Globalping 专项测试步骤成功。
- main.py 测试模式步骤成功。
- 手动诊断任务和正式运行任务在本次测试中按设计跳过。

当前限制：

- 正式运行依赖尚未完成的业务程序。
- 测试运行尚未覆盖完整 Globalping 主程序数据交接。
- 正式运行目前不能生成完整订阅。
- 诊断工具独立运行，不参与正式线路准入。
- 尚无完整业务运行成功的验收记录。

工作流文件已建立不代表各运行模式已经全部验收通过。

---

## 七、测试记录

### 7.1 历史基础测试记录

此前记录的测试包括：

- 频道配置读取。
- 空行和注释处理。
- 重复频道检查。
- 精确匹配和显式别名匹配。
- 来源配置读取。
- 人工来源和自动来源整理。
- 禁用来源跳过。
- 无效 URL 拦截。
- 重复来源 ID 拦截。
- 单条错误来源隔离。
- 主程序来源配置接入模拟测试。

以上属于历史基础测试，不代表当前全部代码已经完成集成验收。

### 7.2 Run #12：阶段4、阶段5专项测试

- Workflow：IPTV-Direct Unified Workflow。
- Run：#12。
- Run ID：43787800907。
- Job ID：133039261601。
- 测试提交：3f9ca524f02f04047e3b80a9553d4f7441d976d9。
- Job 结果：success。
- 测试环境：GitHub Actions，Python 3.15.2。

阶段4实际测试：

- M3U 下载、MIME 识别和频道解析 PASS。
- TXT 下载和频道解析 PASS。
- HLS M3U8 清单识别 PASS。
- 人工来源优先和单来源失败隔离 PASS。
- HTTP 重定向和 HTTP 206 PASS。
- index 相对链接、过滤、去重、40条上限及不递归 PASS。
- 普通来源和 HLS 清单大小限制 PASS。

阶段5实际测试：

- 标准频道名称精确匹配 PASS。
- 明确别名映射函数调用 PASS。
- 拒绝相似名称模糊匹配 PASS。
- URL 归一 PASS。
- 跨来源全局 URL 去重 PASS。
- 无效 URL 和未匹配频道过滤 PASS。
- 单来源候选上限及其他来源继续处理 PASS。
- 本轮剩余候选额度限制 PASS。

隔离情况：

测试使用本地模拟 HTTP 服务和模拟播放列表；未创建 Globalping 测量；未调用正式准入；未修改正式订阅。

限定：

本次证明 collector 和 normalizer 的上述模拟测试用例通过，不代表真实公网 IPTV 来源兼容性，也不代表 checker 的真实公网检测或中国移动实播通过。

阶段4、阶段5专项验收通过。

### 7.3 Run #26：第6阶段本地检测专项验收

- Workflow：IPTV-Direct Unified Workflow。
- Run：#26。
- Run ID：43797731372。
- Job ID：133070279602。
- 测试提交：9a39870ccdc5a241bc4b0c8b09037e6915235d29。
- Job 结果：success。
- 测试环境：GitHub Actions，Python 3.15.2。

实际执行结果：

- checker.py 及相关测试文件语法检查 PASS。
- checker 本地检测专项测试26项 PASS。
- 并发与队列限制专项测试4项 PASS。
- 合计30项全部 PASS。
- python main.py --mode test PASS。

覆盖内容：

- IPv4 socket 连接。
- 连接拒绝。
- HTTP 200/206。
- 空正文。
- 非法 Content-Length/Content-Range。
- 重定向及每跳 IPv4 DNS。
- HTTPS IPv4、SNI 和证书验证配置。
- HLS 主清单和媒体清单。
- 相对地址。
- TS 分片。
- 加密 HLS 和 fMP4 拒绝。
- TS 读取大小限制。
- TS 同步字节检查。
- 超时、并发、队列和排队超时。

隔离情况：

- 使用本机 HTTP 模拟服务和连接层模拟。
- main.py 测试模式使用模拟检测结果。
- 没有执行真实 Globalping 测量。
- 没有调用正式准入。
- 没有修改正式订阅。

限定：

本次证明 checker.py 的本地检测逻辑和相关边界测试通过，不代表真实公网 IPTV 来源兼容性、中国移动家庭宽带或移动网络实播通过。

第6阶段本地检测专项验收通过。

### 7.4 Run #27：第7阶段 Globalping 专项模拟测试

- Workflow：IPTV-Direct Unified Workflow。
- Run：#27。
- Run ID：43800759599。
- Job ID：133080201954。
- 测试提交：f0d07f4bac8c4d196779d5d7740b421df19b79c6。
- Job 结果：success。
- 测试环境：GitHub Actions，Python 3.15.2。

实际执行步骤：

- Python 语法检查 PASS。
- checker 专项测试步骤 PASS。
- Globalping 专项测试步骤 PASS。
- main.py 测试模式步骤 PASS。

Globalping 专项测试：

- tests/test_globalping.py 共34项。
- 34项全部通过。

覆盖内容包括：

- API JSON 对象和数组响应。
- HTTP 304。
- HTTP 429 限流及停止创建新测量。
- 额度和测量预算。
- 中国大陆探针选择及中国移动优先。
- 探针数量限制。
- URL 检查。
- rawBody 完整性和大小限制。
- TS 同步检测。
- HLS 主清单和媒体子清单解析。
- TS Range 检测。
- N/A 状态语义。
- 未知媒体类型拒绝。
- 直接 TS 检测。
- HLS 分阶段检测要求。
- 测量异常和未完成状态处理。

隔离情况：

- 使用模拟 API 响应和模拟网络数据。
- 未调用真实 Globalping 测量。
- 未消耗真实测量额度。
- 未覆盖正式订阅。
- 本次工作流的诊断任务和正式运行任务均按设计跳过。

限定：

Run #27 证明 Globalping 模块的专项模拟测试通过，不代表真实 Globalping API、真实探针测量或中国移动网络实播已经通过。

阶段判断：

- Globalping 模块已经建立。
- 34项专项模拟测试已经通过。
- main.py 尚未接入 Globalping。
- 完整数据交接、主流程异常降级和阶段7整体验收尚未完成。

### 7.5 尚未完成的测试

- collector.py 真实网络下载测试。
- 索引页面真实链接提取测试。
- normalizer.py 完整真实来源内容测试。
- 候选数据全链路交接测试。
- checker.py 与 Globalping 的完整数据交接验收。
- main.py 对 Globalping 结果的接收和状态保留测试。
- Globalping INCOMPLETE 降级流程集成测试。
- 真实 Globalping API 和真实探针测量测试。
- 正式准入和订阅生成测试。
- 发布保护和回滚测试。
- GitHub Actions 完整运行测试。
- 正式运行和订阅发布验收。

没有实际测试证据，不得标记为通过。模拟测试不得当作真实网络测试。

---

## 八、当前待办事项

严格按 MASTER_PLAN.md 和 CORE_FLOW.md 的顺序执行：

1. 阶段4、阶段5专项验收已通过；后续仍需在完整业务链路中复核来源解析和候选数据交接。
2. 阶段6 checker.py 本地检测专项验收已通过；真实公网来源兼容性和中国移动国内实播状态仍需独立记录。
3. 完成阶段7：检查 globalping.py 与 checker.py 的输入输出结构。
4. 完成阶段7：由 main.py 正式调度 globalping.py，将本批候选线路和正式配置传入。
5. 完成阶段7：接收 Globalping 返回结果，保留线路对应关系、探针信息、各阶段状态、总体状态和失败原因。
6. 完成阶段7：验证 Globalping PASS、FAIL、INCOMPLETE、N/A 的主流程处理及 MASTER_PLAN.md 规定的降级规则。
7. 完成阶段7：补充 main.py 与 Globalping 的模拟集成测试，确保测试模式不创建真实测量、不消耗额度、不覆盖订阅。
8. 执行阶段7专项测试和集成测试，核对真实 Actions 结果，满足验收要求后再进入阶段8。
9. 阶段7验收通过后，才开始 publisher.py 的正式准入和订阅生成。
10. 后续按既定阶段完成发布保护、失败恢复和整体验收。

当前唯一下一步：

**先完成 main.py 对 globalping.py 的正式接入和数据交接测试。**

不得提前开发 publisher.py，不得跳过阶段7验收。

---

## 九、进度更新规则

1. 只记录仓库中实际存在的代码和真实测试结果。
2. 文件已提交不等于功能已经完成。
3. 程序能够运行不等于功能验收通过。
4. 模拟测试和真实网络测试必须分别记录。
5. 未通过验收的功能不得标记为完成。
