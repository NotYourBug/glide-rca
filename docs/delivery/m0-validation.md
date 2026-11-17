# M0 实测与交付记录

日期：2026-11-16。阶段：实验环境与接口验证。状态更新：2026-11-17，**实现和本阶段限定验收完成，用户交付审查通过**。

审查记录：用户在M0完成情况讨论后明确“ok，那就M1走起”，记录为含日志修复的M0交付通过，并启动M1设计；不代表M1方案已确认。以下实测保留原观察日期和限定范围。

依据：[已确认的 M0 设计](../design/stages/m0-design.md)。操作步骤见 [本机实验说明](../runbooks/m0-local-lab.md)。本报告只记录 M0；不表示诊断平台或论文效果已经实现。

## 1. 已交付内容及环境

- 本机覆盖配置：固定 Demo 3.1.0、三个回环端口、Grafana 离线插件及经过校准的资源上限。
- `scripts/m0_probe.py`：Python 标准库只读探针，保存三类接口样本与 `m0-probe.v1` 报告。
- `tests/test_m0_probe.py`：配置、边界、失败、身份检查和覆盖保护的单元测试。
- runbook、实现计划与本报告；原始样本、完整版本清单和操作真值留在忽略的 `artifacts/m0/`。

实测环境为 Windows + Docker Desktop Linux/WSL2，宿主物理内存约16 GB、Docker 引擎可用约8 GB；Python 3.11.9、Docker 29.9.2、Compose 5.6.1。固定源提交为 `dedc0178918e260823323b8d95005a8cb924b007`，minimal 组合25个容器。业务镜像使用 `3.1.0-*`，Collector 为0.159.0，Grafana 为13.1.0，OpenSearch 插件为2.34.7。

本机仅发布 `127.0.0.1:8080`、`:9090`、`:9200`。业务服务、Collector、Jaeger 和 Grafana 通过容器网络通信；不向局域网或公网开放。本阶段未修改上游业务代码。

## 2. 验收结果

| 检查项 | 当前结论 | 证据与说明 |
| --- | --- | --- |
| 版本与端口 | 通过 | 固定源提交、25个镜像实际 digest 已归档；最终渲染配置仅3个回环发布端口 |
| 正常请求 | 通过 | Demo UI 真实结账成功；完整 trace 中存在 frontend→checkout→payment |
| 指标 | 已取得 | 请求率、错误率、P95、Ad CPU/内存；健康初始错误序列为空，故障时获得实际值 |
| 日志 | 通过 | Payment 按时间/服务检索成功；保存 mapping、正文和 traceId/spanId |
| 调用链 | 通过 | v3 services、窗口搜索及按 id 读取成功；保存跨服务父子边 |
| Payment 故障与恢复 | 通过 | 最终配置复测有7条故障日志、10个Payment ERROR spans、非零错误率；关闭后新结账完成，滚动错误率回到0 |
| Ad CPU 故障与恢复 | 通过 | 短窗口 CPU 时间速率与 Docker stats 均证实上升/回落；见下文数值 |
| 连续稳定性 | 通过 | 最终配置10:00:40–10:31:30 UTC；58轮、1450条记录；25容器ID不变、无重启/OOM/die事件，健康检查均正常 |
| 探针单元测试 | 通过 | 最后复核16/16；真实CLI退出0、14/14接口检查；日志见本机归档 |
| Git 阶段成果 | 提交清单已核对 | 17个项目文件，阶段分支 `stage/m0-environment-validation`；原始遥测和本机配置不公开 |

最终资源样本跨度30.78分钟，Docker stats 内存合计最小3403.64 MiB、中位数3512.57 MiB、峰值3600.93 MiB（约3.52 GiB）；不包含 Docker VM、桌面程序等额外开销。源、镜像、工具和文件哈希在 `post-calibration/manifest.json`，最终渲染配置 SHA-256 为 `c2702e3da1e5becfe98df2833f8046513d90a1fbece988be747480a3334ab2da`。

## 3. 真实接口契约与限制

| 来源 | 接口与实测字段 | 后续接入注意事项 |
| --- | --- | --- |
| Prometheus | `/api/v1/query_range`，10秒查询步长；`traces_span_metrics_calls_total`、`traces_span_metrics_duration_milliseconds_bucket` | RED 仅取 `service_name="payment"`、`span_kind="SPAN_KIND_SERVER"`；rate 用5分钟窗。请求率为次/秒、错误率为比值、P95为毫秒 |
| 容器指标 | `container_cpu_utilization_ratio`、`container_memory_usage_total_bytes`；身份为 `container_name="ad"` | CPU 的上游单位为 `1`，描述为百分比，保留原始数值，不能看到 `_ratio` 就当作0–1分数；内存为字节。不能用宿主整体指标替代 Ad 指标 |
| OpenSearch | `POST /otel-logs-*/_search`、`GET /otel-logs-*/_mapping` | 查询字段 `resource.service.name.keyword`；`_source.resource` 实际为字面键 `service.name`。保存 `@timestamp`、`observedTimestamp`、`body`、`severity`、`traceId`、`spanId` |
| Jaeger | `/jaeger/ui/api/v3/services`、`/traces?query.serviceName=...`、`/traces/{id}` | 本部署返回单个 JSON，spans 位于 `result.resourceSpans[].scopeSpans[].spans[]`；服务身份在 resource attributes 的 `service.name`，纳秒时间保持原始字符串精度 |

CPU 单位依据同版本 [dockerstatsreceiver 元数据](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.159.0/receiver/dockerstatsreceiver/metadata.yaml)。现场 Prometheus metadata 查询为空，不能据此补造 metadata；本机 Docker stats 百分比作为另一份独立资源记录。

初始健康窗 `healthy-live` 自动检查13/14通过：唯一需复核项为尚未产生的错误序列，没有填零。`payment-fault` 为14/14通过，5条详情均包含 Payment 错误状态 `code=2`。日志正文与 span 错误信息一致。采样数量小，仅证明接入与故障可观察性，不能用于推算诊断准确率。

最终配置使用同一正常基线（10:00 UTC后），随后按顺序执行 Payment 开关10:11:14–10:13:10、恢复确认10:20 UTC、Ad 开关10:20:48–10:22:57。两个故障不重叠；Payment 的5分钟滚动窗口先恢复0，再开启 Ad。最终 Payment 故障样本、恢复样本、Ad 故障样本均为14/14接口检查通过，操作真值单独保存。

探针限制：窗口最多15分钟；HTTP I/O超时10秒、响应10 MiB；日志最多200条、trace 搜索深度100、最多读取5条详情；不重试、不跟随重定向、不使用系统代理。退出码0只表示本次接口检查通过。无结果、非有限值、截断、错误路由或结构缺失必须看报告；不把样本当作完整、不可变诊断快照。OpenSearch 的 `timed_out` 或失败分片标为需复核，即使HTTP为200；总数为下界时保守标截断。

### Ad 资源校准

原始 `container_cpu_utilization_ratio` 在关闭故障后缓慢下降，不能作为瞬时恢复判据。为交叉核对，另对同一 Ad 容器的累计 CPU 纳秒计数器执行：

```promql
rate(container_cpu_usage_nanoseconds_total{container_name="ad"}[1m]) / 1e9 * 100
```

该值表示 CPU 时间相对墙钟时间的百分比，多核合计可以超过100。固定查询步长10秒，原始响应为 `final-observation/ad-cpu-counter-rate.json`；以下是三个不重叠 UTC 子窗口的实际中位数：

| 场景 | UTC 窗口 | 点数 | CPU 中位数 |
| --- | --- | --- | --- |
| 基线 | 09:41:00–09:43:00 | 13 | 1.17% |
| 故障 | 09:45:00–09:47:30 | 16 | 401.01% |
| 恢复 | 09:50:00–09:55:00 | 31 | 1.21% |

Docker stats 也观察到故障时约393%、关闭后约0.22%的 Ad CPU；两者采样时刻和窗口不同，不能要求逐点相等。本阶段保留原始 gauge 和补充 counter-rate，不在探针中悄悄替换指标含义。

最终配置的再次复测仍得到同一结论：基线中位数1.12%（13点）、故障400.98%（7点）、恢复1.11%（13点），原始响应及摘要在 `post-calibration/ad-cpu-counter-rate.json`、`ad-cpu-summary.json`。恢复 probe 为12/12通过，此次窗口只有3条trace详情，不能为了凑数补造5条。

## 4. 初始化修正和已知边界

1. HTTPS 源码获取连接重置，改用 SSH 获取同一固定提交；镜像加速地址 EOF 时经官方 registry-1 获取同版本后添加原名本地 tag，没有改全局 Docker 配置或升级到 latest。
2. 只覆盖 IMAGE_VERSION 会留下上游已展开的旧资源版本；显式覆盖 `OTEL_RESOURCE_ATTRIBUTES` 后重建业务容器。初始化期间的3.0.0 series仍可能保留，不能混入最终观察窗。
3. Grafana 首次因插件下载域名解析失败循环启动；改用官方固定归档、核验 SHA-256、只读挂载并关闭自动更新，内存上限384M。
4. checkout 初始20M上限下曾到99.41%，随后出现一次重启。Docker 后续状态未保留确切退出原因，不能据此断言 OOM。product-catalog 随后到20M上限，09:57:49 UTC 的 Docker 事件明确记录 oom 和退出137。两个 Go 组件均覆盖为128M、`GOMEMLIMIT=96MiB`；之前的观察窗作失败记录保留，最终窗口从10:00:40 UTC重新计时。
5. 上游 minimal 组合的 Collector 命令仍加载 full 配置，因此对未部署 Kafka 产生采集告警；PostgreSQL 健康检查也产生角色相关日志噪声。本阶段未改造上游遥测管线。
6. 原验证曾报告 OpenSearch `mapper_parsing_exception`：`attributes.http` 已映射为 text，又收到 `http.request.method` 对象路径，部分日志被永久丢弃，旧窗口不能用于完整日志数量推算。**后续M0缺陷迭代已修复这一映射拒收**：新索引实收/发送/落库70278条一致，13个真实服务和异构回归通过，详见[修复交付报告](m0-log-ingestion-fix.md)。原错误摘要仍留在 `final-observation/collector-export-errors.log`，历史缺失没有补造；这也不表示所有25容器都新增了日志采集。
7. Jaeger 使用内存存储，停止或重启会丢失其在线 trace；所需样本必须先归档。原始日志可能包含虚构订单和主机属性，未直接提交公开仓库。

## 5. 本机证据与复核

`artifacts/m0/` 为本机材料，不随公开仓库上传：

| 路径 | 用途 |
| --- | --- |
| `setup/`、`initial-live/` | 初始化版本、首次接口问题；不用于声称最终验收通过 |
| `healthy-live/` | 正常基线、结账链路及初始错误序列缺失 |
| `payment-fault/` | Payment 故障窗口的指标、日志、完整 trace |
| `payment-recovered/` | 关闭后的早期恢复样本，5分钟 rate 仍受此前故障影响 |
| `ad-fault/` | Ad CPU 故障窗口样本 |
| `ad-fault-full/`、`ad-recovery-early/` | 完整开关窗口及早期恢复；另有 CPU counter-rate 校准文件 |
| `payment-retest/` | checkout 资源校准后的第二次 Payment 故障样本 |
| `final-observation/experiment.json` | 独立操作真值；未来不能直接作为诊断输入 |
| `final-observation/resources.csv` | 校准前观察，保留早期重启事实 |
| `stable-observation/` | checkout 校准后仍出现 product-catalog OOM 的失败观察；保留事件证据 |
| `post-calibration/` | 两个 Go 组件均校准后的最终观察、场景复测和版本清单 |

运行 `python -m unittest discover -s tests -v`；现场集成复核使用 runbook 中的探针命令，替换成真实UTC窗口和新的输出目录。报告中的有限点计数与原始样本可逐项核对；公开报告不包含完整主机身份、会话和原始日志。

归档包含 `post-calibration/validation-summary.json`、58轮资源记录、初末容器检查、完整镜像清单、最终CLI与单元测试日志、各场景样本，以及独立实验真值。所有故障开关已恢复上游初始定义；归档后25个实验容器已停止，保留镜像和卷，避免持续占用本机资源。按 runbook `start` 可重启，旧 Jaeger 在线数据已经丢失，复核使用本机归档。

本报告随阶段提交提供，提交ID以 Git 历史为准。2026-11-17用户审查通过；当前[M1设计](../design/stages/m1-design.md)待确认，未开始M1开发。

## 6. 实施取舍与代价

| 取舍 | 原因与代价 |
| --- | --- |
| 复用当前 checkout 的独立阶段分支 | 空仓库无须复制已有设计资料；隔离依赖 Git 分支，未另建 worktree |
| 相同固定版本使用 SSH/官方 registry-1，Grafana 插件固定归档 | 绕过现场网络故障并核对提交/digest/SHA-256；复现仍需要官方源可达，插件升级要另行校验 |
| 显式覆盖资源版本及三个组件的内存预算 | 修正实际版本和运行失败；本机预算与上游默认不同，资源变化后重新观察 |
| 保留原始 CPU gauge 并补充 counter-rate | 核实可观察的 CPU 恢复；两种量含义和采样窗不同，后续不能混用 |
| 披露上游日志丢弃与采集噪声 | 本阶段核实 Payment 样本可查询性；全服务日志完整性仍有明确限制 |
| 以阶段分支交付，不创建 main 或合并 | 远端为空，尚无 PR 基线；按用户流程审查当前阶段提交 |
| 原始材料本机归档后停止 Demo | 释放本机内存；Jaeger 在线内存数据会丢失，复核应使用归档或重新运行 |

没有另行延后审查指出的探针问题：容器归属、trace服务身份、OpenSearch部分结果均已修复并有回归。业务应用、LLM、RCAEval 回放和全服务日志管线改造按已确认范围留待其阶段设计，不表示已经开发或验证。
