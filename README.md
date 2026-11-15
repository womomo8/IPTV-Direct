IPTV-Direct

自动采集、检测、筛选 IPv4 IPTV 直播源，并持续生成可用订阅。

项目以中国移动国内网络环境为主要目标，重点区分线路检测结果与国内真实播放验证结果。

项目目标

通过自动化流程，从公开来源和用户手动提供的来源中收集直播线路，经过检测和筛选，生成 IPTV 订阅。

每个频道最多保留 2 条符合准入标准的线路，不为凑数量降低检测要求。

项目正式技术标准以 "MASTER_PLAN.md" (MASTER_PLAN.md) 为准。

项目文档

文件| 用途
MASTER_PLAN.md| 项目正式标准和验收依据
CORE_FLOW.md| 程序执行流程和数据交接
PROGRESS.md| 实际开发进度和测试记录
README.md| 项目介绍和使用说明

当前已建立的内容

- 项目正式标准文件。
- 核心执行流程文件。
- GitHub Actions 手动测试工作流。
- Globalping 中国大陆 IPv4 诊断脚本。

当前 Globalping 诊断脚本使用公开 Mux HLS 测试视频检查探针和媒体访问情况。

注意： 该诊断仅用于检查 Globalping 服务和探针能力，不代表真实 IPTV 频道已经通过检测，也不能证明中国移动国内真实播放成功。

当前使用方法

目前可以通过 GitHub Actions 手动启动已有的 Globalping 诊断：

1. 打开仓库的 Actions 页面。
2. 选择 "IPTV-Direct Unified Workflow"。
3. 点击 "Run workflow"。
4. 选择 "手动测试"。
5. 启动后查看运行日志和诊断结果。

目前尚未实现正式 IPTV 采集、检测、筛选和订阅发布功能。

正式订阅地址

正式订阅生成后，使用以下固定地址：

M3U

https://raw.githubusercontent.com/womomo8/IPTV-Direct/main/output/iptv.m3u

TXT

https://raw.githubusercontent.com/womomo8/IPTV-Direct/main/output/iptv.txt

以上是规划的正式输出地址，目前文件尚未生成。

开发状态

项目从零开始施工，实际开发进度和测试结果以 "PROGRESS.md" (PROGRESS.md) 为准。

只有经过实际测试并达到验收要求的功能，才能标记为完成。
