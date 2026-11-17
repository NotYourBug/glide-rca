# M0 缺陷迭代设计：日志映射冲突修复

日期：2026-11-16。状态更新：2026-11-17，**用户授权的M0内修复及限定验收已完成，随M0获用户交付审查通过**。

审查记录：用户在M0完成情况讨论后明确“ok，那就M1走起”。下文保留当时修复范围；当前M1仅进入设计，方案须另行确认。

本次不启动M1，不改变全局架构。沿用已确认M0的实验栈、版本、三类接入边界与阶段审查流程；本文件记录本轮新增的日志存储约束及验证范围。用户本轮明确要求修复，覆盖原交付中暂留上游缺陷的取舍，无需再次申请相同修复授权。

## 1. 阶段概述与目标

这是M0交付后的缺陷迭代。修复 Collector→OpenSearch 的确定性映射拒收，交付可重复初始化的索引模板、配置覆盖、真实端到端回归及更新的交付记录。

已核实：OpenSearch3.7.0旧索引中，Cart启动日志带 `attributes.http="8080"`，该字段被推断为text；Frontend请求日志带 `http.request.method` 等点号属性。共享动态映射要求同一路径同时为字符串与对象，产生 `mapper_parsing_exception`。Collector将其视为永久错误并丢弃。仅修改重试、关闭错误显示或核对Payment日志不能解决此问题。

核心目标：在固定Demo负载和受控异构日志测试中，消除这类拒收；保留原始属性、正文、事件时间及trace关联；如实界定覆盖范围，不能承诺所有未来应用日志绝对无损。

## 2. 工作范围

需要完成：

- 为 `otel-logs-v2-*` 提供预设模板，核心检索字段显式映射；自由属性只存储在完整 `_source`，不逐键动态建索引。
- 通过小型标准库初始化命令创建本项目新模板及当前日期新索引，先检查已有资源，拒绝覆盖不兼容模板/索引。
- Collector通过最后一层本机覆盖写入新前缀；其内部日志保留Docker标准错误输出，解除自日志经同一失败导出器回灌的放大环路。内部指标继续导出至Prometheus。
- 探针和Grafana默认读取新前缀，保持服务、时间、正文和trace检索契约。旧配置仍可显式查询旧材料。
- 发送独立验证服务的OTLP日志，覆盖纯量/点号/嵌套键、类型变化、数组/null和长错误属性；逐条核对落库值。再核实真实多服务日志及结账关联。
- 持续观察固定负载下的导出结果、队列、错误日志、资源和重启；复跑原有单元与三模态接口检查。

不做：不改上游业务SDK，不部署第二套日志系统，不升级镜像，不删除/重建旧索引，不自动reindex历史数据，不开发M1适配器、业务平台、LLM或故障调度器。已丢弃的历史记录不伪称补回。

## 3. 与上一阶段对接说明

依赖M0固定Demo3.1.0源提交及现有25容器配置、Python只读探针、已保存的错误样本、阶段分支和runbook。保留128M的两个Go服务预算、Grafana384M与固定插件、本机三个回环发布端口。

数据契约：

- SS4O顶层字段及 `_source.resource["service.name"]` 不改名；服务查询仍为 `resource.service.name.keyword`。
- `@timestamp`、`observedTimestamp` 使用date_nanos；正文为text，traceId/spanId为keyword。
- `attributes` 与 `instrumentationScope.attributes` 使用 `object, enabled:false`；它们的原始JSON值完整保留，无法按任意属性子字段搜索、排序或聚合。M0按服务/时间/正文/trace读取不受影响；正式接入如需属性搜索，应在对应设计中选择少量明确字段建立索引。
- resource/scope中的非核心字段通过 `_source` 保留，关闭其自由动态映射；仅声明的核心字段可检索。保留service.name的text+keyword双字段兼容现有查询。
- 新增探针配置 `log_index_pattern`，缺省沿用旧 `otel-logs-*`，公开示例和本机当前配置设为 `otel-logs-v2-*`；报告仍使用 `m0-probe.v1`，增加索引定位属于兼容配置扩展。
- Grafana保留datasource UID `webstore-logs`，替换查询前缀及配置版本。

旧索引作为历史材料保留，新旧写入分开。切换前后核对旧索引UUID、文档数、mapping及抽样原文哈希；原M0样本不覆盖，新增材料放在忽略的 `artifacts/m0-log-fix/`。

## 4. 关键细节与注意事项

1. 已有索引不能直接更改冲突字段类型。初始化必须在Collector恢复写入之前执行；初始化失败就停止启动流程。只创建本项目新资源，不更新已有不兼容对象。
2. 单节点新索引使用1个分片、0副本，避免不必要副本等待；模板按新前缀匹配，后续日期沿用。
3. 选择不解析自由属性，保证点号、类型变化及很长的stacktrace不会形成字段类型或keyword term长度冲突；代价是自由属性不被索引，不能把“保留”解释成“全部可检索”。
4. 原始body通过固定版SS4O导出器的AsString转换；验证包含结构化body，明确其序列化后的表示。SS4O的observedTimestamp由exporter的当前时间重写，并非原始observedTimeUnixNano；事件时间单独保持，测试不把这两种时间混为一谈。不可在测试里复制一套生产转换实现来冒充回归。
5. 初始化命令与只读探针分离；探针继续只读。写入仅限loopback、新模板/新索引和独立验证记录；不加入删除或任意管理API。
6. 验证服务名与真实业务服务分开，不能用人工日志冒充业务覆盖。至少现场核实Cart、Frontend、Payment及另一实际发日志业务服务。
7. HTTP200不代表整批落库：核对OTLP partialSuccess、OpenSearch分片/超时、唯一记录数量及字段值。验证数据采用唯一traceId/spanId，避免与历史样本混淆。
8. Collector指标按当前实例及观察窗增量解释。发送失败次数不是永久丢失条数；无series记缺失，不补造0。结合明确的接收/发送增长、队列、永久错误输出及实际记录核对。
9. 只检查本项目容器，保留Docker stderr诊断；不静默过滤错误。新完整配置先用固定Collector镜像验证，再启动；持续运行至少30分钟，记录是否有重启/OOM及映射/发送失败。
10. 编写/编辑结合Ponytail；复用现有标准库HTTP边界，增加有意义的配置/初始化边界测试与真实集成回归，不引入第三方依赖或通用部署框架。

## 5. 文件目录组织方案

```text
infra/otel-demo/
  compose.local.yaml                      # 修改：挂载最后一层日志覆盖与Grafana datasource
  otelcol.logs.yaml                        # 新增：新索引前缀、自日志出口配置
  opensearch-logs-v2-template.json          # 新增：固定索引/字段契约
  grafana-opensearch.yaml                  # 新增：沿用UID的新查询前缀
configs/m0.example.json                    # 修改：新索引定位
scripts/m0_probe.py                        # 修改：安全配置项及查询前缀
scripts/m0_logs_setup.py                   # 新增：有界、非破坏初始化
tests/test_m0_probe.py                     # 修改：配置边界及旧配置兼容
tests/test_m0_logs_setup.py                # 新增：初始化保护回归
tests/integration/test_m0_log_ingestion.py  # 新增：显式运行的真实OTLP→OpenSearch回归
docs/design/stages/m0-log-ingestion-fix.md  # 本文件
docs/runbooks/m0-local-lab.md               # 修改：启动前初始化、验证与旧数据说明
docs/delivery/m0-log-ingestion-fix.md        # 新增：实际结果/限制/证据
docs/delivery/m0-validation.md              # 修改：原问题状态与修复链接
docs/design/README.md、README.md            # 修改：修复入口与阶段状态
artifacts/m0-log-fix/                       # 忽略：现场样本、旧数据校验、观测和真值
```

## 6. 文件职责说明

| 文件 | 职责与对外能力 | 依赖 |
| --- | --- | --- |
| compose.local.yaml | 挂载两份项目配置，Collector命令最后加载日志覆盖；端口/业务预算保持原值 | 固定上游Compose和本机配置 |
| otelcol.logs.yaml | 仅覆盖日志索引出口及自日志回灌；内部指标继续保留 | 固定Collector0.159.0配置合并 |
| opensearch-logs-v2-template.json | 声明模板、新索引匹配、核心查询字段和原始属性存储策略 | OpenSearch3.7.0 |
| grafana-opensearch.yaml | 使用原UID展示新索引，避免跨旧坏映射查询 | 固定插件及新索引契约 |
| m0.example.json | 声明新读索引；本机副本更新但不公开 | 只读探针 |
| m0_probe.py | 校验索引定位并执行原有有界只读检查 | 标准库、现有HTTP保护 |
| m0_logs_setup.py | 暴露标准库模块命令；比较已存在模板/索引，创建缺失资源，发现不兼容则退出 | 模板文件、受限loopback客户端 |
| 两份单元测试 | 验证索引路径边界、兼容性和初始化不覆盖旧数据 | mock和现有unittest |
| 集成测试 | 显式运行；向现成OTLP入口发独立记录并验证全部原始值及核心检索 | 已初始化且运行的本机实验栈 |
| runbook | 说明初始化顺序、命令、查询语义、历史边界与停止方式 | 实际通过的命令 |
| 新交付报告及原报告/索引 | 区分旧失败、修复结果、覆盖范围及审查状态 | 本机证据和版本清单 |
| artifacts/m0-log-fix | 原始样本和验证材料，全部保持不跟踪 | 实际查询、测试和观察 |

## 7. 验收与架构边界

- 旧数据身份、数量和样本哈希保持；没有删除旧索引/卷。
- 异构验证批次全部落库，原始自由属性/正文/时间/trace关联符合固定出口契约；重复初始化不覆盖资源，不兼容映射会被阻止。
- 真实冲突两端服务及其他业务日志可在新索引按服务/时间读取，真实结账成功并关联trace。
- 至少30分钟固定低并发观察；映射永久拒收不再发生，当前实例接收/发送有增长，失败/队列记录及缺失项如实报告，容器稳定。
- 原有单元与三类探针验证通过；文档列明属性检索限制和历史缺失不可补回，提交M0修复成果供审查。

全局架构没有改变：仍为相同Demo→Collector→Prometheus/OpenSearch/Jaeger→只读验证路径；新增的是M0存储schema及初始化约束，不提前定义M1业务模型。

依据：[固定版SS4O编码器](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.159.0/exporter/opensearchexporter/encoder.go)、[OpenSearch enabled参数](https://docs.opensearch.org/latest/mappings/mapping-parameters/enabled/)、[索引模板API](https://docs.opensearch.org/latest/api-reference/index-apis/create-index-template/)、[已有字段类型不可直接修改](https://docs.opensearch.org/latest/api-reference/index-apis/put-mapping/)。

实施记录：初始化的服务端默认值差异及额外pipeline/写入设置风险均先复现、补回归再修复；34项单元、11条真实异构记录与核心检索通过。固定负载持续30.90分钟，25容器无重启/OOM；停止来源并排空后实收/发送/落库均70278，旧138875条原记录身份、映射及抽样哈希不变。详见[修复交付报告](../../delivery/m0-log-ingestion-fix.md)，本阶段仍等待用户审查。
