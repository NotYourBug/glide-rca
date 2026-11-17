# GLIDE RCA

基于 GALA+ 思路的微服务智能排障项目。全局路线为 OpenTelemetry Demo 实验环境 + RCAEval 回放，后端使用 Python，模型服务商暂定 DeepSeek。

**M0：实验环境与接口验证已于 2026-11-17 获用户审查通过。当前进入 M1 设计阶段，方案等待确认，尚未开始开发。** 已有代码仍为本地观测接口探针，不提供根因诊断业务。

- [设计目录](docs/design/README.md)
- [待确认的 M1 设计：可信数据与确定性诊断基线](docs/design/stages/m1-design.md)
- [已确认的 M0 设计](docs/design/stages/m0-design.md)
- [M0 操作说明](docs/runbooks/m0-local-lab.md)
- [M0 实测与交付状态](docs/delivery/m0-validation.md)
- [M0 日志映射缺陷修复](docs/delivery/m0-log-ingestion-fix.md)
- [固定协作规则与 Ponytail 约束](AGENTS.md)

探针仅使用 Python 3.11+ 标准库，测试不依赖 Docker 或外网：

```powershell
python -m unittest discover -s tests -v
```

仓库不包含第三方 Demo 源码、镜像、原始遥测、私有配置和数据集；如何获取固定版本见操作说明。
