# M0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按已确认方案交付可复核的 Demo 环境、三模态只读探针和 M0 验证记录。

**Architecture:** 复用固定 Demo 的 minimal 配置，仅用本机覆盖层调整版本与端口。一个 Python 标准库脚本完成有界查询和样本保存，测试使用 unittest；不创建业务应用骨架。

**Tech Stack:** Docker Compose、Python 3.11 标准库、unittest、Git。

**Spec:** `docs/design/stages/m0-design.md`（用户已确认并授权实施，无需重复申请计划确认）。

## Global Constraints

- 只实施 M0；保持 3.1.0 固定源版本，不追随 latest。
- 查询窗口 ≤15 分钟；HTTP timeout 10 秒；单响应 ≤10 MiB；无自动重试。
- loopback HTTP、JSON 配置、只读探针；原始样本/本机配置/论文不入公开 Git。
- 输出目录必须新建；保留失败、空结果、截断和结构异常。
- 结合 Ponytail 规划与编辑，用现成工具及标准库完成必要行为。

## Review Focus

- 重定向不能使请求离开 loopback；本机代理不能转发读取请求。
- 非法/无时区时间、NaN、参数结构和超大响应必须拒绝。
- 无 trace 与错误路由的 404 必须区分；span 时间保留整数精度。
- 目录已存在及中途失败不能覆盖旧结果或伪造成功。
- Docker 启动、镜像获取或资源条件失败时，明确交付未完成的验收项。

## Task 1: 固定实验环境

- [x] 检查并启动现有 Docker Desktop，核对 Linux 引擎、Compose、磁盘与已有工作负载。
- [x] 检查项目 remote，初始化阶段分支；固定上游源版本。
- [x] 创建版本覆盖和本机端口覆盖；渲染核查 minimal 组合与绑定地址，启动并记录镜像 digest。

## Task 2: 只读接口探针

**Files:** `scripts/m0_probe.py`、`tests/test_m0_probe.py`、`configs/m0.example.json`。

**Interfaces:** `validate_config(dict) -> dict`、`validate_window(str,str) -> tuple[datetime,datetime]`、`request_json(url, body=None) -> tuple[int,object]`、`run_probe(config,start,end,output) -> dict`、`main(argv=None) -> int`。

- [x] 先写失败测试：配置/时间/重定向/响应大小/HTTP错误/结果结构/覆盖保护。
- [x] 运行 unittest 确认测试因实现缺失失败。
- [x] 实现最小标准库脚本及真实接口检查，不添加通用抽象层。
- [x] 运行测试，基于实际响应校验 probe report 和时间精度。

## Task 3: 实验与交付

**Files:** `README.md`、`docs/runbooks/m0-local-lab.md`、`docs/delivery/m0-validation.md`；原始材料在忽略的 artifacts 中。

- [x] 正常流量和结账验证，记录基线与资源；完成 Payment/Ad 故障与恢复，累计稳定运行至少30分钟。
- [x] 记录 manifest、原始查询样本和分离的实验真值，写实际可重复操作说明。
- [x] 运行必要单元与集成检查，核对公开提交内容，提交阶段成果供用户审查。
- [x] 无外部阻塞；已保留失败观察与数据覆盖限制，不进入下一阶段。

## Execution Ledger

- 2026-11-16：用户确认 M0 并明确授权实施；Ponytail 规划/编辑约束已写入 AGENTS。
- Task 1：在现有目录建立 stage/m0-environment-validation 分支；SSH 读取空远端成功。Docker Linux 引擎已启动，Compose 5.6.1；固定源通过 SSH 获取，25个镜像已取得，配置检查仅发布3个回环端口。
- Ruling：复用当前工作区的独立阶段分支，不另建空仓库 worktree，避免复制已有设计资料；不在 main 上实现。
- Ruling：HTTPS Git连接重置后改用SSH获取相同提交；Docker Hub加速地址EOF后用官方 registry-1 获取同版本并重新标记本地tag，不改变上游业务镜像版本。
- Ruling：显式覆盖派生的 OTEL_RESOURCE_ATTRIBUTES，避免仍报告3.0.0；Grafana插件DNS失败后用官方发布的2.34.7归档并验证SHA-256，关闭自动更新并校准内存至384M。均为M0已授权本机配置修正。
- Task 2：先观察实现缺失导致测试失败，再实现；独立审查的2个身份误通过问题和现场SS4O字面键差异均经失败回归后修复；当前14项测试通过。
- 真实接口：Jaeger v3在本部署返回单个JSON result，窗口搜索及按id查询均可用；已核实结账跨服务边。Ad CPU/memory、日志mapping/Payment日志、服务端请求及P95已获得；健康错误序列缺失按需复核记录。
- Ruling：checkout 接近20M后重启，product-catalog 随后出现明确OOM；两者覆盖128M/GOMEMLIMIT96MiB，重新计时，不把旧观察窗算稳定通过。代价是预算增加且需重新观察，仍不改上游业务或全局架构。
- Ruling：原始 Ad CPU gauge 回落慢，保留原值，另用同容器 CPU 纳秒计数器的一分钟 rate 与 Docker stats 校准。代价是两种测量不可逐点等同，文档明确其含义。
- Task 2 final review：补充 OpenSearch 超时/分片失败及下界总量的2项回归，观察3个断言失败后作最小修复；16项全通过。现场已存样本没有搜索超时或分片失败，原证据不受影响。
- Ruling：保留并披露上游 attributes.http mapping冲突丢日志与未部署Kafka采集噪声；M0验证Payment可查询性，不额外改造全服务日志管线。代价是不能承诺全量日志完整，正式接入设计必须解决此限制。
- Task 3 done：两个Go组件均校准后，10:00:40–10:31:30 UTC完成58轮、1450条记录，样本跨度30.78分钟，25个容器ID不变；状态与健康正常、重启0、OOM/die/restart事件0。五用户负载下，内存合计峰值约3.52GiB。
- Task 3 done：最终窗复测Payment故障有7条错误日志、10个错误span；关闭后真实结账成功、滚动错误率0，再做Ad故障与恢复，counter-rate中位数约1.12→400.98→1.11%。最终单元16/16、CLI14/14退出0。
- Ruling：按用户既定流程保留独立阶段分支供审查，不套用合并菜单；远端无基线，不创建main或PR。代价是本轮通过阶段文件/提交审查，而非PR差异。
- Ruling：归档后只停止本项目25容器，不清理镜像/卷或全局Docker。代价是Jaeger在线数据消失，复核依赖已归档样本或重新运行。
- Final：公开清单17文件已检查无本机配置、原始遥测或PDF；无延后的探针审查问题。待用户审查，不进入下一阶段。
