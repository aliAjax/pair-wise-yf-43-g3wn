# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制、质控超限级联和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：批次准入、质控历史与放行操作页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果。
- `batch`：检测批次（`batch_no` + 仪器 + 方法），放行以批次为准入单位；同一仪器上批次号不可重复。
- `qc_check`：质控登记，含标准值、实测值、允许偏差、执行人、执行时间；`|实测值-标准值|` 超允许偏差即超限。

## 放行与质控闭环

1. 建批次登记仪器、方法和批次号，批次初始为准入（`active`）。
2. 检测结果必须挂在准入批次上；放行时按批次上的仪器/方法校验状态。
3. 质控超限：批次自动停用（`suspended`），该批次所有待放行结果退回为 `returned`，结果上列明标准值、实测值、偏差与允许偏差；停用期间不能登记新结果、不能放行。
4. 恢复：由**另一名执行人**做复测（`qc_type=retest`），停用期内只接受复测；**连续两次合格**后，由不同于复测执行人的授权/管理员**复核**（`review_recovery`），批次恢复准入。
5. 退回结果保留原始记录（值、单位、退回原因与偏差），批次恢复后可按原记录重新放行。
6. 批次也可人工停用（维护等），同样退回待放行结果，恢复规则一致。

质控类型：`routine`（准入批次的例行质控）、`retest`（停用后的复测）。复测失败会重新开始"连续两次"计数。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（支持`batches`、`qc_checks`等）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
  - 质控：`POST /api/qc_checks`（登记即判定，超限自动停用批次并退回结果）。
  - 批次：`suspend`（人工停用）、`review_recovery`（两次合格复测后复核恢复）。
  - 结果：`release`（放行/退回后重新放行）、`return_for_qc`（系统级联退回）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制；登记质控时`performed_by`必须等于当前`X-User-Id`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
