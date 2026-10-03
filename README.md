# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/repository.py`：SQLite、事务、乐观版本和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources` 和 `POST /api/items/<id>/actions`。身份使用 `X-User-Id`、`X-Role` 请求头。

## 轨道观测版本与失效重算

每个接近事件携带 `observation_version`（初始为 1），评估和规避方案都绑定到各自的观测版本。

- 轨道观测一更新（`report_revision` 动作或提交新来源 `sources`），旧评估、已批准规避方案和运营方签字立即失效：`observation_version` 递增，评估、方案、意见和执行记录被清空，状态回到 `pending`，需要重新评估、重新批准。
- 提交观测（`sources`）必须携带 `expected_version`。两个分析员同时提交时，后到的一方遇到 `version_conflict`（409），需重新读取并基于最新结果重算，不能覆盖前一个结果。
- 已批准的规避方案执行失败（`report_execution_failure`）后保留机动窗口，状态回到 `coordinating`，只重试未完成的 `execute`，不重走审批；全部尝试记录在 `execution.attempts` 中。
- 旧数据升级时，缺少 `observation_version` 的接近事件在初始化时补成初始版本 1，原审计记录继续可查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道、运营方意见冲突、观测更新失效重算、并发提交版本冲突、执行失败重试和旧数据升级。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
