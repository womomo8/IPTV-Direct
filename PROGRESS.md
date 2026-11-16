IPTV-Direct — PROGRESS.md

版本：3.0

项目状态：第7阶段 Globalping 主程序接入及模拟数据交接验收已通过；第8阶段线路筛选和订阅生成尚未开始。

记录原则：只记录仓库实际状态和真实测试结果。代码已建立不代表功能验收通过；模拟测试不代表真实网络测试。

---

一、当前项目状态

项目正式标准以 "MASTER_PLAN.md" 为准。
程序执行流程以 "CORE_FLOW.md" 为准。
本文件只记录实际施工进度。

六个正式业务程序当前状态：

程序| 当前状态
main.py| 已接入 collector、normalizer、checker 和 globalping；已完成 Globalping 主程序交接模拟测试；尚未接入 publisher
collector.py| 已建立；来源整理、下载、索引链接发现和采集限制已实现
normalizer.py| 已建立；来源内容解析、频道匹配、URL 归一和去重已实现
checker.py| 已建立；本地 IPv4、HTTP、HLS、TS 检测已实现；第6阶段专项验收通过
globalping.py| 已建立；Globalping API、额度、探针、测量和结果整理已实现；第7阶段专项及主程序交接验收通过
publisher.py| 尚未建立；第8阶段开始实施

当前系统已经具备来源处理、本地检测和 Globalping 辅助检测的程序接入，但尚未形成正式线路准入、订阅生成和发布闭环。

---

二、当前仓库文件情况

文件或目录| 当前状态| 说明
MASTER_PLAN.md| 已建立| 项目唯一正式标准依据
CORE_FLOW.md| 已建立| 核心执行流程文件
PROGRESS.md| 已更新| 本文件
README.md| 已建立| 项目介绍和使用说明
settings.json| 已建立| 正式运行参数配置
channels.txt| 已建立| 当前没有实际目标频道
manual.txt| 已建立| 当前没有人工来源
sources.json| 已建立| sources 当前为空数组
main.py| 已建立| 已接入本地检测及 Globalping 检测
collector.py| 已建立| 来源整理、下载和索引提取
normalizer.py| 已建立| 内容解析、频道匹配、归一和去重
checker.py| 已建立| 本地 IPv4、HTTP、HLS、TS 检测
globalping.py| 已建立| Globalping 辅助检测
publisher.py| 尚未建立| 第8阶段实施内容
tests/test_checker.py| 已建立| checker 专项测试
tests/test_checker_limits.py| 已建立| checker 并发和队列限制测试
tests/test_globalping.py| 已建立| Globalping 专项模拟测试
tests/test_main_globalping.py| 已建立| main.py 与 Globalping 交接模拟测试
.github/workflows/iptv-direct.yml| 已建立| 定时及三种手动运行入口
tools/globalping_api_test.py| 已建立| 独立 Globalping 诊断工具
output/iptv.m3u| 未生成| 正式 M3U 订阅
output/iptv.txt| 未生成| 正式 TXT 订阅

---

三、实施阶段进度

阶段| 施工内容| 当前状态
1| 项目结构与统一配置| 基础文件及配置读取已建立，完整验收未完成
2| 频道配置| 读取、检查和精确匹配代码已建立，完整验收未完成
3| 人工来源和自动来源| 来源配置读取、校验和整理已实现，完整验收未完成
4| 来源下载和解析| Run #12 专项模拟 HTTP 测试通过
5| 线路归一和去重| Run #12 专项测试通过
6| 本地检测| Run #26，30项专项测试通过
7| Globalping| Run #30，主程序交接模拟测试通过
8| 线路筛选和订阅生成| 尚未开始
9| 发布保护| 尚未开始
10| 自动化和整体验收| 工作流已建立，完整运行验收未完成

说明：

1. 阶段4、5、6、7的专项测试通过，不代表全部业务链路已经验收。
2. 阶段7已完成 Globalping 模块、主程序接入和模拟数据交接验收。
3. Globalping 模拟测试不代表真实 Globalping 探针测量成功。
4. 当前没有实际目标频道和来源配置，无法进行真实订阅生成验收。
5. 第8阶段必须依据 MASTER_PLAN.md 和 CORE_FLOW.md 实施，不得自行降低线路准入标准。

---

四、程序实际实现情况

4.1 main.py

已实现：

- 正式运行和测试运行参数入口。
- 读取四个根目录配置文件。
- 检查配置文件结构和正式参数。
- 读取目标频道配置。
- 调用 collector.py 整理人工和自动来源。
- 调用 normalizer.py 解析来源内容。
- 汇总来源处理和候选数量信息。
- 执行现有来源数量及候选数量停止判断。
- 导入 checker.py 和 globalping.py。
- 通过 "run_detection_pipeline()" 执行本地检测与 Globalping 交接。
- 本地检测未全部通过时，不提交 Globalping。
- 校验本地检测和 Globalping 返回数量及候选身份。
- 保留 Globalping PASS、FAIL、INCOMPLETE 状态。
- Globalping 调用异常或结果不符合要求时，降级标记为 INCOMPLETE。
- 保留直接 TS 的 HLS N/A 状态。
- 保持多条候选线路原始顺序。
- 测试模式提供模拟检测交接测试。

尚未实现：

- 调度 publisher.py 执行正式线路准入。
- 统计各频道累计有效线路数量。
- 根据正式准入结果决定是否继续采集下一批。
- 最终订阅生成和发布调度。
- 完整业务链路验收。

当前运行状态：

- 正式运行尚未完成订阅生成和发布，不能视为正式业务闭环。
- 测试运行已覆盖阶段4、阶段5、checker、Globalping及主程序交接模拟测试。
- 测试模式不调用真实 checker 网络检测和 Globalping API，不执行正式准入和订阅发布。

4.2 collector.py

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

4.3 normalizer.py

已实现：

- 读取 channels.txt。
- 忽略空行和注释行。
- 检查重复频道和空频道配置。
- 频道标准名称精确匹配。
- 支持调用方明确提供的别名匹配。
- 识别和解析 TXT、M3U、HLS 等支持的来源内容。
- 提取频道信息及直播 URL。
- 对无法确认频道信息的线路拒绝强行匹配。
- 规范化 HTTP/HTTPS URL。
- 去除 URL 片段和默认端口。
- 执行全局 URL 去重。
- 执行单来源候选数量限制。
- 执行本轮候选剩余额度限制。
- 返回候选数量、目标频道匹配数量、重复数量和解析错误。
- 已通过 Run #12 阶段5专项测试。

频道匹配规则：

- channels.txt 填写用户确定的标准频道名称。
- 正式流程以标准频道名称精确匹配为基础。
- 只有存在可靠、明确的别名对应关系时，才允许别名映射。
- 不把 tvg-id 自动当成频道别名。
- 不使用字符串相似度猜测频道归属。
- 不新增独立别名配置文件。

尚未完成：

- 完整候选数据交接验收。
- 与 publisher.py 的集成验收。
- 真实 IPTV 来源内容测试。

4.4 checker.py

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
- checker 本地检测专项测试26项通过。
- 并发与队列限制专项测试4项通过。
- 合计30项全部通过。

尚未完成：

- 与 publisher.py 的完整数据交接验收。
- 真实公网 IPTV 来源兼容性测试。
- 中国移动国内实播验证。

4.5 globalping.py

已实现：

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

- tests/test_globalping.py 共34项。
- Run #27 和 Run #30 均通过 Globalping 专项测试。

尚未完成：

- 与 publisher.py 的完整数据交接验收。
- 真实 Globalping API 和真实探针测量验收。

重要限制：

专项测试使用模拟 API 响应和模拟网络数据，不代表真实 Globalping 测量已成功，也不代表中国移动网络实播通过。

4.6 publisher.py

尚未建立。

第8阶段需要依据 MASTER_PLAN.md 实现：

- 正式线路准入判断。
- 按实际媒体类型检查适用检测状态。
- 正确处理 Globalping FAIL 和 INCOMPLETE。
- 保留国内真实网络验证状态，不得伪造实播通过。
- 人工来源优先于自动补充来源。
- 按明确的来源优先级和原始顺序选择线路。
- 每个频道最多保留2条合格线路。
- 统计各频道累计有效线路数量并反馈 main.py。
- 使用同一份最终筛选结果生成 M3U 和 TXT。
- 检查订阅格式、内容及频道线路一致性。

第9阶段再实施发布保护、正式文件替换、发布后核验和失败回滚。

---

五、配置文件状态

- channels.txt：目前没有实际目标频道。
- manual.txt：目前没有人工来源。
- sources.json：sources 当前为空数组。
- settings.json：已建立，配置结构已由 main.py 检查。

当前没有实际目标频道和来源，因此不能完成真实订阅生成验收。

---

六、GitHub Actions 状态

工作流文件：

".github/workflows/iptv-direct.yml"

已建立：

- 每天北京时间 06:17 自动运行入口。
- 手动正式运行入口。
- 手动测试运行入口。
- 手动诊断入口。
- 正式运行提交订阅文件的步骤。
- 不同运行模式对应的权限设置。

最新测试：

- Run #30。
- 手动测试运行成功。
- checker 专项测试步骤成功。
- Globalping 专项测试步骤成功。
- main.py 与 Globalping 交接测试步骤成功。
- 原有测试流程成功。
- 正式运行任务和诊断任务按本次测试模式设计跳过。

当前限制：

- 正式运行尚未形成完整准入和订阅发布流程。
- publisher.py 尚未建立。
- 当前没有实际目标频道和来源配置。
- 尚无正式订阅生成及发布成功的验收记录。

---

七、测试记录

7.1 Run #12：阶段4、阶段5专项测试

- Workflow：IPTV-Direct Unified Workflow。
- Run：#12。
- Run ID：43787800907。
- Job ID：133039261601。
- 测试提交：3f9ca524f02f04047e3b80a9553d4f7441d976d9。
- Job 结果：success。

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
- 明确别名映射 PASS。
- 拒绝相似名称模糊匹配 PASS。
- URL 归一 PASS。
- 跨来源全局 URL 去重 PASS。
- 无效 URL 和未匹配频道过滤 PASS。
- 单来源候选上限及其他来源继续处理 PASS。
- 本轮剩余候选额度限制 PASS。

隔离情况：

使用本地模拟 HTTP 服务和模拟播放列表；未创建 Globalping 测量；未调用正式准入；未修改正式订阅。

阶段4、阶段5专项验收通过。

7.2 Run #26：第6阶段本地检测专项验收

- Workflow：IPTV-Direct Unified Workflow。
- Run：#26。
- Run ID：43797731372。
- Job ID：133070279602。
- 测试提交：9a39870ccdc5a241bc4b0c8b09037e6915235d29。
- Job 结果：success。
- 测试环境：GitHub Actions。

实际执行结果：

- checker.py 及相关测试文件语法检查 PASS。
- checker 本地检测专项测试26项 PASS。
- 并发与队列限制专项测试4项 PASS。
- 合计30项全部 PASS。
- python main.py --mode test PASS。

隔离情况：

使用本机 HTTP 模拟服务和连接层模拟；没有执行真实 Globalping 测量，没有调用正式准入，没有修改正式订阅。

第6阶段本地检测专项验收通过。

7.3 Run #27：第7阶段 Globalping 专项模拟测试

- Workflow：IPTV-Direct Unified Workflow。
- Run：#27。
- Run ID：43800759599。
- Job ID：133080201954。
- 测试提交：f0d07f4bac8c4d196779d5d7740b421df19b79c6。
- Job 结果：success。

实际执行结果：

- Python 语法检查 PASS。
- checker 专项测试 PASS。
- Globalping 专项测试 PASS。
- main.py 测试模式 PASS。

Globalping 专项测试共34项，全部通过。

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

使用模拟 API 响应和模拟网络数据；未调用真实 Globalping 测量；未消耗真实测量额度；未覆盖正式订阅。

7.4 Run #29：主程序交接测试首次失败

- Run：#29。
- Run ID：43805734514。
- Job 结果：failure。

失败原因：

"tests/test_main_globalping.py" 中直接 TS 测试的模拟远程结果没有保留本地 checks 数据，导致测试读取本地 HLS 状态时出现 KeyError。

处理结果：

修正测试夹具，使模拟远程结果保留本地检测数据，再重新执行完整测试。

本次失败属于测试数据构造问题，不代表真实 Globalping 测量失败。

7.5 Run #30：第7阶段主程序接入和完整交接验收

<badge>验收通过</badge>

- Workflow：IPTV-Direct Unified Workflow。
- Run：#30。
- Run ID：43806083097。
- Job ID：133097066623。
- 测试提交：ee43a5c267e7c669fe998c989073c76adbd9fe44。
- 当前 main 分支提交：ee43a5c267e7c669fe998c989073c76adbd9fe44。
- Job 结果：success。
- 测试环境：GitHub Actions，Python 3.15.2。

实际执行结果：

测试步骤| 结果
Python 语法检查| PASS
checker 专项测试| 30项全部通过
Globalping 专项测试| 34项全部通过
main.py 与 Globalping 交接测试| 9项全部通过
原有 main.py 测试流程| PASS

main.py 与 Globalping 交接测试覆盖：

- HLS PASS 状态保留。
- HLS FAIL 状态保留。
- HLS INCOMPLETE 状态保留。
- 直接 TS 的 HLS N/A 状态保留。
- 本地检测失败时不调用 Globalping。
- Globalping 调用异常时降级为 INCOMPLETE。
- Globalping 返回身份不匹配时降级为 INCOMPLETE。
- Globalping 返回无效状态时降级为 INCOMPLETE。
- 多条候选线路保持原始顺序及各自检测状态。

原有测试流程实际输出：

- 阶段4来源下载与解析测试全部 PASS。
- 阶段5线路归一和去重专项测试全部 PASS。
- 主程序交接测试 PASS。
- 测试模式未调用真实 checker 网络检测、Globalping API、正式准入或订阅发布。

隔离情况：

- 使用模拟检测函数。
- 未发起真实 Globalping 测量。
- 未消耗真实 Globalping 测量额度。
- 未执行正式线路准入。
- 未生成或覆盖正式订阅。

限定：

Run #30 证明主程序与 Globalping 的模拟数据交接及异常降级测试通过，不代表真实 Globalping API、真实探针测量或中国移动网络实播已经通过。

第7阶段 Globalping 主程序接入及模拟数据交接验收通过。

---

八、当前待办事项

严格按照 MASTER_PLAN.md 和 CORE_FLOW.md 执行。

1. 第7阶段已经验收通过，不再重复开发或重复添加相同交接测试。
2. 开始第8阶段：线路筛选和订阅生成。
3. 实施前核对 MASTER_PLAN.md 中的正式准入条件、线路保留顺序、每频道数量上限及输出格式。
4. 核对 main.py、checker.py、globalping.py 当前实际数据结构，确定 publisher.py 的输入输出接口。
5. 先明确 publisher.py 与 main.py 的数据交接，再编写完整程序。
6. 为正式准入、线路排序、频道数量限制、Globalping 降级及 M3U/TXT 一致性建立专项模拟测试。
7. 测试运行不得调用真实 Globalping 测量，不得覆盖正式订阅。
8. 完成第8阶段测试并核对 GitHub Actions 实际结果。
9. 第8阶段通过后，再进入第9阶段发布保护和失败恢复。

当前唯一下一步：实施第8阶段前，核对 MASTER_PLAN.md 的准入及订阅输出标准，并检查现有 main.py、checker.py、globalping.py 的实际数据结构，确定 publisher.py 接口。

不得跳过接口核对直接编写 publisher.py，不得提前进入第9阶段。

---

九、进度更新规则

1. 只记录仓库中实际存在的代码和真实测试结果。
2. 文件已提交不等于功能已经完成。
3. 程序能够运行不等于功能验收通过。
4. 模拟测试和真实网络测试必须分别记录。
5. 未通过验收的功能不得标记为完成。
6. 不得把 Globalping 云端探针结果写成中国移动国内实播验证结果。
7. 所有后续修改必须遵守 MASTER_PLAN.md 和 CORE_FLOW.md。
