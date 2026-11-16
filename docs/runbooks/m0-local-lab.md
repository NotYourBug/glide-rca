# M0 本机实验操作说明

本文件随实际验证更新。环境或故障验证的最终结果以 [交付记录](../delivery/m0-validation.md) 为准；固定第三方源提交为 `dedc0178918e260823323b8d95005a8cb924b007`。

## 1. 环境和固定源码

在项目根目录使用 PowerShell。需要 Python 3.11+、Git、Docker Desktop Linux 引擎，以及 Compose ≥2.24.4。

```powershell
docker version
docker compose version
docker info --format '{{.OSType}} {{.MemTotal}}'
Get-PSDrive C
git clone --branch 3.1.0 --depth 1 https://github.com/open-telemetry/opentelemetry-demo.git .external/opentelemetry-demo
git -C .external/opentelemetry-demo rev-parse HEAD
Copy-Item infra/otel-demo/.env.override.example .external/opentelemetry-demo/.env.override
```

若 HTTPS 连接重置，可使用已验证 SSH 认证的 `git@github.com:open-telemetry/opentelemetry-demo.git` 获取同一版本，不切换 tag。

`.env.override.example` 同时显式设置资源中的 service.version。上游 `.env` 已经展开的派生变量不会因后续仅覆盖 IMAGE_VERSION 而重新插值。

### Grafana 固定插件

本机覆盖使用官方 OpenSearch 插件 2.34.7 的只读挂载，避免启动时依赖受限的下载域名。启动前从官方 GitHub 取得归档并核验发布的 SHA-256：

```powershell
curl.exe -fLsS --connect-timeout 10 --max-time 60 'https://github.com/grafana/opensearch-datasource/releases/download/v2.34.7/grafana-opensearch-datasource-2.34.7.linux_amd64.zip' -o .external/grafana-opensearch-2.34.7.zip
$m0PluginHash = (Get-FileHash .external/grafana-opensearch-2.34.7.zip -Algorithm SHA256).Hash.ToLower()
if ($m0PluginHash -ne 'ec04c982a61f4dbf82b9884b5e533d67db9a562d1a134859b69da34ed50f727e') { throw 'Plugin SHA-256 mismatch' }
Expand-Archive .external/grafana-opensearch-2.34.7.zip -DestinationPath .external/grafana-plugins
```

插件来自 [官方发布](https://github.com/grafana/opensearch-datasource/releases/tag/v2.34.7)，解压后应有 `.external/grafana-plugins/grafana-opensearch-datasource/plugin.json`。已有目录先核对版本，不覆盖来源不明的内容。本机覆盖关闭插件自动安装/更新，并将 Grafana 内存上限校准为384M。checkout 在20M上限下曾接近满额并重启，原退出原因未被直接证实；product-catalog 随后在同样上限发生已确认的OOM。两个 Go 组件均覆盖为128M及 `GOMEMLIMIT=96MiB`，之后重新做稳定观察。业务代码保持固定上游版本；日志修复通过本项目覆盖层表达。

## 2. 统一 Compose 参数

以下数组只用于当前 PowerShell 会话，所有命令保持同一文件顺序和项目名。不要在上游目录中直接运行裸 `docker compose up`。

```powershell
$m0Compose = @(
  '--project-name', 'glide-m0',
  '--project-directory', '.external/opentelemetry-demo',
  '--env-file', '.external/opentelemetry-demo/.env',
  '--env-file', '.external/opentelemetry-demo/.env.override',
  '-f', '.external/opentelemetry-demo/compose.yaml',
  '-f', '.external/opentelemetry-demo/compose.observability.yaml',
  '-f', '.external/opentelemetry-demo/compose.extras.yaml',
  '-f', 'infra/otel-demo/compose.local.yaml'
)
New-Item -ItemType Directory -Path artifacts/m0/setup -Force | Out-Null
docker compose @m0Compose config --format json --output artifacts/m0/setup/compose.resolved.json
docker compose @m0Compose config --images
```

核对渲染结果：25 个 minimal 组件；业务镜像为 `3.1.0-*`；没有 Kafka/accounting/fraud-detection；只有 frontend-proxy 8080、Prometheus 9090、OpenSearch 9200 发布到 `127.0.0.1`。容器内服务端口保持上游配置。上游设置固定 container_name，如同名容器已存在需先协调，不能删除其他实验。

## 3. 拉取和启动

```powershell
docker compose @m0Compose --progress plain pull
# 首次创建本机配置；已有配置先核对log_index_pattern为otel-logs-v2-*，不覆盖个人设置。
if (!(Test-Path configs/m0.local.json)) { Copy-Item configs/m0.example.json configs/m0.local.json }
docker compose @m0Compose up --no-build --pull never --wait --wait-timeout 120 -d opensearch
python -m scripts.m0_logs_setup --config configs/m0.local.json
if ($LASTEXITCODE -ne 0) { throw 'Log schema initialization failed; keep Collector stopped' }
docker compose @m0Compose run --rm --no-deps otel-collector validate --config=/etc/otelcol-config.yml --config=/etc/otelcol-config-full.yml --config=/etc/otelcol-config-observability.yml --config=/etc/otelcol-config-extras.yml --config=/etc/otelcol-glide-logs.yml --feature-gates=service.profilesSupport
if ($LASTEXITCODE -ne 0) { throw 'Collector configuration is invalid' }
docker compose @m0Compose up --no-build --pull never -d
docker compose @m0Compose ps
```

如果拉取出现 EOF/超时，先保留错误并检查 registry 网络；`--ignore-pull-failures` 仅帮助其他镜像继续下载，不代表拉取完整。Docker Hub 加速源不可用时可尝试官方 registry-1 地址的**同一镜像版本**，成功后记录 digest，再添加配置原名的本地 tag。不能仅因启动失败就改成 latest。

本机曾用以下方式绕过失效加速地址，未修改 Docker 全局配置：

```powershell
docker pull registry-1.docker.io/library/postgres:18.4
docker tag registry-1.docker.io/library/postgres:18.4 postgres:18.4
docker pull registry-1.docker.io/grafana/grafana:13.1.0
docker tag registry-1.docker.io/grafana/grafana:13.1.0 grafana/grafana:13.1.0
```

本机入口：商店 `http://127.0.0.1:8080/`，Grafana `/grafana/`，Jaeger `/jaeger/ui/`，负载 `/loadgen/`，故障配置 `/feature/`；Prometheus 为 `http://127.0.0.1:9090/`，OpenSearch 为 `http://127.0.0.1:9200/`。运行结论与实际可访问情况见交付记录。

### 日志初始化与旧数据

初始化命令只创建本项目模板 `glide-m0-logs-v2` 和UTC当天的 `otel-logs-v2-YYYY-MM-DD` 索引。已有兼容资源直接复用；不兼容则失败，不能删除旧索引来绕过。必须先初始化，再允许Collector写入。运行中的旧配置做升级时先停止Collector，再执行上述初始化和 `up`；仅修改模板不会修复已经生成的旧索引。

日志覆盖层最后加载 `otelcol.logs.yaml`，写入新前缀，并保留Collector自日志到Docker stderr；自日志不再经同一OpenSearch导出器回灌。Collector内部指标仍发送到Prometheus。Grafana沿用 `webstore-logs` UID，默认读取新前缀，避免与旧映射混查。

核心服务/时间/正文/trace字段建立索引；`attributes` 和 `instrumentationScope.attributes` 原样保存在 `_source`，不按自由子字段建立索引，因此不支持任意属性子字段搜索、排序或聚合。未声明的resource/scope字段也保留原文，不能据此假定它们可检索。SS4O的 `observedTimestamp` 是导出时刻；`@timestamp` 是事件时刻，均按date_nanos建索引。历史永久丢弃的日志无法补回，只能保留旧缺失区间，并重新运行请求取新样本。

用现有负载生成器低并发产生请求，人工完成一次真实结账。先正常观察至少10分钟；用 Docker stats/inspect 记录资源、重启次数与 OOM，累计稳定观察至少30分钟。不需要额外创建流量服务。

## 4. 核对数据和运行探针

先确认 Prometheus 中实际 metric 名称和单位、OpenSearch mapping 和服务字段、Jaeger v3 返回结构。`configs/m0.example.json` 是需要与现场核对的查询示例；采集失败/空结果不能直接解释为服务正常。

```powershell
python scripts/m0_probe.py --config configs/m0.local.json --start '2026-11-16T09:00:00Z' --end '2026-11-16T09:05:00Z' --output artifacts/m0/example-run
```

把时间换成真实 UTC 事件窗口（最大15分钟），输出目录必须不存在。探针读取5项指标、日志 mapping/search、Jaeger services/search，以及最多5条 trace 详情；单次响应限制10 MiB、超时10秒，无重试，不跟随重定向、不使用环境代理。

现场核实的容器指标为 `container_cpu_utilization_ratio` 和 `container_memory_usage_total_bytes`，身份标签为 `container_name="ad"`；前者保留后端原始单位，不擅自解释为核数。RED 使用服务端 spans；实际 RED 相邻导出点约60秒，因此示例 rate 采用5分钟窗口，不能用不足两点的1分钟窗口判断无流量。容器采集与 RED 导出周期不同。OpenSearch `_source.resource` 的服务属性是字面键 `service.name`，mapping/search 路径仍使用 `resource.service.name.keyword`。新配置 `log_index_pattern=otel-logs-v2-*`；缺省沿用旧 `otel-logs-*` 仅为旧配置兼容，当前验收使用新前缀。历史字段冲突及修复验证见交付记录；单服务接口可读不能证明全量日志完整。

退出码：0=本次自动检查通过；1=检查失败或需要人工复核；2=参数、配置或输出路径错误。trace 搜索合法无结果与错误路由分开记录；已截断的样本不能用于准确率或尾延迟估计。OpenSearch 超时或分片失败标为 `needs_review/partial_results`，总量为下界也标截断。没有错误序列时，健康窗口的 error_rate 可能无值，保留此事实，不补造零。

报告 `probe-report.json` 和 `samples/` 都保留在忽略的 artifacts 中。人工核对至少一个 trace 的 frontend→checkout→payment 父子关系；报告通过不等于整个 M0 完成。

真实日志回归在实验栈运行且初始化成功后显式执行，不随普通单元测试发网络请求：

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s tests/integration -v
```

集成测试写入独立服务 `glide-log-validation` 的唯一记录，验证混合属性、长错误字段和核心检索；原始材料写入忽略的 `artifacts/m0-log-fix/controlled-<traceId>/`。它验证入库契约，不能替代真实业务服务覆盖及Collector失败指标观察。

## 5. 故障和恢复

通过 `/feature/` UI 操作，每次保存初始开关状态，并在本机 experiment.json 记录生效时间、请求窗口与恢复结果。

1. 正常基线后，打开 `paymentFailure` 的 `100%`，提交结账并查询 Payment 错误日志、错误 spans 和错误率。
2. 恢复原开关，验证新的结账请求成功及错误率回落，保存恢复样本。
3. 打开 `adHighCpu` 的 `on`，保持低并发首页流量，记录明确归属 ad 的 CPU 基线和变化。
4. 恢复原开关，核实 CPU 回落及服务稳定性。两种故障不能重叠，未恢复不能标记实验结束。

本机原始 CPU gauge 回落较慢，恢复需结合 Docker stats 与 Prometheus 中 `rate(container_cpu_usage_nanoseconds_total{container_name="ad"}[1m]) / 1e9 * 100` 核对。该表达式为多核 CPU 时间百分比，可超过100；不要把原始 `_ratio` 后缀理解成0–1标准化值。实际基线/故障/恢复数字见交付记录。

原始故障配置和时间表作为独立实验真值，不交给未来诊断 worker。本阶段不提供自动注入脚本。

## 6. 停止、保留和复现

```powershell
docker compose @m0Compose stop
# 下次按第3节先启动OpenSearch、初始化校验，再up完整环境。
```

停止会使 Jaeger 内存数据丢失，先归档所需样本。OpenSearch当前数据在已有容器文件系统中，`stop` 保留它，删除或重建该容器可能丢失历史索引，不能为修复日志而重建OpenSearch。不要对其他 Compose 项目操作，不运行全局 prune，不用 `down -v` 清理数据。本项目 artifacts 与第三方目录均不进入公开提交。

交付时保存源提交、镜像 digest、Compose 配置 hash、接口样本、独立故障记录、资源观察及测试结果，填写 [M0 交付记录](../delivery/m0-validation.md)。
