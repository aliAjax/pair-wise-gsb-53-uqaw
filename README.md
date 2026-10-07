# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限、联合批次规划和主附案联动。
- `src/repository.py`：SQLite建表、整批事务、版本快照、名额占用和历史回填。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景和批次一致性测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表并迁移历史数据。

## 批次一致性

联合申请使用同一个批次号和批次版本，案件、材料回执、当日受理名额、留痕和完整批次快照在一个SQLite事务中提交：

- 新批次：`POST /api/batches`。
- 查询完整批次：`GET /api/batches/{batch_no}`。
- 整批保存：`POST /api/batches/{batch_no}/save`。
- 批次留痕：`GET /api/batches/{batch_no}/audit`。
- 整批保存必须提交`expected_version`、全局唯一的`save_token`和完整`cases`。
- 回执编号重复时只合并新增材料或差异字段，并将回执关联到补充提交的案件；不会再次占用名额。
- 主案担保人更换、撤回或主申请人撤回时，所有仍在待办链路中的主/附案立即按新版本失效并重算材料要求；已决定、上诉或归档案件不改原依据，设置`review_status=review_pending`并写入复核留痕。
- 两位经办同时基于同一版本保存时，先取得写锁的事务落盘；后到事务收到409、当前版本和完整快照，调用方保留填写内容后再合并。
- 保存失败整体回滚。使用同一个`save_token`和完整批次重试时，名额占用和留痕均不重复；响应丢失后的重发会返回已提交版本与快照。
- 启动迁移时，缺少批次号的历史记录优先按原承办人和原始日期回填；任一信息补不全时进入`pending_verification`，停止继续办理。

批次保存请求示例：

```json
{
  "expected_version": 1,
  "save_token": "client-generated-unique-token",
  "quota_date": "2026-10-07",
  "quota_limit": 100,
  "cases": [
    {"reference": "MAIN", "is_main": true, "data": {"applicant_id": "A-1", "case_type": "family", "received_day": 1, "deadline_days": 30, "response_day": 1, "representation_active": true, "sponsor_id": "S-1", "required_documents": ["passport"]}},
    {"reference": "DEP-1", "main_reference": "MAIN", "data": {"applicant_id": "A-2", "case_type": "family", "received_day": 1, "deadline_days": 30, "response_day": 1, "representation_active": true, "required_documents": ["passport"]}}
  ],
  "receipts": [
    {"receipt_number": "RC-1", "issued_day": 1, "documents": ["passport", "sponsor_letter"], "case_references": ["MAIN", "DEP-1"], "fields": {"channel": "counter"}}
  ],
  "notes": [
    {"event_key": "note-1", "case_reference": "MAIN", "text": "已核对联合申请关系"}
  ]
}
```

## 其他接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建单案件兼容流程，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、回执差异补录、名额幂等、主附案联动、并发保存、失败恢复和历史回填。
