# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果；`delegation`：岗位转授记录。

## 检测结果双人会签

检测结果放行必须由分析员（`sign_analyst`）和授权人（`sign_authorizer`）各签一次，两签的实际签署人不能是同一人。状态流转：

- `pending`：尚未签署。
- `awaiting_analyst`：授权人已签，待分析员签。
- `awaiting_authorizer`：分析员已签，待授权人签。
- `released`：两签齐全，结果放行。

签署时通过 `expected_version` 携带读到的版本号。两人同时提交第二签时，先写入者生效，后到者收到版本冲突（409）。

## 岗位转授

休假时管理员可把岗位临时转给同事：`POST /api/delegations`，请求体含 `role`（`analyst` 或 `authorizer`）、`from_user_id`（委托人）、`to_user_id`（受托人）、`valid_from`、`valid_to`。

- 受托人在转授有效期内代签，操作记在委托人名下（签名的 `on_behalf_of` 字段）。
- 无岗位且无有效转授而越权代签，直接拒绝（403）。
- 岗位一有变化转授即失效：就同一 `role` 与 `from_user_id` 再次转授时，旧转授自动作废；也可通过 `revoke` 动作提前撤回。已作废或超出有效期的转授不能用于签署。

## 撤回签名

`POST /api/entities/<id>/actions`，`{"action":"withdraw_signature","data":{"sign_role":"analyst"}}`。撤回某一签后结果退回待该签状态，另一签保留；签署人、委托人或管理员可撤回。签名与转授均按时间写入审计。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
