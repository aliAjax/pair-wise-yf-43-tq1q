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

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果；`delegation`：岗位临时转授。

## 检测结果双人会签

检测结果不再一次放行，必须经两签：

1. 分析员（`analyst`）提交首签，同时提交仪器、方法、数值等放行包，结果进入 `countersigning`。
2. 授权人（`authorizer`）提交第二签，结果进入 `released`。
3. 两签不能为同一人——同时校验**记名人**（岗位委托人）和**实际操作人**。
4. `withdraw` 撤回某一签：撤回后结果退回 `countersigning`（另一签保留）或 `pending`（无剩余签名）。
5. 两人同时提交第二签时，先写入者生效，后到者凭乐观锁收到 `409 ConflictError`（`expected_version` 陈旧也同样报版本冲突）。

动作：

- `POST /api/entities/<id>/actions` 提交 `{"action":"sign","data":{...},"expected_version":n}`
- 或 `{"action":"withdraw","data":{"slot":"analyst|authorizer"}}`

## 岗位临时转授（休假代岗）

管理员创建转授，把某用户的某个岗位临时交给同事：

```
POST /api/delegation
{"principal_id":"analyst-1","agent_id":"backup-7","role":"analyst","expires_at":"可选ISO时间"}
```

- 只有 `admin` 可创建/撤销；委托人与代理人不能是同一人；可带 `expires_at`。
- 代理人请求时带头 `X-On-Behalf-Of: analyst-1`（需要消歧时再加 `X-Delegation-Id`）。
- 代做的业务操作（签名槽、`released_by` 等）**记在委托人名下**；审计时间线同时记录实际操作人 `actor_id` 与 `detail.delegated.principal_id/physical_actor/delegation_id`。
- **越权代签直接拒绝**：无有效转授、转授岗位与动作所需岗位不符、转授已过期，一律 `403 PermissionDenied`。所需岗位由实体状态确定性推出（如会签缺哪一签）。
- **岗位一变即失效**：同委托人同岗位再建转授，旧转授自动变为 `superseded`；`revoke` 动作使其变为 `revoked`。失效后代理人立即无法再以委托人名义操作。
- 委托人本人仍可直接操作（不带 `X-On-Behalf-Of` 即按本人身份）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录（签名、撤回、转授的创建/覆盖/撤销均按时间留痕）。

请求身份通过`X-User-Id`和`X-Role`请求头传入；代岗另传`X-On-Behalf-Of`（及可选`X-Delegation-Id`）。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
