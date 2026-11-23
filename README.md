IPTV-Direct

自动采集、检测和筛选可用 IPv4 IPTV 直播源，持续生成 IPTV 订阅。

配置文件

- "channels.txt"：目标频道，每行一个。
- "manual.txt"：人工来源，每行一个网址。
- "sources.json"：自动采集来源。
- "settings.json"：运行参数。

自动运行

GitHub Actions 每天北京时间 00:00 自动运行。

也支持手动选择以下运行模式：

- 正式运行
- 测试运行
- 诊断运行

订阅地址

- "M3U 订阅" (https://raw.githubusercontent.com/womomo8/IPTV-Direct/main/output/iptv.m3u)
- "TXT 订阅" (https://raw.githubusercontent.com/womomo8/IPTV-Direct/main/output/iptv.txt)
