# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限、材料完整性与冲突检查，以及联合申请批次规则（主案事实级联、回执差额）。
- `src/repository.py`：SQLite建表、加锁事务、批次/回执/名额/幂等保存与历史回填。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与联合批次测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 联合申请批次版本

联合申请按申请人分别建档，案件、材料回执、当日受理名额和留痕共用同一批次版本（`batch_no` + `batch_version`）。

- `POST /api/batches`：`{"batch_no":"J-1","data":{"application_id":"...","quota_per_day":50}}`。
- `GET /api/batches` / `GET /api/batches/{batch_no}`：批次列表 / 完整批次（成员、回执、名额、快照）。
- `POST /api/batches/{batch_no}/saves`：整批保存，请求体：
  ```json
  {
    "expected_version": 0,
    "save_id": "客户端生成的唯一保存号",
    "data": {
      "members": [{"reference":"R-P","role":"primary","applicant_id":"A-1", "...": "建档字段"},
                  {"reference":"R-D","role":"dependent","applicant_id":"A-2", "...": "..."}],
      "receipts": [{"receipt_no":"RC-1","documents":["passport"],"note":""}],
      "quota_day": 100,
      "quota_days": {"RC-1": 100}
    }
  }
  ```
- `GET /api/quota/{day}`：当日名额用量。
- `POST /api/admin/backfill`：手工触发历史数据回填（管理员/主管）。

批次保存语义：

1. **一个批次一个版本**：一次保存内的所有案件、回执、名额和留痕要么全部落盘，要么全部回滚。
2. **回执差额补登**：回执编号重复时只合并材料差异，不新增受理名额；完全重复时仅记录“重复无新增”。
3. **主案事实级联**：主申请人撤回/更换担保人等事实更新后，受影响的待办案件（draft/submitted/evidence_requested/response_received）原依据立即失效并按新事实重算（留痕`fact_invalidated`+`fact_recalculated`）；已决定/上诉案件保留`basis_snapshot`原依据并进入`review`复核；归档与待核案件冻结。
4. **并发单版本落盘**：两位经办同时保存，仅一个版本落盘；另一位收到409 `batch_version_conflict`，响应携带`current_version`和完整`batch`快照，前端保留其填写内容并提示版本已前进。
5. **失败恢复与幂等重试**：保存失败整批回滚，可从`GET /api/batches/{batch_no}`的完整批次恢复；使用同一`save_id`重试时回放首次结果（响应含`replayed:true`），不会重复占名额或重复留痕（留痕有幂等键唯一约束）。
6. **历史数据回填**：服务启动时自动回填缺批次号的旧案件——以原承办人归并到`LEGACY-{承办人}`批次并记录原受理日期；承办人或日期补不全的案件停在`pending_verification`待核状态。回填幂等，可重复执行。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。批次操作角色：`intake_officer`、`legal_rep`、`case_officer`、`supervisor`、`admin`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
