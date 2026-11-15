IPTV-Direct

自动采集、检测和筛选可用 IPv4 IPTV 直播源，持续生成 IPTV 订阅。

配置文件

- "channels.txt"：目标频道，每行一个。
- "manual.txt"：人工来源，每行一个网址。
- "sources.json"：自动采集来源。
- "settings.json"：运行参数。

自动运行

GitHub Actions 每天北京时间 06:17 自动运行，也支持手动正式运行、测试运行和诊断。

订阅地址

- "M3U 订阅" (https://raw.githubusercontent.com/womomo8/IPTV-Direct/main/output/iptv.m3u)
- "TXT 订阅" (https://raw.githubusercontent.com/womomo8/IPTV-Direct/main/output/iptv.txt)

项目文档

- "MASTER_PLAN.md"：项目标准。
- "CORE_FLOW.md"：运行流程。
- "PROGRESS.md"：开发进度。
