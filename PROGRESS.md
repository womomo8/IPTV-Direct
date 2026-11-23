PROGRESS.md

IPTV-Direct 程序流程

一、程序组成

原有六个程序全部保留，只新增一个程序 "stream.py"。

1. "main.py"：统一调度。
2. "collector.py"：来源采集。
3. "normalizer.py"：解析、频道匹配和去重。
4. "checker.py"：原有基础线路检测。
5. "stream.py"：使用 FFmpeg / FFprobe 检测真实视频质量。
6. "globalping.py"：外部探针检测。
7. "publisher.py"：结合原有规则和质量结果，决定最终准入并生成、发布订阅。

二、完整工作流程

              main.py
                 |
                 v
            collector.py
                 |
                 v
              main.py
                 |
                 v
           normalizer.py
                 |
                 v
              main.py
                 |
                 v
             checker.py
                 |
                 v
              main.py
                 |
                 v
              stream.py
          FFmpeg / FFprobe
            视频质量检测
                 |
                 v
             质量筛选
              /     \
          不合格     合格
             |        |
             v        v
            淘汰    返回 main.py
                       |
                       v
                 globalping.py
                       |
                       v
                    main.py
                       |
                       v
                  publisher.py
                       |
                       v
             原有规则 + 质量结果
                  最终准入
                       |
                       v
                    main.py
                       |
                       v
                判断下一个来源
                  /        \
                继续       结束
                 |          |
                 v          v
              下一来源   生成并发布订阅
                            |
                            v
                           结束

三、固定规则

1. 原有六个程序的职责和规则保持不变。
2. 只新增 "stream.py"，由 "main.py" 统一调度。
3. 每条进入 "stream.py" 的线路只检测一次。
4. 视频质量不合格，直接淘汰，不进入 Globalping。
5. 视频质量合格，返回 "main.py"，继续原有 Globalping 检测。
6. "publisher.py" 结合原有准入规则和视频质量结果，决定最终是否收录。
7. 每个频道最多保留两条合格线路，原有规则不变。
8. "main.py" 根据 "publisher.py" 返回的结果决定是否继续下一个来源。
9. 全部来源处理完成后，生成并发布订阅。

四、当前进度

- [x] 确定新增 "stream.py"。
- [x] 确定七个程序的职责。
- [x] 确定完整工作流程。
- [ ] 实施 "stream.py"。
- [ ] 接入 "main.py"。
- [ ] 接入 "publisher.py"。
- [ ] 完成测试和正式运行验证。
