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

## 业务规则

- 每个接近事件携带 `observation_version`（初始为 1）。轨道观测更新（`report_revision`）后版本递增，评估立即重算，已批准的规避方案、运营方签字和执行进度一并作废，状态退回 `assessed` 重走审批；作废内容保留在审计事件中。
- `report_revision`、`approve`、`execute`、`execution_update`、`resolve`、`cancel` 必须携带 `expected_version`。两名分析员同时提交观测时，后到的一方会收到版本冲突（409），需重新读取、基于最新结果重算后再提交，不会覆盖前一个结果。
- `execute` 可用 `steps` 登记分步动作；`execution_update` 上报 `completed`/`failed`。执行失败后保留已批准的机动窗口并回到 `coordinating`，重试时只允许执行未完成的动作，全部完成后才能 `resolve`。
- 启动时自动迁移旧数据：缺少观测版本的存量接近事件补成初始版本，原审计记录继续可查，迁移本身也会追加审计事件。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道、运营方意见冲突、观测更新失效重算、执行失败分步重试和旧数据迁移。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
