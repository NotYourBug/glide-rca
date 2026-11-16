# M0 日志映射修复交付记录

日期：2026-11-16。状态：**本次映射拒收修复和限定验收已完成，提交用户审查；不进入M1**。

本次为用户授权的M0缺陷迭代，依据[修订方案](../design/stages/m0-log-ingestion-fix.md)，不启动M1。原M0的资源/故障验证仍保留其历史事实，本报告单独记录日志修复。

## 根因与修复范围

Cart启动日志含 `attributes.http="8080"`，旧索引将其推断为text；Frontend请求日志含 `http.request.method`，OpenSearch要求同一路径为对象，导致永久拒收。Collector自日志又经同一出口回灌，会放大导出错误。

修复采用 `otel-logs-v2-*` 新索引及固定模板；服务、事件/观察时间、正文、trace/span建立明确索引，自由日志/scope属性不解析、完整保存在 `_source`。Collector自日志保留Docker stderr，内部指标继续进入Prometheus。Grafana沿用原UID读取新索引，探针使用可配置的安全索引定位。

历史旧索引保持不变，修复前为138875条；初始化只允许创建新资源或复用兼容资源，不能覆盖不兼容的模板、字段或写入设置。已永久丢弃的历史记录无法恢复。

## 验收记录

| 检查项 | 当前结果 | 本机证据 |
| --- | --- | --- |
| 根因闭环 | 已确认Cart纯量与Frontend点号字段冲突 | `http-source.json`、原M0导出错误样本 |
| 配置验证 | 固定Collector整组validate退出0；25服务、3回环端口 | `compose.resolved.json` |
| 初始化保护 | 通过；真实重复运行均复用既有资源，无覆盖 | 原始template/mapping/settings快照、16项初始化回归 |
| 异构日志端到端 | 通过；11条全部入库，纳秒等值窗/正文检索均通过 | `controlled-1d21c0c135df45f0ac4cac31f09fc5ad/` |
| 真实多服务/结账关联 | 已读到13个真实服务；人工结账关联47个span、19条日志、10个服务 | `services-early.json`、`manual-checkout-summary.json`及原始trace/log |
| 当前Collector失败/队列 | 通过；同实例实收=发送=入库70278，队列排空为0 | `conservation.json`、`drain-00.json`及连续内部指标 |
| 至少30分钟稳定性 | 通过；30.90分钟、58轮、1450条资源记录，无重启/OOM/容器失败事件 | `observation/validation-summary.json`及原始记录 |
| 旧数据保持 | 通过；UUID、138875条文档、mapping及5条原文哈希不变 | `legacy-before.json`、`legacy-after.json` |

材料目录为忽略的 `artifacts/m0-log-fix/`。单元测试不访问网络；真实日志回归通过runbook中的独立命令显式执行。阶段提交只含配置、代码、测试和脱敏文档。

真实冲突两端均已验证：新索引Cart日志仍保留 `http` 原值，Frontend仍保留 `http.request.method` 等点号键，不通过删除冲突属性解决拒收。Grafana的 `webstore-logs` 已读取新前缀，health返回 `Index OK. Time field name OK.`。

34项单元测试已通过，独立审查发现的模板额外pipeline/组件模板及已有index写入设置校验缺口已补回归修复。真实异构批次还覆盖scope属性、两个方向的类型变化、数组/布尔/null、40000字符长错误及结构化body，核对全部 `_source` 原值。核心查询单独验证1纳秒精确窗口和body全文检索。

Payment真实故障窗口在新索引得到4条错误日志、8个支付ERROR span，本次13/13接口检查通过；关闭后再次结账完成，滚动错误率回到0，恢复CLI退出0、11/11通过（本窗2条trace详情）。初始健康窗错误序列尚不存在时为13/14、需复核，没有补造零。计数随实际trace数量变化，最多读取5条，不为了凑数补样本。

最终持续观察窗口为 **14:19:40–14:50:37 UTC**。五用户负载下，25个容器ID保持、健康状态正常；Docker stats内存合计最小3401.25 MiB、中位数3757.15 MiB、峰值3887.53 MiB（约3.80 GiB，不含VM/桌面程序开销）。保留的连续Collector stderr无 `mapper_parsing_exception`、`Dropping data` 或 `Exporting failed`。其他未部署采集源的scrape告警没有被隐藏，不等于OpenSearch日志拒收。

观察结束后先停止业务日志来源，保留Collector、OpenSearch及内部指标出口，等待队列排空。固定当前Collector实例后，gRPC/HTTP累计接收合计 **70278**，OpenSearch导出成功计数 **70278**，新索引实际文档数 **70278**；其中独立验证记录11条，真实运行来源70267条。接收失败与拒收计数存在且保持0；`send_failed`/`enqueue_failed`本次没有series，**没有补成数值0**，采用实际收发存数量守恒、排空队列与连续错误输出共同核对本次未丢失已接收日志。

收尾阶段的正常停止事件在持续观察窗口之外，不能混算为运行失败。所有开关已恢复原定义；归档后25个实验容器均已停止，保留镜像、原OpenSearch容器与数据，不做清理/重建。代码、配置与测试沿用 `stage/m0-environment-validation` 提交，提交ID以Git历史为准，原始材料保持本机。

## 保留的语义与限制

- 按 `resource.service.name.keyword`、时间、正文、traceId/spanId检索；任意自由属性子字段及未声明的resource/scope字段不支持搜索/排序/聚合，但原文仍可读取。后续如需更多查询字段，需在对应阶段设计中明确白名单。
- 固定SS4O将结构化body转为JSON字符串；`@timestamp`保留事件纳秒，`observedTimestamp`由exporter生成，不等于原始OTLP observedTimeUnixNano。
- 受控验证服务与真实业务服务分开计数；一次批次全部入库和观察窗内无永久错误，只能证明本版本/本负载的修复结果，不能扩大为所有来源的无限期无损承诺。
- 本次实际覆盖的13个服务为ad、cart、checkout、currency、email、frontend、frontend-proxy、load-generator、payment、product-catalog、quote、recommendation、shipping。没有把全部25容器都当作已配置OTLP日志源；本次没有给其他容器新增stdout/filelog采集。
- 原OpenSearch数据位于当前容器文件系统；保留旧容器，不通过重建或清空解决问题。原M0 artifacts不覆盖，原缺失区间不补造数据。

复现、初始化顺序和停止方式见[操作说明](../runbooks/m0-local-lab.md)。本阶段成果仍待用户审查。

## 实施取舍

- 保持SS4O及原核心查询路径，使用新索引修复自由属性冲突；代价是任意扩展属性不直接建索引，原始值通过 `_source` 读取。
- 初始化命令仅管理本项目前缀；不兼容对象直接拒绝，不覆盖用户设置。它验证模板、映射、分片/副本、写入pipeline、blocks和aliases，服务端默认值经过明确规范化；没有通用迁移或删除能力。
- Collector自日志留在Docker stderr，解除同出口回灌；因此OpenSearch中的业务日志覆盖不包含Collector自身诊断日志，观察时另行保存stderr和内部指标。
- 本次沿用25个组件及原镜像ID，未新增日志存储平台、Python依赖或业务模块。原始验证材料保持本机，不随公开仓库发布。
