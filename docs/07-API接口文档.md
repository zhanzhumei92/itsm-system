# 亿网通IT运维监控管理平台 — API 接口文档

> **本文档由 `scripts/gen_api_doc.py` 从代码自动生成，请勿手工修改。**
> 生成依据：`FastAPI.openapi()`（接口/参数/出入参结构）+ AST 扫描源码（接口权限声明）
> 生成时间：2026-09-22 09:50:54
> 服务地址：`http://<host>:<port>` ｜ 接口总前缀：`/api/v1` ｜ **共 75 个接口**
> **字段说明覆盖率：572/579 = 98.8%**（剩余字段为自解释字段如 `id`/`name`，或尚未补写说明；该比率随代码一起更新）

> **为什么会做成自动生成：** 接口文档与代码是最典型的"同一件事两处定义"。
> 手写文档的准确性只存在于写完的那一刻；三个月后字段改了名、加了参数，
> 文档还停留在旧版本 —— 而失效点要到联调那天才被发现。
> 本文档每次执行脚本都会被完整重算，**不可能漂移**。
>
> 唯一的例外是"接口需要什么权限"这件事：OpenAPI 规范里没有地方表达
> `dependencies=[Depends(require_role(ROLE_ADMIN))]` 这种依赖（它不产生入参），
> 因此脚本额外用 AST 直接读源码里的权限声明作为第二个事实源。

**在线版本**：服务启动后访问 `/docs`（Swagger UI）或 `/redoc`（ReDoc），
OpenAPI JSON 在 `/openapi.json`。

---

## 一、统一约定

### 1.1 请求

| 项 | 约定 |
| --- | --- |
| 接口前缀 | `/api/v1` |
| 请求编码 | `application/json; charset=utf-8`（文件上传接口用 `multipart/form-data`） |
| 鉴权方式 | `Authorization: Bearer <token>`（除登录接口外全部需要） |
| 分页参数 | `page`（从 1 开始）、`page_size`（默认 20） |
| 时间格式 | `YYYY-MM-DD HH:MM:SS`（字符串，非时间戳） |

### 1.2 响应包装

所有接口统一返回三层结构：

```json
{
  "code": 0,
  "msg": "成功",
  "data": { }
}
```

- `code = 0` 表示业务成功；非 0 时 `msg` 是可直接展示给用户的中文提示（前端无需再翻译）
- `data` 为业务数据，失败时为 `null`
- **分页接口的 `data` 固定为**：`{ total, page, page_size, pages, items }`

### 1.3 HTTP 状态码 与 业务码的分工

这套分工是为了让前端**只写一套判断逻辑**：

| 类别 | 表达 | HTTP | 码值 | 前端该做什么 |
| --- | --- | --- | --- | --- |
| 请求本身没被正确处理 | 协议层/权限层错误 | **4xx** | 400 / 401 / 403 / 404 | 拦截器统一处理：401 跳登录、403 提示无权限 |
| 请求处理了但业务不允许 | 业务规则不通过 | **200** | 1xxx | 弹 `body.msg` 即可 |

业务码一览：

| code | 含义 | 典型场景 |
| --- | --- | --- |
| `0` | 成功 | — |
| `1001` | 数据重复 | 资产编号重复、设备 IP 重复、账号已存在 |
| `1002` | 设备忙 | 设备正在采集中，操作被拒 |
| `1003` | 配置缺失 | 未配置企微 Webhook 就点了"渠道自测" |
| `1004` | 操作被拒 | 内置数据保护、被其它数据依赖占用 |
| `1005` | 状态不合法 | 已恢复的告警不能再确认 |
| `1006` | 文件格式不合法 | Excel 导入时表头缺失、内容为空 |

> **为什么业务错误要用 HTTP 200 而不是 4xx**：
> 若返回 4xx，前端 axios 会把它当成"网络/协议异常"抛进 catch，
> 于是每个调用点都要写一遍"到底是真的网络挂了，还是资产编号重复"。
> 统一走 200 + 业务码，前端只需 `if (res.code !== 0) toast(res.msg)` 一行。

### 1.4 鉴权与角色

本项目**只有两个角色**（`app/schemas/user.py` → `VALID_ROLES = {"admin", "viewer"}`）：

| 角色 | 权限范围 |
| --- | --- |
| **管理员** `admin` | 全部：设备/资产/告警规则/系统配置/用户账号的增删改，以及告警确认 |
| **运维查看员** `viewer` | 只读全部数据 + **告警确认**（值班人员要能认领告警） |

> 需求书里提到的"操作员"角色在本版本中由 `viewer` 承担：区分只读与可写两档已经覆盖了
> 政企运维的实际组织形态（值班同事认领告警、管理员改配置）。若现场确需中间 permissions 档，
> 扩展点是 `deps.require_role(*roles)` —— 传入新角色名即可，不需要改任何已有路由。

实现要点（写在这里是为了让调用方理解行为）：

1. **每次请求都重新查库取用户**，不信任 JWT 里的 `role` 字段。
   管理员把某人从 admin 降为 viewer 后必须**立即生效** ——
   若信令牌里的 role，用户在令牌过期前仍保有旧权限，这是真实的安全漏洞。
2. 令牌有效期由 `ACCESS_TOKEN_EXPIRE_MINUTES` 控制（默认 720 分钟 = 12 小时，一个班次）。
3. 401 响应前端可据此判断"需要重新登录"，不会与业务错误混淆。

### 1.5 典型调用流程

```bash
# 1. 登录拿 token
TOKEN=$(curl -s -X POST http://127.0.0.1:8011/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['access_token'])")

# 2. 带 token 调用业务接口
curl -s 'http://127.0.0.1:8011/api/v1/devices?page=1&page_size=20' \
  -H "Authorization: Bearer $TOKEN"
```

---

## 二、接口总览

| 模块 | 接口数 | 对应需求 | 说明 |
| --- | --- | --- | --- |
| **认证** | 4 | 模块5 后台管理 | 登录、登出、获取当前用户信息、刷新令牌。 |
| **用户管理** | 7 | 模块5 后台管理 | 后台账号的增删改查、启停、角色分配与密码修改。 |
| **设备管理** | 14 | 模块1 设备接入与协议采集 | 设备台账 CRUD、启停采集、连通性测试、凭据管理、实时指标。 |
| **资产管理** | 10 | 模块2 IT 资产管理 | IT 资产台账 CRUD、与设备绑定/解绑、Excel 批量导入导出、维保预警。 |
| **指标管理** | 6 | 模块3 监控指标与自定义告警 | 指标字典查询、实时值、历史采样与趋势数据。 |
| **告警管理** | 15 | 模块3 监控指标与自定义告警 | 告警规则 CRUD、静默、告警事件确认/关闭、处理时间线、推送记录、渠道自测。 |
| **可视化大屏** | 6 | 模块4 可视化大屏 | 大屏聚合数据：KPI 总览、设备状态灯矩阵、趋势、排行榜、分布、整屏打包。 |
| **日志查询** | 4 | 模块5 后台管理 | 系统日志、采集日志、告警日志三类日志查询与采集成功率统计。 |
| **系统配置** | 6 | 模块5 后台管理 | 运行参数在线编辑、批量保存、渠道连通性自测、敏感项掩码。 |
| **系统信息** | 3 | 模块5 后台管理 | 健康检查、系统信息、运行时状态。 |

---

## 三、认证（4 个接口）

> 对应需求：模块5 后台管理

### 三.1 `POST /api/v1/auth/login`

**用户登录** ｜ 权限：公开（无需登录） ｜ 定义位置：`app/api/v1/auth.py`

登录并获取 JWT 访问令牌。

- 账号或密码错误统一返回 401，不区分"用户不存在"与"密码错误"
- 登录成功/失败都会写入 sys_log，失败记录可用于排查异常登录

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `username` | 字符串 | 是 | 登录账号 |
| `password` | 字符串 | 是 | 登录密码 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `access_token` | 字符串 | 是 | JWT 访问令牌；后续请求以 `Authorization: Bearer <此值>` 携带 |
| `token_type` | 字符串 | 否 | 令牌类型，固定为 bearer |
| `expires_in` | 整数 | 是 | 有效期（秒）；到期后接口返回 401，前端需重新登录 |
| `role` | 字符串 | 是 | 当前用户角色：admin 管理员 / viewer 运维查看员 |
| `username` | 字符串 | 是 | 登录账号 |
| `real_name` | 字符串 | 否 | 显示名，未设置时为 None |
| `must_change_pwd` | 布尔 | 否 | 是否需强制修改密码；为 true 时前端应跳转到修改密码页而不是首页（管理员重置密码后的新账号会带此标记） |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/auth/login' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 三.2 `POST /api/v1/auth/logout`

**退出登录** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/auth.py`

退出登录。

JWT 是无状态的，服务端无法真正"注销"令牌，前端需自行清除本地令牌。
此处仅记录操作日志，便于审计"谁在什么时候退出了系统"。

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/auth/logout' \
  -H 'Authorization: Bearer $TOKEN'
```

### 三.3 `POST /api/v1/auth/change-password`

**修改自己的密码** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/auth.py`

修改密码需验证原密码，防止令牌被盗后直接接管账号

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `old_password` | 字符串 | 是 | 当前密码；不知道密码改不了账号，防止令牌被盗后直接接管 |
| `new_password` | 字符串 | 是 | 新密码，至少 6 位 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/auth/change-password' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 三.4 `GET /api/v1/auth/me`

**获取当前登录用户信息** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/auth.py`

前端刷新页面时调用，用于恢复用户信息与权限判断。

注意：每次都从数据库读取，保证角色变更立即生效（不信任令牌内的 role）。

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 用户 ID |
| `username` | 字符串 | 是 | 登录账号，2-64 位，创建后不可修改 |
| `real_name` | 字符串 | 否 | 显示姓名 |
| `role` | 字符串 | 是 | 角色：admin 管理员 / viewer 运维查看员 |
| `email` | 字符串 | 否 | 邮箱；用于接收邮件告警 |
| `phone` | 字符串 | 否 | 手机号 |
| `status` | 布尔 | 否 | 是否启用；false 后该账号立即无法登录（令牌校验每次都查库） |
| `must_change_pwd` | 布尔 | 否 | 是否需强制改密 |
| `last_login_at` | 字符串 | 否 | 最近登录时间 |
| `last_login_ip` | 字符串 | 否 | 最近登录 IP |
| `remark` | 字符串 | 否 | 备注 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `role_text` | 字符串 | 是 | 角色中文名 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/auth/me' \
  -H 'Authorization: Bearer $TOKEN'
```

---

## 四、用户管理（7 个接口）

> 对应需求：模块5 后台管理

### 四.1 `GET /api/v1/users`

**用户列表** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/users.py`

分页查询用户（所有登录用户可见，便于查看责任人信息）

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | 整数 | 否 | `1` |  |
| `page_size` | 整数 | 否 | `20` |  |
| `keyword` | 字符串 | 否 |  | 账号或姓名模糊搜索 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `total` | 整数 | 否 | 总记录数 |
| `page` | 整数 | 否 | 当前页码，从 1 开始 |
| `page_size` | 整数 | 否 | 每页条数 |
| `pages` | 整数 | 否 | 总页数 |
| `items` | 对象[] | 否 | 当前页数据 |
| `items[].id` | 整数 | 是 | 用户 ID |
| `items[].username` | 字符串 | 是 | 登录账号，2-64 位，创建后不可修改 |
| `items[].real_name` | 字符串 | 否 | 显示姓名 |
| `items[].role` | 字符串 | 是 | 角色：admin 管理员 / viewer 运维查看员 |
| `items[].email` | 字符串 | 否 | 邮箱；用于接收邮件告警 |
| `items[].phone` | 字符串 | 否 | 手机号 |
| `items[].status` | 布尔 | 否 | 是否启用；false 后该账号立即无法登录（令牌校验每次都查库） |
| `items[].must_change_pwd` | 布尔 | 否 | 是否需强制改密 |
| `items[].last_login_at` | 字符串 | 否 | 最近登录时间 |
| `items[].last_login_ip` | 字符串 | 否 | 最近登录 IP |
| `items[].remark` | 字符串 | 否 | 备注 |
| `items[].created_at` | 字符串 | 否 | 创建时间 |
| `items[].role_text` | 字符串 | 是 | 角色中文名 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/users' \
  -H 'Authorization: Bearer $TOKEN'
```

### 四.2 `POST /api/v1/users`

**新增用户（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/users.py`

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `username` | 字符串 | 是 | 登录账号，2-64 位，创建后不可修改 |
| `password` | 字符串 | 是 | 初始密码，至少 6 位；建议配合 must_change_pwd 强制首次登录改密 |
| `real_name` | 字符串 | 否 | 显示姓名 |
| `role` | 字符串 | 否 | 角色：admin 管理员 / viewer 运维查看员 |
| `email` | 字符串 | 否 | 邮箱；用于接收邮件告警 |
| `phone` | 字符串 | 否 | 手机号 |
| `status` | 布尔 | 否 | 是否启用；false 后该账号立即无法登录（令牌校验每次都查库） |
| `remark` | 字符串 | 否 | 备注 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 用户 ID |
| `username` | 字符串 | 是 | 登录账号，2-64 位，创建后不可修改 |
| `real_name` | 字符串 | 否 | 显示姓名 |
| `role` | 字符串 | 是 | 角色：admin 管理员 / viewer 运维查看员 |
| `email` | 字符串 | 否 | 邮箱；用于接收邮件告警 |
| `phone` | 字符串 | 否 | 手机号 |
| `status` | 布尔 | 否 | 是否启用；false 后该账号立即无法登录（令牌校验每次都查库） |
| `must_change_pwd` | 布尔 | 否 | 是否需强制改密 |
| `last_login_at` | 字符串 | 否 | 最近登录时间 |
| `last_login_ip` | 字符串 | 否 | 最近登录 IP |
| `remark` | 字符串 | 否 | 备注 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `role_text` | 字符串 | 是 | 角色中文名 |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/users' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 四.3 `GET /api/v1/users/roles`

**角色字典** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/users.py`

前端下拉框数据源，集中定义避免前后端角色取值不一致

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/users/roles' \
  -H 'Authorization: Bearer $TOKEN'
```

### 四.4 `GET /api/v1/users/{user_id}`

**用户详情** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/users.py`

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 用户 ID |
| `username` | 字符串 | 是 | 登录账号，2-64 位，创建后不可修改 |
| `real_name` | 字符串 | 否 | 显示姓名 |
| `role` | 字符串 | 是 | 角色：admin 管理员 / viewer 运维查看员 |
| `email` | 字符串 | 否 | 邮箱；用于接收邮件告警 |
| `phone` | 字符串 | 否 | 手机号 |
| `status` | 布尔 | 否 | 是否启用；false 后该账号立即无法登录（令牌校验每次都查库） |
| `must_change_pwd` | 布尔 | 否 | 是否需强制改密 |
| `last_login_at` | 字符串 | 否 | 最近登录时间 |
| `last_login_ip` | 字符串 | 否 | 最近登录 IP |
| `remark` | 字符串 | 否 | 备注 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `role_text` | 字符串 | 是 | 角色中文名 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/users/{user_id}' \
  -H 'Authorization: Bearer $TOKEN'
```

### 四.5 `PUT /api/v1/users/{user_id}`

**编辑用户（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/users.py`

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | 整数 |  |

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `real_name` | 字符串 | 否 | 显示姓名 |
| `role` | 字符串 | 否 | 角色：admin 管理员 / viewer 运维查看员；降级后立即生效 |
| `email` | 字符串 | 否 | 邮箱 |
| `phone` | 字符串 | 否 | 手机号 |
| `status` | 布尔 | 否 | 是否启用；false 立即禁止登录 |
| `remark` | 字符串 | 否 | 备注 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 用户 ID |
| `username` | 字符串 | 是 | 登录账号，2-64 位，创建后不可修改 |
| `real_name` | 字符串 | 否 | 显示姓名 |
| `role` | 字符串 | 是 | 角色：admin 管理员 / viewer 运维查看员 |
| `email` | 字符串 | 否 | 邮箱；用于接收邮件告警 |
| `phone` | 字符串 | 否 | 手机号 |
| `status` | 布尔 | 否 | 是否启用；false 后该账号立即无法登录（令牌校验每次都查库） |
| `must_change_pwd` | 布尔 | 否 | 是否需强制改密 |
| `last_login_at` | 字符串 | 否 | 最近登录时间 |
| `last_login_ip` | 字符串 | 否 | 最近登录 IP |
| `remark` | 字符串 | 否 | 备注 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `role_text` | 字符串 | 是 | 角色中文名 |

**示例**

```bash
curl -X PUT 'http://127.0.0.1:8011/api/v1/users/{user_id}' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 四.6 `DELETE /api/v1/users/{user_id}`

**删除用户（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/users.py`

内置 admin 账号与当前登录账号不允许删除（防止把自己锁在系统外）

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X DELETE 'http://127.0.0.1:8011/api/v1/users/{user_id}' \
  -H 'Authorization: Bearer $TOKEN'
```

### 四.7 `POST /api/v1/users/{user_id}/reset-password`

**重置密码（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/users.py`

管理员重置他人密码。

默认要求对方下次登录强制改密——避免管理员知晓他人永久密码造成的责任不清。

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | 整数 |  |

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `new_password` | 字符串 | 是 | 新密码，至少 6 位 |
| `must_change_pwd` | 布尔 | 否 | 是否要求对方下次登录改密 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/users/{user_id}/reset-password' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

---

## 五、设备管理（14 个接口）

> 对应需求：模块1 设备接入与协议采集

### 五.1 `GET /api/v1/devices`

**设备列表** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | 整数 | 否 | `1` |  |
| `page_size` | 整数 | 否 | `20` |  |
| `keyword` | 字符串 | 否 |  | 名称/IP/位置模糊搜索 |
| `protocol` | 字符串 | 否 |  |  |
| `device_type` | 字符串 | 否 |  |  |
| `online_status` | 字符串 | 否 |  | online/offline/unknown |
| `enabled` | 布尔 | 否 |  |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `total` | 整数 | 否 | 总记录数 |
| `page` | 整数 | 否 | 当前页码，从 1 开始 |
| `page_size` | 整数 | 否 | 每页条数 |
| `pages` | 整数 | 否 | 总页数 |
| `items` | 对象[] | 否 | 当前页数据 |
| `items[].id` | 整数 | 是 | 设备 ID |
| `items[].name` | 字符串 | 否 | 设备名称 |
| `items[].ip` | 字符串 | 否 | 管理 IP |
| `items[].device_type` | 字符串 | 否 | switch/router/firewall/server/storage/camera/access/ap/ups/other |
| `items[].vendor` | 字符串 | 否 | 厂商标识，如 huawei/h3c/cisco/ruijie/sangfor/hikvision；前端展示的中文名由 DeviceOut.vendor_text 提供 |
| `items[].os_type` | 字符串 | 否 | 操作系统/固件描述，如 CentOS 7.9 / Windows Server 2019 |
| `items[].protocol` | 字符串 | 否 | 采集协议：snmp/ssh/winrm/icmp，取值见 /api/v1/devices/protocols |
| `items[].model` | 字符串 | 否 | 设备型号 |
| `items[].location` | 字符串 | 否 | 物理位置，如 3号楼机房A柜 |
| `items[].collect_interval` | 整数 | 否 | 采集周期（秒），最小 10 秒 |
| `items[].timeout` | 整数 | 否 | 采集超时（秒） |
| `items[].enabled` | 布尔 | 否 | 是否启用采集；false 时大屏显示“停用”而不是“在线” |
| `items[].remark` | 字符串 | 否 | 备注 |
| `items[].online_status` | 字符串 | 否 | 在线状态：online/offline/unknown |
| `items[].fail_count` | 整数 | 否 | 连续采集失败次数；达到阈值后判为离线 |
| `items[].last_collect_at` | 字符串 | 否 | 最近一次采集时间 |
| `items[].last_online_at` | 字符串 | 否 | 最近一次在线时间 |
| `items[].created_at` | 字符串 | 否 | 创建时间 |
| `items[].protocol_text` | 字符串 | 是 | 协议中文名 |
| `items[].device_type_text` | 字符串 | 是 | 设备类型中文名 |
| `items[].online_text` | 字符串 | 是 | 在线状态中文名 |
| `items[].vendor_text` | 字符串 | 是 | 厂商中文名（未登记的品牌原样透出） |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/devices' \
  -H 'Authorization: Bearer $TOKEN'
```

### 五.2 `POST /api/v1/devices`

**新增设备（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

新增监控设备（设备 + 协议凭据一次提交）。

凭据中的口令会在服务层用 Fernet 加密后入库，数据库中不存在明文口令。

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | 字符串 | 是 | 设备名称 |
| `ip` | 字符串 | 是 | 管理 IP |
| `device_type` | 字符串 | 是 | switch/router/firewall/server/storage/camera/access/ap/ups/other |
| `vendor` | 字符串 | 否 | 厂商标识，如 huawei/h3c/cisco/ruijie/sangfor/hikvision；前端展示的中文名由 DeviceOut.vendor_text 提供 |
| `os_type` | 字符串 | 否 | 操作系统/固件描述，如 CentOS 7.9 / Windows Server 2019 |
| `protocol` | 字符串 | 是 | 采集协议：snmp/ssh/winrm/icmp，取值见 /api/v1/devices/protocols |
| `model` | 字符串 | 否 | 设备型号 |
| `location` | 字符串 | 否 | 物理位置，如 3号楼机房A柜 |
| `collect_interval` | 整数 | 否 | 采集周期（秒），最小 10 秒 |
| `timeout` | 整数 | 否 | 采集超时（秒） |
| `enabled` | 布尔 | 否 | 是否启用采集；false 时大屏显示“停用”而不是“在线” |
| `remark` | 字符串 | 否 | 备注 |
| `credential` | 对象 | 是 | 协议凭据输入（明文）。  ⚠ 安全约定：本 Schema 中的密码/团体字字段是**明文入参**， 由 DeviceService 在写库前调用 encrypt_secret() 加密， **绝不允许直接把本对象 dump 后写入数据库**。 |
| `credential.protocol` | 字符串 | 是 | snmp/ssh/winrm/icmp |
| `credential.snmp_version` | 字符串 | 否 | v2c/v3 |
| `credential.snmp_community` | 字符串 | 否 | v2c 团体字 |
| `credential.snmp_port` | 整数 | 否 | SNMP 端口，默认 161 |
| `credential.snmp_username` | 字符串 | 否 | v3 安全名 |
| `credential.snmp_auth_proto` | 字符串 | 否 | MD5/SHA/SHA256 |
| `credential.snmp_auth_key` | 字符串 | 否 | v3 认证口令 |
| `credential.snmp_priv_proto` | 字符串 | 否 | DES/AES/AES256 |
| `credential.snmp_priv_key` | 字符串 | 否 | v3 加密口令 |
| `credential.snmp_timeout` | 整数 | 否 | SNMP 超时（秒） |
| `credential.snmp_retries` | 整数 | 否 | SNMP 重试次数 |
| `credential.ssh_port` | 整数 | 否 | SSH 端口，默认 22 |
| `credential.ssh_username` | 字符串 | 否 | SSH 登录账号 |
| `credential.ssh_password` | 字符串 | 否 | SSH 登录口令（明文入参，写库前加密） |
| `credential.ssh_key_path` | 字符串 | 否 | 私钥文件路径；留空表示用口令登录 |
| `credential.ssh_device_type` | 字符串 | 否 | huawei/hp_comware/linux |
| `credential.winrm_port` | 整数 | 否 | WinRM 端口，默认 5985 |
| `credential.winrm_username` | 字符串 | 否 | Windows 账号 |
| `credential.winrm_password` | 字符串 | 否 | Windows 口令（明文入参，写库前加密） |
| `credential.winrm_transport` | 字符串 | 否 | ntlm/basic/ssl |
| `credential.winrm_scheme` | 字符串 | 否 | http/https |
| `credential.ping_count` | 整数 | 否 | 每轮发包数 |
| `credential.ping_interval` | 整数 | 否 | 发包间隔（秒） |
| `credential.ping_timeout` | 整数 | 否 | 单包超时（秒） |
| `credential.ping_size` | 整数 | 否 | 包大小（字节） |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 设备 ID |
| `name` | 字符串 | 否 | 设备名称 |
| `ip` | 字符串 | 否 | 管理 IP |
| `device_type` | 字符串 | 否 | switch/router/firewall/server/storage/camera/access/ap/ups/other |
| `vendor` | 字符串 | 否 | 厂商标识，如 huawei/h3c/cisco/ruijie/sangfor/hikvision；前端展示的中文名由 DeviceOut.vendor_text 提供 |
| `os_type` | 字符串 | 否 | 操作系统/固件描述，如 CentOS 7.9 / Windows Server 2019 |
| `protocol` | 字符串 | 否 | 采集协议：snmp/ssh/winrm/icmp，取值见 /api/v1/devices/protocols |
| `model` | 字符串 | 否 | 设备型号 |
| `location` | 字符串 | 否 | 物理位置，如 3号楼机房A柜 |
| `collect_interval` | 整数 | 否 | 采集周期（秒），最小 10 秒 |
| `timeout` | 整数 | 否 | 采集超时（秒） |
| `enabled` | 布尔 | 否 | 是否启用采集；false 时大屏显示“停用”而不是“在线” |
| `remark` | 字符串 | 否 | 备注 |
| `online_status` | 字符串 | 否 | 在线状态：online/offline/unknown |
| `fail_count` | 整数 | 否 | 连续采集失败次数；达到阈值后判为离线 |
| `last_collect_at` | 字符串 | 否 | 最近一次采集时间 |
| `last_online_at` | 字符串 | 否 | 最近一次在线时间 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `credential` | 对象 | 否 | 已脱敏的协议凭据；敏感项只回传 has_* 标记 |
| `protocol_text` | 字符串 | 是 | 协议中文名 |
| `device_type_text` | 字符串 | 是 | 设备类型中文名 |
| `online_text` | 字符串 | 是 | 在线状态中文名 |
| `vendor_text` | 字符串 | 是 | 厂商中文名（未登记的品牌原样透出） |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/devices' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 五.3 `POST /api/v1/devices/batch-toggle`

**批量启停采集任务** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

批量启停（批量维护时使用）。

单台失败不中断整批：逐台 try/except，失败原因汇总返回，
避免"第 3 台设备 ID 不存在"导致后面 50 台都没处理。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `enabled` | 布尔 | 是 |  | true 启用 / false 停用 |

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ids` | 整数[] | 否 | ID 列表 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `success` | 整数 | 否 | 成功条数 |
| `failed` | 整数 | 否 | 失败条数 |
| `messages` | 字符串[] | 否 | 失败原因列表 |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/devices/batch-toggle' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 五.4 `GET /api/v1/devices/protocols`

**支持的采集协议列表** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

返回当前平台已实现采集器的协议。

前端"新增设备"表单据此渲染协议下拉框，
未实现的协议不会出现在选项中，避免用户配置了却采不到数据。

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/devices/protocols' \
  -H 'Authorization: Bearer $TOKEN'
```

### 五.5 `GET /api/v1/devices/simple`

**设备精简列表** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

全部设备精简信息（供告警规则、指标查询页面的设备下拉框使用）。

最多返回 2000 台：超过此规模应改用搜索接口，
否则一次响应几 MB 会明显拖慢页面。

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `[].id` | 整数 | 是 | 设备 ID |
| `[].name` | 字符串 | 否 | 设备名称 |
| `[].ip` | 字符串 | 否 | 管理 IP |
| `[].device_type` | 字符串 | 否 | 设备类型 |
| `[].protocol` | 字符串 | 否 | 采集协议 |
| `[].online_status` | 字符串 | 否 | 在线状态 |
| `[].enabled` | 布尔 | 否 | 是否启用采集 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/devices/simple' \
  -H 'Authorization: Bearer $TOKEN'
```

### 五.6 `GET /api/v1/devices/{device_id}`

**设备详情** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

详情含脱敏后的协议凭据（密钥类字段只返回"是否已配置"）

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `device_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 设备 ID |
| `name` | 字符串 | 否 | 设备名称 |
| `ip` | 字符串 | 否 | 管理 IP |
| `device_type` | 字符串 | 否 | switch/router/firewall/server/storage/camera/access/ap/ups/other |
| `vendor` | 字符串 | 否 | 厂商标识，如 huawei/h3c/cisco/ruijie/sangfor/hikvision；前端展示的中文名由 DeviceOut.vendor_text 提供 |
| `os_type` | 字符串 | 否 | 操作系统/固件描述，如 CentOS 7.9 / Windows Server 2019 |
| `protocol` | 字符串 | 否 | 采集协议：snmp/ssh/winrm/icmp，取值见 /api/v1/devices/protocols |
| `model` | 字符串 | 否 | 设备型号 |
| `location` | 字符串 | 否 | 物理位置，如 3号楼机房A柜 |
| `collect_interval` | 整数 | 否 | 采集周期（秒），最小 10 秒 |
| `timeout` | 整数 | 否 | 采集超时（秒） |
| `enabled` | 布尔 | 否 | 是否启用采集；false 时大屏显示“停用”而不是“在线” |
| `remark` | 字符串 | 否 | 备注 |
| `online_status` | 字符串 | 否 | 在线状态：online/offline/unknown |
| `fail_count` | 整数 | 否 | 连续采集失败次数；达到阈值后判为离线 |
| `last_collect_at` | 字符串 | 否 | 最近一次采集时间 |
| `last_online_at` | 字符串 | 否 | 最近一次在线时间 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `credential` | 对象 | 否 | 已脱敏的协议凭据；敏感项只回传 has_* 标记 |
| `protocol_text` | 字符串 | 是 | 协议中文名 |
| `device_type_text` | 字符串 | 是 | 设备类型中文名 |
| `online_text` | 字符串 | 是 | 在线状态中文名 |
| `vendor_text` | 字符串 | 是 | 厂商中文名（未登记的品牌原样透出） |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/devices/{device_id}' \
  -H 'Authorization: Bearer $TOKEN'
```

### 五.7 `PUT /api/v1/devices/{device_id}`

**编辑设备（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

编辑设备与凭据。

凭据中的敏感字段（口令、团体字）留空表示"保持不变"，
这样前端在无法回显口令的前提下也能安全地修改其它参数。

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `device_id` | 整数 |  |

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | 字符串 | 否 | 设备名称 |
| `ip` | 字符串 | 否 | 管理 IP 或主机名 |
| `device_type` | 字符串 | 否 | 设备类型，取值见 /api/v1/devices/types |
| `vendor` | 字符串 | 否 | 厂商标识（huawei/h3c/...），见 GET /api/v1/devices/vendors |
| `os_type` | 字符串 | 否 | 操作系统/固件版本描述 |
| `protocol` | 字符串 | 否 | 采集协议，取值见 /api/v1/devices/protocols |
| `model` | 字符串 | 否 | 设备型号 |
| `location` | 字符串 | 否 | 物理位置 |
| `collect_interval` | 整数 | 否 | 采集周期（秒） |
| `timeout` | 整数 | 否 | 单次采集超时（秒） |
| `enabled` | 布尔 | 否 | 是否参与采集；false 后大屏显示“停用”而非“在线” |
| `remark` | 字符串 | 否 | 备注 |
| `credential` | 对象 | 否 | 传入则更新凭据；敏感项留空表示保持原值不变 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 设备 ID |
| `name` | 字符串 | 否 | 设备名称 |
| `ip` | 字符串 | 否 | 管理 IP |
| `device_type` | 字符串 | 否 | switch/router/firewall/server/storage/camera/access/ap/ups/other |
| `vendor` | 字符串 | 否 | 厂商标识，如 huawei/h3c/cisco/ruijie/sangfor/hikvision；前端展示的中文名由 DeviceOut.vendor_text 提供 |
| `os_type` | 字符串 | 否 | 操作系统/固件描述，如 CentOS 7.9 / Windows Server 2019 |
| `protocol` | 字符串 | 否 | 采集协议：snmp/ssh/winrm/icmp，取值见 /api/v1/devices/protocols |
| `model` | 字符串 | 否 | 设备型号 |
| `location` | 字符串 | 否 | 物理位置，如 3号楼机房A柜 |
| `collect_interval` | 整数 | 否 | 采集周期（秒），最小 10 秒 |
| `timeout` | 整数 | 否 | 采集超时（秒） |
| `enabled` | 布尔 | 否 | 是否启用采集；false 时大屏显示“停用”而不是“在线” |
| `remark` | 字符串 | 否 | 备注 |
| `online_status` | 字符串 | 否 | 在线状态：online/offline/unknown |
| `fail_count` | 整数 | 否 | 连续采集失败次数；达到阈值后判为离线 |
| `last_collect_at` | 字符串 | 否 | 最近一次采集时间 |
| `last_online_at` | 字符串 | 否 | 最近一次在线时间 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `credential` | 对象 | 否 | 已脱敏的协议凭据；敏感项只回传 has_* 标记 |
| `protocol_text` | 字符串 | 是 | 协议中文名 |
| `device_type_text` | 字符串 | 是 | 设备类型中文名 |
| `online_text` | 字符串 | 是 | 在线状态中文名 |
| `vendor_text` | 字符串 | 是 | 厂商中文名（未登记的品牌原样透出） |

**示例**

```bash
curl -X PUT 'http://127.0.0.1:8011/api/v1/devices/{device_id}' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 五.8 `DELETE /api/v1/devices/{device_id}`

**删除设备（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

删除设备。

会清理凭据、最新指标与资产绑定关系；
历史采样与日志保留，供事后审计（详见 device_service.delete_device 说明）。

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `device_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X DELETE 'http://127.0.0.1:8011/api/v1/devices/{device_id}' \
  -H 'Authorization: Bearer $TOKEN'
```

### 五.9 `GET /api/v1/devices/{device_id}/collect-logs`

**设备采集日志** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

某台设备的采集记录（排查"为什么这台采不到数据"）

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `device_id` | 整数 |  |

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | 整数 | 否 | `1` |  |
| `page_size` | 整数 | 否 | `20` |  |
| `status` | 字符串 | 否 |  | success/fail/timeout |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `total` | 整数 | 否 | 总记录数 |
| `page` | 整数 | 否 | 当前页码，从 1 开始 |
| `page_size` | 整数 | 否 | 每页条数 |
| `pages` | 整数 | 否 | 总页数 |
| `items` | 对象[] | 否 | 当前页数据 |
| `items[].id` | 整数 | 是 | 日志 ID |
| `items[].device_id` | 整数 | 是 | 设备 ID |
| `items[].device_name` | 字符串 | 否 | 设备名称 |
| `items[].device_ip` | 字符串 | 否 | 设备 IP |
| `items[].protocol` | 字符串 | 否 | 采集协议 |
| `items[].status` | 字符串 | 是 | success 成功 / fail 失败 / timeout 超时 |
| `items[].duration_ms` | 整数 | 否 | 耗时（毫秒） |
| `items[].error_msg` | 字符串 | 否 | 失败原因 |
| `items[].metric_count` | 整数 | 否 | 本次采集到的指标数 |
| `items[].collect_time` | 字符串 | 否 | 采集时间 |
| `items[].status_text` | 字符串 | 是 | 状态中文名 |
| `items[].duration_text` | 字符串 | 是 | 耗时展示，如 42ms / 1.3s |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/devices/{device_id}/collect-logs' \
  -H 'Authorization: Bearer $TOKEN'
```

### 五.10 `GET /api/v1/devices/{device_id}/collect-stat`

**设备采集健康度** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

近 N 小时采集成功率与平均耗时（设备详情页顶部指标卡）

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `device_id` | 整数 |  |

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `hours` | 整数 | 否 | `24` |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/devices/{device_id}/collect-stat' \
  -H 'Authorization: Bearer $TOKEN'
```

### 五.11 `GET /api/v1/devices/{device_id}/history`

**设备指标历史曲线** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

历史曲线数据。

跨度大时自动降采样（≤6 小时原始点、≤4 天 5 分钟均值、更长取小时均值），
避免一次返回十万个点把浏览器卡死。

⚠ **为什么补了 `hours` 这个参数**
原先本接口只有 start/end，而调用方绝大多数需求是"看最近 N 小时"，
每次都要自己算发送资料再拼进 URL。更隐蔽的问题是：
**FastAPI 会静默忽略未声明的 query 参数** —— 于是有人写了 `?hours=2`，
服务器当没看见、按默认 6 小时窗口返回，既不报错也不符合预期。

这次就是这样踩到的：回归脚本里写了 `hours=2`，实际根本没生效，
只是因为演示数据恰好在 6 小时窗口内，连续多轮都"通过"了。

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `device_id` | 整数 |  |

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `metric_key` | 字符串 | 是 |  | 指标键，如 cpu_usage |
| `start` | 日期时间 | 否 |  | 起始时间；与 hours 二选一，同时给出则以本参数为准 |
| `end` | 日期时间 | 否 |  | 结束时间；不传则取当前时间 |
| `hours` | 整数 | 否 |  | 最近 N 小时（1-8760，即一年）；与告警趋势等接口统一使用“最近 N 小时”这一种时间窗口写法 |
| `limit` | 整数 | 否 | `500` |  |
| `agg` | 字符串 | 否 |  | raw/5min/hour，不传则自动选择 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `device_id` | 整数 | 是 | 设备 ID |
| `device_name` | 字符串 | 否 | 设备名称 |
| `metric_key` | 字符串 | 是 | 指标键 |
| `metric_name` | 字符串 | 否 | 指标中文名 |
| `unit` | 字符串 | 否 | 单位 |
| `count` | 整数 | 否 | 采样点数量 |
| `points` | 对象[] | 否 | 数据点序列 |
| `points[].time` | 字符串 | 是 | 时间，格式 YYYY-MM-DD HH:MM:SS |
| `points[].value` | 数值 | 否 |  |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/devices/{device_id}/history' \
  -H 'Authorization: Bearer $TOKEN'
```

### 五.12 `GET /api/v1/devices/{device_id}/metrics`

**设备实时指标** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

设备当前全部指标最新值（设备详情页"实时指标"卡片）

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `device_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `device_id` | 整数 | 是 |  |
| `device_name` | 字符串 | 否 |  |
| `device_ip` | 字符串 | 否 |  |
| `online_status` | 字符串 | 否 |  |
| `last_collect_at` | 字符串 | 否 |  |
| `metrics` | 对象[] | 否 |  |
| `metrics[].device_id` | 整数 | 是 | 设备 ID |
| `metrics[].metric_key` | 字符串 | 是 | 指标键 |
| `metrics[].metric_value` | 数值 | 否 | 数值型指标值 |
| `metrics[].str_value` | 字符串 | 否 | 字符串型指标值 |
| `metrics[].unit` | 字符串 | 否 | 单位 |
| `metrics[].collect_time` | 字符串 | 否 | 采集时间 |
| `metrics[].metric_name` | 字符串 | 否 | 指标中文名 |
| `metrics[].category` | 字符串 | 否 | cpu/memory/disk/interface/ping |
| `metrics[].display_value` | 字符串 | 是 | 展示值：字符串指标优先，否则用数值 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/devices/{device_id}/metrics' \
  -H 'Authorization: Bearer $TOKEN'
```

### 五.13 `POST /api/v1/devices/{device_id}/test`

**测试连接** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

测试设备连通性与协议可用性。

走的是正式采集器（同一套代码路径），因此"测试通过"等价于"采集能成功"。
测试结果**不写入指标表**，避免污染真实监控曲线。

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `device_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `success` | 布尔 | 是 | 是否连通成功 |
| `message` | 字符串 | 是 | 结果说明；失败时给出可排查的原因，如“SNMP 团体名错误” |
| `duration_ms` | 整数 | 否 | 耗时（毫秒） |
| `metric_count` | 整数 | 否 | 本次试采到的指标条数；为 0 说明凭据对但指标不支持 |
| `details` | 字符串[] | 否 | 采集到的指标摘要，便于确认凭据正确 |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/devices/{device_id}/test' \
  -H 'Authorization: Bearer $TOKEN'
```

### 五.14 `POST /api/v1/devices/{device_id}/toggle`

**启用/停用采集任务** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/devices.py`

启用或停用该设备的采集任务（需求：支持启用/停用采集任务）。

停用后调度器立即跳过该设备，历史数据与告警记录不受影响。

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `device_id` | 整数 |  |

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `enabled` | 布尔 | 是 |  | true 启用 / false 停用 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 设备 ID |
| `name` | 字符串 | 否 | 设备名称 |
| `ip` | 字符串 | 否 | 管理 IP |
| `device_type` | 字符串 | 否 | switch/router/firewall/server/storage/camera/access/ap/ups/other |
| `vendor` | 字符串 | 否 | 厂商标识，如 huawei/h3c/cisco/ruijie/sangfor/hikvision；前端展示的中文名由 DeviceOut.vendor_text 提供 |
| `os_type` | 字符串 | 否 | 操作系统/固件描述，如 CentOS 7.9 / Windows Server 2019 |
| `protocol` | 字符串 | 否 | 采集协议：snmp/ssh/winrm/icmp，取值见 /api/v1/devices/protocols |
| `model` | 字符串 | 否 | 设备型号 |
| `location` | 字符串 | 否 | 物理位置，如 3号楼机房A柜 |
| `collect_interval` | 整数 | 否 | 采集周期（秒），最小 10 秒 |
| `timeout` | 整数 | 否 | 采集超时（秒） |
| `enabled` | 布尔 | 否 | 是否启用采集；false 时大屏显示“停用”而不是“在线” |
| `remark` | 字符串 | 否 | 备注 |
| `online_status` | 字符串 | 否 | 在线状态：online/offline/unknown |
| `fail_count` | 整数 | 否 | 连续采集失败次数；达到阈值后判为离线 |
| `last_collect_at` | 字符串 | 否 | 最近一次采集时间 |
| `last_online_at` | 字符串 | 否 | 最近一次在线时间 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `protocol_text` | 字符串 | 是 | 协议中文名 |
| `device_type_text` | 字符串 | 是 | 设备类型中文名 |
| `online_text` | 字符串 | 是 | 在线状态中文名 |
| `vendor_text` | 字符串 | 是 | 厂商中文名（未登记的品牌原样透出） |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/devices/{device_id}/toggle' \
  -H 'Authorization: Bearer $TOKEN'
```

---

## 六、资产管理（10 个接口）

> 对应需求：模块2 IT 资产管理

### 六.1 `GET /api/v1/assets`

**资产列表** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/assets.py`

资产列表（含绑定监控设备的在线状态，实现"资产与实时监控数据关联"）

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | 整数 | 否 | `1` |  |
| `page_size` | 整数 | 否 | `20` |  |
| `keyword` | 字符串 | 否 |  | 编号/名称/IP/序列号/责任人模糊搜索 |
| `status` | 字符串 | 否 |  |  |
| `device_type` | 字符串 | 否 |  |  |
| `room_location` | 字符串 | 否 |  |  |
| `monitored_only` | 布尔 | 否 | `False` | 仅看已绑定监控设备的资产 |
| `warranty_warning_days` | 整数 | 否 |  | 仅看 N 天内维保到期的资产 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `total` | 整数 | 否 | 总记录数 |
| `page` | 整数 | 否 | 当前页码，从 1 开始 |
| `page_size` | 整数 | 否 | 每页条数 |
| `pages` | 整数 | 否 | 总页数 |
| `items` | 对象[] | 否 | 当前页数据 |
| `items[].id` | 整数 | 是 | 资产 ID |
| `items[].asset_code` | 字符串 | 否 | 资产编号（唯一） |
| `items[].name` | 字符串 | 否 | 设备名称 |
| `items[].device_type` | 字符串 | 否 | 资产类型，取值见 /api/v1/devices/types |
| `items[].vendor` | 字符串 | 否 | 厂商 |
| `items[].model` | 字符串 | 否 | 型号 |
| `items[].serial_no` | 字符串 | 否 | 序列号 |
| `items[].ip` | 字符串 | 否 | 管理 IP |
| `items[].mac` | 字符串 | 否 | MAC 地址 |
| `items[].room_location` | 字符串 | 否 | 机房位置 |
| `items[].rack_position` | 字符串 | 否 | 机柜/机位 |
| `items[].online_at` | 字符串 | 否 | 上架时间 |
| `items[].warranty_start` | 字符串 | 否 | 维保起始 |
| `items[].warranty_end` | 字符串 | 否 | 维保到期时间；用于首页维保预警 |
| `items[].owner` | 字符串 | 否 | 责任人 |
| `items[].owner_phone` | 字符串 | 否 | 责任人电话 |
| `items[].supplier` | 字符串 | 否 | 供应商 |
| `items[].purchase_at` | 字符串 | 否 | 采购日期 |
| `items[].purchase_price` | 数值 | 否 | 采购金额（元） |
| `items[].status` | 字符串 | 否 | in_use/idle/repair/scrapped |
| `items[].remark` | 字符串 | 否 | 备注 |
| `items[].device_id` | 整数 | 否 | 已绑定的监控设备 ID；未绑定为 null |
| `items[].created_at` | 字符串 | 否 | 创建时间 |
| `items[].updated_at` | 字符串 | 否 | 最后修改时间 |
| `items[].device_name` | 字符串 | 否 | 绑定设备名称 |
| `items[].device_online_status` | 字符串 | 否 | 绑定设备在线状态 |
| `items[].status_text` | 字符串 | 是 | 状态中文名 |
| `items[].is_monitored` | 布尔 | 是 | 是否已绑定监控设备 |
| `items[].warranty_days_left` | 整数 | 是 | 距维保到期天数，负数表示已过期 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/assets' \
  -H 'Authorization: Bearer $TOKEN'
```

### 六.2 `POST /api/v1/assets`

**新增资产（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/assets.py`

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `asset_code` | 字符串 | 是 | 资产编号（唯一） |
| `name` | 字符串 | 是 | 设备名称 |
| `device_type` | 字符串 | 否 | 资产类型，取值见 /api/v1/devices/types |
| `vendor` | 字符串 | 否 | 厂商 |
| `model` | 字符串 | 否 | 型号 |
| `serial_no` | 字符串 | 否 | 序列号 |
| `ip` | 字符串 | 否 | 管理 IP |
| `mac` | 字符串 | 否 | MAC 地址 |
| `room_location` | 字符串 | 否 | 机房位置 |
| `rack_position` | 字符串 | 否 | 机柜/机位 |
| `online_at` | 日期时间 | 否 | 上架时间 |
| `warranty_start` | 日期时间 | 否 | 维保起始 |
| `warranty_end` | 日期时间 | 否 | 维保到期时间；用于首页维保预警 |
| `owner` | 字符串 | 否 | 责任人 |
| `owner_phone` | 字符串 | 否 | 责任人电话 |
| `supplier` | 字符串 | 否 | 供应商 |
| `purchase_at` | 日期时间 | 否 | 采购日期 |
| `purchase_price` | 数值 | 否 | 采购金额（元） |
| `status` | 字符串 | 否 | in_use/idle/repair/scrapped |
| `remark` | 字符串 | 否 | 备注 |
| `device_id` | 整数 | 否 | 可同时绑定监控设备 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 资产 ID |
| `asset_code` | 字符串 | 否 | 资产编号（唯一） |
| `name` | 字符串 | 否 | 设备名称 |
| `device_type` | 字符串 | 否 | 资产类型，取值见 /api/v1/devices/types |
| `vendor` | 字符串 | 否 | 厂商 |
| `model` | 字符串 | 否 | 型号 |
| `serial_no` | 字符串 | 否 | 序列号 |
| `ip` | 字符串 | 否 | 管理 IP |
| `mac` | 字符串 | 否 | MAC 地址 |
| `room_location` | 字符串 | 否 | 机房位置 |
| `rack_position` | 字符串 | 否 | 机柜/机位 |
| `online_at` | 字符串 | 否 | 上架时间 |
| `warranty_start` | 字符串 | 否 | 维保起始 |
| `warranty_end` | 字符串 | 否 | 维保到期时间；用于首页维保预警 |
| `owner` | 字符串 | 否 | 责任人 |
| `owner_phone` | 字符串 | 否 | 责任人电话 |
| `supplier` | 字符串 | 否 | 供应商 |
| `purchase_at` | 字符串 | 否 | 采购日期 |
| `purchase_price` | 数值 | 否 | 采购金额（元） |
| `status` | 字符串 | 否 | in_use/idle/repair/scrapped |
| `remark` | 字符串 | 否 | 备注 |
| `device_id` | 整数 | 否 | 已绑定的监控设备 ID；未绑定为 null |
| `created_at` | 字符串 | 否 | 创建时间 |
| `updated_at` | 字符串 | 否 | 最后修改时间 |
| `device_name` | 字符串 | 否 | 绑定设备名称 |
| `device_online_status` | 字符串 | 否 | 绑定设备在线状态 |
| `status_text` | 字符串 | 是 | 状态中文名 |
| `is_monitored` | 布尔 | 是 | 是否已绑定监控设备 |
| `warranty_days_left` | 整数 | 是 | 距维保到期天数，负数表示已过期 |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/assets' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 六.3 `GET /api/v1/assets/export`

**导出资产台账** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/assets.py`

按当前筛选条件导出全部资产（不分页）

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `keyword` | 字符串 | 否 |  |  |
| `status` | 字符串 | 否 |  |  |
| `device_type` | 字符串 | 否 |  |  |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/assets/export' \
  -H 'Authorization: Bearer $TOKEN'
```

### 六.4 `POST /api/v1/assets/import`

**导入资产（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/assets.py`

从 Excel 导入资产。

导入策略为"按资产编号 UPSERT"：编号已存在则更新，不存在则新增，
并用 updated / created 两个计数明确告知本次操作结果。
单行失败不影响其它行，错误明细在 errors 中返回。

**请求体**（`multipart/form-data`，文件上传）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | 字符串 | 是 | 按模板填写的 xlsx 文件 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `total` | 整数 | 否 | Excel 中读取到的数据行数 |
| `created` | 整数 | 否 | 新增条数 |
| `updated` | 整数 | 否 | 按资产编号更新条数 |
| `failed` | 整数 | 否 | 失败条数 |
| `errors` | 字符串[] | 否 | 逐行错误说明（最多返回 50 条） |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/assets/import' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 六.5 `GET /api/v1/assets/maintain-remind`

**维保到期提醒列表** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/assets.py`

维保到期提醒（大屏"容量视图"与后台列表共用）。

数据来自 asset_maintain_remind 表，由每日 08:00 的定时任务刷新。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `limit` | 整数 | 否 | `50` |  |
| `only_pending` | 布尔 | 否 | `False` | 仅看未处理的提醒 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `[].id` | 整数 | 是 | 提醒记录 ID |
| `[].asset_id` | 整数 | 是 | 资产 ID |
| `[].asset_code` | 字符串 | 否 | 资产编号 |
| `[].asset_name` | 字符串 | 否 | 资产名称 |
| `[].warranty_end` | 字符串 | 否 | 维保到期日期 |
| `[].remind_days` | 整数 | 否 | 距到期天数；负数表示已过期 |
| `[].remind_level` | 字符串 | 否 | 提醒级别 |
| `[].remind_status` | 字符串 | 否 | 提醒状态 |
| `[].owner` | 字符串 | 否 | 责任人（Service 补充） |
| `[].owner_phone` | 字符串 | 否 | 责任人电话 |
| `[].remind_level_text` | 字符串 | 是 | 提醒等级中文名 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/assets/maintain-remind' \
  -H 'Authorization: Bearer $TOKEN'
```

### 六.6 `GET /api/v1/assets/template`

**下载资产导入模板** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/assets.py`

下载 Excel 导入模板（含表头、示例行、填写说明页）。

先给模板再导入，是避免"列名不匹配导致整批失败"最有效的手段。

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/assets/template' \
  -H 'Authorization: Bearer $TOKEN'
```

### 六.7 `GET /api/v1/assets/{asset_id}`

**资产详情** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/assets.py`

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `asset_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 资产 ID |
| `asset_code` | 字符串 | 否 | 资产编号（唯一） |
| `name` | 字符串 | 否 | 设备名称 |
| `device_type` | 字符串 | 否 | 资产类型，取值见 /api/v1/devices/types |
| `vendor` | 字符串 | 否 | 厂商 |
| `model` | 字符串 | 否 | 型号 |
| `serial_no` | 字符串 | 否 | 序列号 |
| `ip` | 字符串 | 否 | 管理 IP |
| `mac` | 字符串 | 否 | MAC 地址 |
| `room_location` | 字符串 | 否 | 机房位置 |
| `rack_position` | 字符串 | 否 | 机柜/机位 |
| `online_at` | 字符串 | 否 | 上架时间 |
| `warranty_start` | 字符串 | 否 | 维保起始 |
| `warranty_end` | 字符串 | 否 | 维保到期时间；用于首页维保预警 |
| `owner` | 字符串 | 否 | 责任人 |
| `owner_phone` | 字符串 | 否 | 责任人电话 |
| `supplier` | 字符串 | 否 | 供应商 |
| `purchase_at` | 字符串 | 否 | 采购日期 |
| `purchase_price` | 数值 | 否 | 采购金额（元） |
| `status` | 字符串 | 否 | in_use/idle/repair/scrapped |
| `remark` | 字符串 | 否 | 备注 |
| `device_id` | 整数 | 否 | 已绑定的监控设备 ID；未绑定为 null |
| `created_at` | 字符串 | 否 | 创建时间 |
| `updated_at` | 字符串 | 否 | 最后修改时间 |
| `device_name` | 字符串 | 否 | 绑定设备名称 |
| `device_online_status` | 字符串 | 否 | 绑定设备在线状态 |
| `status_text` | 字符串 | 是 | 状态中文名 |
| `is_monitored` | 布尔 | 是 | 是否已绑定监控设备 |
| `warranty_days_left` | 整数 | 是 | 距维保到期天数，负数表示已过期 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/assets/{asset_id}' \
  -H 'Authorization: Bearer $TOKEN'
```

### 六.8 `PUT /api/v1/assets/{asset_id}`

**编辑资产（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/assets.py`

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `asset_id` | 整数 |  |

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `asset_code` | 字符串 | 否 | 资产编号，全平台唯一 |
| `name` | 字符串 | 否 | 资产名称 |
| `device_type` | 字符串 | 否 | 资产类型，取值见 /api/v1/assets/types |
| `vendor` | 字符串 | 否 | 厂商 |
| `model` | 字符串 | 否 | 型号 |
| `serial_no` | 字符串 | 否 | 序列号（SN） |
| `ip` | 字符串 | 否 | 管理 IP |
| `mac` | 字符串 | 否 | MAC 地址 |
| `room_location` | 字符串 | 否 | 所在机房/位置 |
| `rack_position` | 字符串 | 否 | 机柜与 U 位 |
| `online_at` | 日期时间 | 否 | 上线日期 |
| `warranty_start` | 日期时间 | 否 | 维保开始日期 |
| `warranty_end` | 日期时间 | 否 | 维保到期日期；用于首页维保预警 |
| `owner` | 字符串 | 否 | 责任人 |
| `owner_phone` | 字符串 | 否 | 责任人电话 |
| `supplier` | 字符串 | 否 | 供应商 |
| `purchase_at` | 日期时间 | 否 | 采购日期 |
| `purchase_price` | 数值 | 否 | 采购金额（元） |
| `status` | 字符串 | 否 | 资产状态，取值同 AssetBase：in_use/idle/repair/scrapped |
| `device_id` | 整数 | 否 | 绑定的监控设备 ID；解绑时传 null |
| `remark` | 字符串 | 否 | 备注 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 资产 ID |
| `asset_code` | 字符串 | 否 | 资产编号（唯一） |
| `name` | 字符串 | 否 | 设备名称 |
| `device_type` | 字符串 | 否 | 资产类型，取值见 /api/v1/devices/types |
| `vendor` | 字符串 | 否 | 厂商 |
| `model` | 字符串 | 否 | 型号 |
| `serial_no` | 字符串 | 否 | 序列号 |
| `ip` | 字符串 | 否 | 管理 IP |
| `mac` | 字符串 | 否 | MAC 地址 |
| `room_location` | 字符串 | 否 | 机房位置 |
| `rack_position` | 字符串 | 否 | 机柜/机位 |
| `online_at` | 字符串 | 否 | 上架时间 |
| `warranty_start` | 字符串 | 否 | 维保起始 |
| `warranty_end` | 字符串 | 否 | 维保到期时间；用于首页维保预警 |
| `owner` | 字符串 | 否 | 责任人 |
| `owner_phone` | 字符串 | 否 | 责任人电话 |
| `supplier` | 字符串 | 否 | 供应商 |
| `purchase_at` | 字符串 | 否 | 采购日期 |
| `purchase_price` | 数值 | 否 | 采购金额（元） |
| `status` | 字符串 | 否 | in_use/idle/repair/scrapped |
| `remark` | 字符串 | 否 | 备注 |
| `device_id` | 整数 | 否 | 已绑定的监控设备 ID；未绑定为 null |
| `created_at` | 字符串 | 否 | 创建时间 |
| `updated_at` | 字符串 | 否 | 最后修改时间 |
| `device_name` | 字符串 | 否 | 绑定设备名称 |
| `device_online_status` | 字符串 | 否 | 绑定设备在线状态 |
| `status_text` | 字符串 | 是 | 状态中文名 |
| `is_monitored` | 布尔 | 是 | 是否已绑定监控设备 |
| `warranty_days_left` | 整数 | 是 | 距维保到期天数，负数表示已过期 |

**示例**

```bash
curl -X PUT 'http://127.0.0.1:8011/api/v1/assets/{asset_id}' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 六.9 `DELETE /api/v1/assets/{asset_id}`

**删除资产（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/assets.py`

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `asset_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X DELETE 'http://127.0.0.1:8011/api/v1/assets/{asset_id}' \
  -H 'Authorization: Bearer $TOKEN'
```

### 六.10 `POST /api/v1/assets/{asset_id}/bind`

**绑定/解绑监控设备** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/assets.py`

资产与监控设备绑定（device_id 传 null 表示解绑）。

绑定后可在资产详情查看该设备的实时指标与历史曲线，
这是"资产台账"与"监控数据"打通的唯一入口。

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `asset_id` | 整数 |  |

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `device_id` | 整数 | 否 | 传 null 表示解绑 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 资产 ID |
| `asset_code` | 字符串 | 否 | 资产编号（唯一） |
| `name` | 字符串 | 否 | 设备名称 |
| `device_type` | 字符串 | 否 | 资产类型，取值见 /api/v1/devices/types |
| `vendor` | 字符串 | 否 | 厂商 |
| `model` | 字符串 | 否 | 型号 |
| `serial_no` | 字符串 | 否 | 序列号 |
| `ip` | 字符串 | 否 | 管理 IP |
| `mac` | 字符串 | 否 | MAC 地址 |
| `room_location` | 字符串 | 否 | 机房位置 |
| `rack_position` | 字符串 | 否 | 机柜/机位 |
| `online_at` | 字符串 | 否 | 上架时间 |
| `warranty_start` | 字符串 | 否 | 维保起始 |
| `warranty_end` | 字符串 | 否 | 维保到期时间；用于首页维保预警 |
| `owner` | 字符串 | 否 | 责任人 |
| `owner_phone` | 字符串 | 否 | 责任人电话 |
| `supplier` | 字符串 | 否 | 供应商 |
| `purchase_at` | 字符串 | 否 | 采购日期 |
| `purchase_price` | 数值 | 否 | 采购金额（元） |
| `status` | 字符串 | 否 | in_use/idle/repair/scrapped |
| `remark` | 字符串 | 否 | 备注 |
| `device_id` | 整数 | 否 | 已绑定的监控设备 ID；未绑定为 null |
| `created_at` | 字符串 | 否 | 创建时间 |
| `updated_at` | 字符串 | 否 | 最后修改时间 |
| `device_name` | 字符串 | 否 | 绑定设备名称 |
| `device_online_status` | 字符串 | 否 | 绑定设备在线状态 |
| `status_text` | 字符串 | 是 | 状态中文名 |
| `is_monitored` | 布尔 | 是 | 是否已绑定监控设备 |
| `warranty_days_left` | 整数 | 是 | 距维保到期天数，负数表示已过期 |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/assets/{asset_id}/bind' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

---

## 七、指标管理（6 个接口）

> 对应需求：模块3 监控指标与自定义告警

### 七.1 `GET /api/v1/metrics/catalog`

**指标字典列表** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/metrics.py`

指标字典（前端指标选择器、告警规则配置页的数据源）。

新增采集指标只需在此登记一行元数据（中文名/单位/分类），无需改表结构。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `enabled_only` | 布尔 | 否 | `False` | 仅返回已启用的指标 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `[].id` | 整数 | 是 | ID |
| `[].metric_key` | 字符串 | 是 | 指标键 |
| `[].metric_name` | 字符串 | 是 | 指标中文名 |
| `[].unit` | 字符串 | 否 | 单位 |
| `[].metric_type` | 字符串 | 否 | gauge/counter/state |
| `[].value_type` | 字符串 | 否 | float/int/string |
| `[].protocol` | 字符串 | 否 | 适用协议 |
| `[].category` | 字符串 | 否 | 分类 |
| `[].is_builtin` | 布尔 | 否 | 是否内置（内置项不可删除） |
| `[].enabled` | 布尔 | 否 | 是否启用 |
| `[].sort_order` | 整数 | 否 | 排序号 |
| `[].remark` | 字符串 | 否 | 备注 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/metrics/catalog' \
  -H 'Authorization: Bearer $TOKEN'
```

### 七.2 `POST /api/v1/metrics/catalog`

**新增自定义指标（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/metrics.py`

登记自定义指标元数据。

注意：这里只登记"元数据"，真正采集到该指标还需要在采集器解析器中
产出对应的 metric_key（元数据可随时加，解析逻辑需发版）。

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `metric_key` | 字符串 | 是 | 指标键，仅小写字母数字下划线 |
| `metric_name` | 字符串 | 是 | 指标中文名，用于界面展示 |
| `unit` | 字符串 | 否 | 单位，如 % / ms / MB/s / ℃ |
| `metric_type` | 字符串 | 否 | gauge/counter/state |
| `value_type` | 字符串 | 否 | float/int/string |
| `protocol` | 字符串 | 否 | 适用的采集协议；为空表示通用 |
| `category` | 字符串 | 否 | 分类：cpu/mem/disk/net/system |
| `sort_order` | 整数 | 否 | 排序号，小的在前 |
| `remark` | 字符串 | 否 | 备注 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | ID |
| `metric_key` | 字符串 | 是 | 指标键 |
| `metric_name` | 字符串 | 是 | 指标中文名 |
| `unit` | 字符串 | 否 | 单位 |
| `metric_type` | 字符串 | 否 | gauge/counter/state |
| `value_type` | 字符串 | 否 | float/int/string |
| `protocol` | 字符串 | 否 | 适用协议 |
| `category` | 字符串 | 否 | 分类 |
| `is_builtin` | 布尔 | 否 | 是否内置（内置项不可删除） |
| `enabled` | 布尔 | 否 | 是否启用 |
| `sort_order` | 整数 | 否 | 排序号 |
| `remark` | 字符串 | 否 | 备注 |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/metrics/catalog' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 七.3 `DELETE /api/v1/metrics/catalog/{catalog_id}`

**删除自定义指标（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/metrics.py`

内置指标不可删除（大屏与告警规则已引用），如需隐藏请停用

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `catalog_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X DELETE 'http://127.0.0.1:8011/api/v1/metrics/catalog/{catalog_id}' \
  -H 'Authorization: Bearer $TOKEN'
```

### 七.4 `GET /api/v1/metrics/history`

**查询历史曲线** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/metrics.py`

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `device_id` | 整数 | 是 |  | 设备 ID |
| `metric_key` | 字符串 | 是 |  | 指标键 |
| `start` | 日期时间 | 否 |  |  |
| `end` | 日期时间 | 否 |  |  |
| `limit` | 整数 | 否 | `500` |  |
| `agg` | 字符串 | 否 |  | raw/5min/hour，不传自动选择 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `device_id` | 整数 | 是 | 设备 ID |
| `device_name` | 字符串 | 否 | 设备名称 |
| `metric_key` | 字符串 | 是 | 指标键 |
| `metric_name` | 字符串 | 否 | 指标中文名 |
| `unit` | 字符串 | 否 | 单位 |
| `count` | 整数 | 否 | 采样点数量 |
| `points` | 对象[] | 否 | 数据点序列 |
| `points[].time` | 字符串 | 是 | 时间，格式 YYYY-MM-DD HH:MM:SS |
| `points[].value` | 数值 | 否 |  |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/metrics/history' \
  -H 'Authorization: Bearer $TOKEN'
```

### 七.5 `GET /api/v1/metrics/history/multi`

**查询多条历史曲线** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/metrics.py`

一次取多条曲线（设备详情页同时画 CPU / 内存 / 磁盘 趋势）。

比前端并发发起 3 个请求更省连接，也保证三条曲线的时间轴一致。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `device_id` | 整数 | 是 |  | 设备 ID |
| `metric_keys` | 字符串[] | 是 |  | 指标键列表 |
| `start` | 日期时间 | 否 |  |  |
| `end` | 日期时间 | 否 |  |  |
| `limit` | 整数 | 否 | `300` |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `[].device_id` | 整数 | 是 | 设备 ID |
| `[].device_name` | 字符串 | 否 | 设备名称 |
| `[].metric_key` | 字符串 | 是 | 指标键 |
| `[].metric_name` | 字符串 | 否 | 指标中文名 |
| `[].unit` | 字符串 | 否 | 单位 |
| `[].count` | 整数 | 否 | 采样点数量 |
| `[].points` | 对象[] | 否 | 数据点序列 |
| `[].points[].time` | 字符串 | 是 | 时间，格式 YYYY-MM-DD HH:MM:SS |
| `[].points[].value` | 数值 | 否 |  |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/metrics/history/multi' \
  -H 'Authorization: Bearer $TOKEN'
```

### 七.6 `GET /api/v1/metrics/latest`

**批量取最新指标** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/metrics.py`

批量取多台设备的最新指标。

大屏与设备列表页都用这一接口，一次请求拿全所需数据，
避免前端对 200 台设备发起 200 次请求。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `device_ids` | 整数[] | 是 |  | 设备 ID 列表，可重复传参 |
| `metric_keys` | 字符串[] | 否 |  | 指标键列表，不传则取全部 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `[].device_id` | 整数 | 是 | 设备 ID |
| `[].metric_key` | 字符串 | 是 | 指标键 |
| `[].metric_value` | 数值 | 否 | 数值型指标值 |
| `[].str_value` | 字符串 | 否 | 字符串型指标值 |
| `[].unit` | 字符串 | 否 | 单位 |
| `[].collect_time` | 字符串 | 否 | 采集时间 |
| `[].metric_name` | 字符串 | 否 | 指标中文名 |
| `[].category` | 字符串 | 否 | cpu/memory/disk/interface/ping |
| `[].display_value` | 字符串 | 是 | 展示值：字符串指标优先，否则用数值 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/metrics/latest' \
  -H 'Authorization: Bearer $TOKEN'
```

---

## 八、告警管理（15 个接口）

> 对应需求：模块3 监控指标与自定义告警

### 八.1 `GET /api/v1/alerts/events`

**告警事件列表** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

告警中心主列表（告警列表、已处理/未处理筛选、告警历史）

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | 整数 | 否 | `1` |  |
| `page_size` | 整数 | 否 | `20` |  |
| `level` | 字符串 | 否 |  |  |
| `status` | 字符串 | 否 |  | firing/acked/recovered/closed |
| `device_id` | 整数 | 否 |  |  |
| `keyword` | 字符串 | 否 |  | 设备名/IP/标题模糊搜索 |
| `start` | 日期时间 | 否 |  |  |
| `end` | 日期时间 | 否 |  |  |
| `only_unhandled` | 布尔 | 否 | `False` | 只看未处理（告警中+已确认） |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `total` | 整数 | 否 | 总记录数 |
| `page` | 整数 | 否 | 当前页码，从 1 开始 |
| `page_size` | 整数 | 否 | 每页条数 |
| `pages` | 整数 | 否 | 总页数 |
| `items` | 对象[] | 否 | 当前页数据 |
| `items[].id` | 整数 | 是 | 事件 ID |
| `items[].event_no` | 字符串 | 否 | 事件编号，用于人工沟通时引用 |
| `items[].rule_id` | 整数 | 否 | 触发该事件的规则 ID |
| `items[].device_id` | 整数 | 是 | 设备 ID |
| `items[].device_name` | 字符串 | 否 | 设备名称 |
| `items[].device_ip` | 字符串 | 否 | 设备 IP |
| `items[].metric_key` | 字符串 | 否 | 超标指标键 |
| `items[].metric_name` | 字符串 | 否 | 超标指标中文名 |
| `items[].metric_value` | 数值 | 否 | 触发时刻的实测值 |
| `items[].threshold` | 数值 | 否 | 规则阈值 |
| `items[].operator` | 字符串 | 否 | 比较符：gt/ge/lt/le/eq/ne |
| `items[].level` | 字符串 | 是 | 级别：critical/major/minor/info |
| `items[].title` | 字符串 | 否 | 告警标题 |
| `items[].content` | 字符串 | 否 | 告警正文，推送时直接使用 |
| `items[].fingerprint` | 字符串 | 否 | 去重指纹 = md5(device_id + rule_id)；同一设备同一规则只会有一条未恢复事件 |
| `items[].status` | 字符串 | 否 | 状态：firing 告警中 / acked 已确认 / recovered 已恢复 / closed 已关闭 |
| `items[].occur_count` | 整数 | 否 | 累计触发次数；同一事件再次触发时 +1，不新增事件 |
| `items[].first_occur_at` | 字符串 | 否 | 首次发生时间 |
| `items[].last_occur_at` | 字符串 | 否 | 最近一次发生时间 |
| `items[].recover_at` | 字符串 | 否 | 恢复时间；未恢复为 null |
| `items[].ack_at` | 字符串 | 否 | 确认时间 |
| `items[].ack_user` | 字符串 | 否 | 确认人 |
| `items[].ack_remark` | 字符串 | 否 | 确认备注 |
| `items[].rule_name` | 字符串 | 否 | 所属规则名称（Service 补充） |
| `items[].level_text` | 字符串 | 是 | 级别中文名 |
| `items[].status_text` | 字符串 | 是 | 状态中文名 |
| `items[].level_color` | 字符串 | 是 | 级别配色，前端直接用 |
| `items[].duration_text` | 字符串 | 是 | 持续时间描述，如 已持续 2小时13分 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/alerts/events' \
  -H 'Authorization: Bearer $TOKEN'
```

### 八.2 `POST /api/v1/alerts/events/batch-ack`

**批量确认告警** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

批量确认（值班交接时一次处理多条）。

允许查看员操作：一线值班人员需要能标记"已看到"，这是他们的日常工作。
但查看员不能关闭事件（关闭属于管理动作，见 /events/{id}/ack 的 close 参数）。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `remark` | 字符串 | 否 |  | 处理备注 |

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ids` | 整数[] | 否 | ID 列表 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `success` | 整数 | 否 | 成功条数 |
| `failed` | 整数 | 否 | 失败条数 |
| `messages` | 字符串[] | 否 | 失败原因列表 |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/alerts/events/batch-ack' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 八.3 `GET /api/v1/alerts/events/{event_id}`

**告警详情** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `event_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 事件 ID |
| `event_no` | 字符串 | 否 | 事件编号，用于人工沟通时引用 |
| `rule_id` | 整数 | 否 | 触发该事件的规则 ID |
| `device_id` | 整数 | 是 | 设备 ID |
| `device_name` | 字符串 | 否 | 设备名称 |
| `device_ip` | 字符串 | 否 | 设备 IP |
| `metric_key` | 字符串 | 否 | 超标指标键 |
| `metric_name` | 字符串 | 否 | 超标指标中文名 |
| `metric_value` | 数值 | 否 | 触发时刻的实测值 |
| `threshold` | 数值 | 否 | 规则阈值 |
| `operator` | 字符串 | 否 | 比较符：gt/ge/lt/le/eq/ne |
| `level` | 字符串 | 是 | 级别：critical/major/minor/info |
| `title` | 字符串 | 否 | 告警标题 |
| `content` | 字符串 | 否 | 告警正文，推送时直接使用 |
| `fingerprint` | 字符串 | 否 | 去重指纹 = md5(device_id + rule_id)；同一设备同一规则只会有一条未恢复事件 |
| `status` | 字符串 | 否 | 状态：firing 告警中 / acked 已确认 / recovered 已恢复 / closed 已关闭 |
| `occur_count` | 整数 | 否 | 累计触发次数；同一事件再次触发时 +1，不新增事件 |
| `first_occur_at` | 字符串 | 否 | 首次发生时间 |
| `last_occur_at` | 字符串 | 否 | 最近一次发生时间 |
| `recover_at` | 字符串 | 否 | 恢复时间；未恢复为 null |
| `ack_at` | 字符串 | 否 | 确认时间 |
| `ack_user` | 字符串 | 否 | 确认人 |
| `ack_remark` | 字符串 | 否 | 确认备注 |
| `rule_name` | 字符串 | 否 | 所属规则名称（Service 补充） |
| `level_text` | 字符串 | 是 | 级别中文名 |
| `status_text` | 字符串 | 是 | 状态中文名 |
| `level_color` | 字符串 | 是 | 级别配色，前端直接用 |
| `duration_text` | 字符串 | 是 | 持续时间描述，如 已持续 2小时13分 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/alerts/events/{event_id}' \
  -H 'Authorization: Bearer $TOKEN'
```

### 八.4 `POST /api/v1/alerts/events/{event_id}/ack`

**确认/关闭告警** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

手动标记已确认（需求：手动标记已确认）。

- close=false：标记为"已确认"，正在处理，设备恢复前不再重复推送
- close=true ：直接关闭（误报、或已人工处理完毕）

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `event_id` | 整数 |  |

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `remark` | 字符串 | 否 | 处理备注 |
| `close` | 布尔 | 否 | 确认同时关闭事件（适用已人工处理完毕的情况） |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 事件 ID |
| `event_no` | 字符串 | 否 | 事件编号，用于人工沟通时引用 |
| `rule_id` | 整数 | 否 | 触发该事件的规则 ID |
| `device_id` | 整数 | 是 | 设备 ID |
| `device_name` | 字符串 | 否 | 设备名称 |
| `device_ip` | 字符串 | 否 | 设备 IP |
| `metric_key` | 字符串 | 否 | 超标指标键 |
| `metric_name` | 字符串 | 否 | 超标指标中文名 |
| `metric_value` | 数值 | 否 | 触发时刻的实测值 |
| `threshold` | 数值 | 否 | 规则阈值 |
| `operator` | 字符串 | 否 | 比较符：gt/ge/lt/le/eq/ne |
| `level` | 字符串 | 是 | 级别：critical/major/minor/info |
| `title` | 字符串 | 否 | 告警标题 |
| `content` | 字符串 | 否 | 告警正文，推送时直接使用 |
| `fingerprint` | 字符串 | 否 | 去重指纹 = md5(device_id + rule_id)；同一设备同一规则只会有一条未恢复事件 |
| `status` | 字符串 | 否 | 状态：firing 告警中 / acked 已确认 / recovered 已恢复 / closed 已关闭 |
| `occur_count` | 整数 | 否 | 累计触发次数；同一事件再次触发时 +1，不新增事件 |
| `first_occur_at` | 字符串 | 否 | 首次发生时间 |
| `last_occur_at` | 字符串 | 否 | 最近一次发生时间 |
| `recover_at` | 字符串 | 否 | 恢复时间；未恢复为 null |
| `ack_at` | 字符串 | 否 | 确认时间 |
| `ack_user` | 字符串 | 否 | 确认人 |
| `ack_remark` | 字符串 | 否 | 确认备注 |
| `rule_name` | 字符串 | 否 | 所属规则名称（Service 补充） |
| `level_text` | 字符串 | 是 | 级别中文名 |
| `status_text` | 字符串 | 是 | 状态中文名 |
| `level_color` | 字符串 | 是 | 级别配色，前端直接用 |
| `duration_text` | 字符串 | 是 | 持续时间描述，如 已持续 2小时13分 |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/alerts/events/{event_id}/ack' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 八.5 `GET /api/v1/alerts/events/{event_id}/notify-records`

**告警通知记录** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

各渠道推送结果（邮件成功 / 企微失败 等）。

用途：出现"怎么没收到告警"时，先看这里定位是哪个渠道没送到，
而不是盲目怀疑平台。

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `event_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `[].id` | 整数 | 是 | 记录 ID |
| `[].event_id` | 整数 | 是 | 所属告警事件 ID |
| `[].channel` | 字符串 | 是 | 渠道：email/wecom/dingtalk/wechat |
| `[].target` | 字符串 | 否 | 推送目标（邮箱地址 / 群机器人 URL 摘要） |
| `[].status` | 字符串 | 否 | pending 待发送 / success 已送出 / failed 发送失败 |
| `[].retry_count` | 整数 | 否 | 已重试次数 |
| `[].response_msg` | 字符串 | 否 | 渠道返回的原始信息或错误原因；排障第一现场 |
| `[].sent_at` | 字符串 | 否 | 实际发送时间 |
| `[].channel_text` | 字符串 | 是 | 渠道中文名 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/alerts/events/{event_id}/notify-records' \
  -H 'Authorization: Bearer $TOKEN'
```

### 八.6 `GET /api/v1/alerts/events/{event_id}/timeline`

**告警处理时间线** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

触发 → 通知 → 确认 → 恢复 全过程留痕（告警详情页时间线）

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `event_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `[].id` | 整数 | 是 | 日志 ID |
| `[].event_id` | 整数 | 否 | 关联告警事件 ID |
| `[].device_id` | 整数 | 否 | 关联设备 ID |
| `[].action` | 字符串 | 是 | 动作：trigger/notify/recover/ack/converge/close/suppress |
| `[].channel` | 字符串 | 否 | 通知动作涉及的渠道 |
| `[].level` | 字符串 | 否 | 级别 |
| `[].message` | 字符串 | 否 | 日志内容 |
| `[].operator` | 字符串 | 否 | 操作人；系统自动动作为 system |
| `[].created_at` | 字符串 | 否 | 发生时间 |
| `[].action_text` | 字符串 | 是 | 动作中文名 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/alerts/events/{event_id}/timeline' \
  -H 'Authorization: Bearer $TOKEN'
```

### 八.7 `GET /api/v1/alerts/latest`

**最新告警（大屏滚动字幕）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

最新未恢复告警。

只返回 firing / acked 状态：已恢复的告警留在滚动条上会让现场领导误判"还在故障"。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `limit` | 整数 | 否 | `20` |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `[].id` | 整数 | 是 | 事件 ID |
| `[].event_no` | 字符串 | 否 | 事件编号，用于人工沟通时引用 |
| `[].rule_id` | 整数 | 否 | 触发该事件的规则 ID |
| `[].device_id` | 整数 | 是 | 设备 ID |
| `[].device_name` | 字符串 | 否 | 设备名称 |
| `[].device_ip` | 字符串 | 否 | 设备 IP |
| `[].metric_key` | 字符串 | 否 | 超标指标键 |
| `[].metric_name` | 字符串 | 否 | 超标指标中文名 |
| `[].metric_value` | 数值 | 否 | 触发时刻的实测值 |
| `[].threshold` | 数值 | 否 | 规则阈值 |
| `[].operator` | 字符串 | 否 | 比较符：gt/ge/lt/le/eq/ne |
| `[].level` | 字符串 | 是 | 级别：critical/major/minor/info |
| `[].title` | 字符串 | 否 | 告警标题 |
| `[].content` | 字符串 | 否 | 告警正文，推送时直接使用 |
| `[].fingerprint` | 字符串 | 否 | 去重指纹 = md5(device_id + rule_id)；同一设备同一规则只会有一条未恢复事件 |
| `[].status` | 字符串 | 否 | 状态：firing 告警中 / acked 已确认 / recovered 已恢复 / closed 已关闭 |
| `[].occur_count` | 整数 | 否 | 累计触发次数；同一事件再次触发时 +1，不新增事件 |
| `[].first_occur_at` | 字符串 | 否 | 首次发生时间 |
| `[].last_occur_at` | 字符串 | 否 | 最近一次发生时间 |
| `[].recover_at` | 字符串 | 否 | 恢复时间；未恢复为 null |
| `[].ack_at` | 字符串 | 否 | 确认时间 |
| `[].ack_user` | 字符串 | 否 | 确认人 |
| `[].ack_remark` | 字符串 | 否 | 确认备注 |
| `[].rule_name` | 字符串 | 否 | 所属规则名称（Service 补充） |
| `[].level_text` | 字符串 | 是 | 级别中文名 |
| `[].status_text` | 字符串 | 是 | 状态中文名 |
| `[].level_color` | 字符串 | 是 | 级别配色，前端直接用 |
| `[].duration_text` | 字符串 | 是 | 持续时间描述，如 已持续 2小时13分 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/alerts/latest' \
  -H 'Authorization: Bearer $TOKEN'
```

### 八.8 `GET /api/v1/alerts/overview`

**告警总览统计** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

告警总数 / 未处理数 / 今日新增 / 分级统计（大屏顶部卡片 + 告警中心头部）

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `total` | 整数 | 否 | 告警总数 |
| `active` | 整数 | 否 | 未恢复告警数 |
| `today` | 整数 | 否 | 今日新增告警数 |
| `by_level` | 对象 | 否 | 按级别统计 {'critical': 2} |
| `by_status` | 对象 | 否 | 按状态统计 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/alerts/overview' \
  -H 'Authorization: Bearer $TOKEN'
```

### 八.9 `GET /api/v1/alerts/rules`

**告警规则列表** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

规则列表。

返回结果附带 firing_device_count（该规则当前触发中的设备数），
运维一眼就能看出"哪条规则最容易报"，便于反过来调阈值。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | 整数 | 否 | `1` |  |
| `page_size` | 整数 | 否 | `20` |  |
| `keyword` | 字符串 | 否 |  |  |
| `device_id` | 整数 | 否 |  | 按设备筛选 |
| `level` | 字符串 | 否 |  | critical/major/minor/info |
| `enabled` | 布尔 | 否 |  |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `total` | 整数 | 否 | 总记录数 |
| `page` | 整数 | 否 | 当前页码，从 1 开始 |
| `page_size` | 整数 | 否 | 每页条数 |
| `pages` | 整数 | 否 | 总页数 |
| `items` | 对象[] | 否 | 当前页数据 |
| `items[].id` | 整数 | 是 | 规则 ID |
| `items[].name` | 字符串 | 否 | 规则名称 |
| `items[].device_id` | 整数 | 否 | null 表示全局规则 |
| `items[].metric_key` | 字符串 | 否 | 指标键 |
| `items[].operator` | 字符串 | 否 | gt/lt/ge/le/eq/ne |
| `items[].threshold` | 数值 | 否 | 阈值 |
| `items[].duration_n` | 整数 | 否 | 连续 N 次超标才触发 |
| `items[].recovery_n` | 整数 | 否 | 连续 M 次正常才恢复 |
| `items[].level` | 字符串 | 否 | critical/major/minor/info |
| `items[].converge_window` | 整数 | 否 | 收敛窗口（秒），0=不收敛 |
| `items[].converge_max` | 整数 | 否 | 窗口内最大推送条数 |
| `items[].notify_channels` | 字符串[] | 否 | 通知渠道列表 |
| `items[].silence_until` | 字符串 | 否 | 静默截止时间 |
| `items[].enabled` | 布尔 | 否 | 是否启用；false 时规则不再参与评估 |
| `items[].remark` | 字符串 | 否 | 备注 |
| `items[].created_at` | 字符串 | 否 | 创建时间 |
| `items[].updated_at` | 字符串 | 否 | 最后修改时间 |
| `items[].device_name` | 字符串 | 否 | 设备名称，全局规则为“全部设备” |
| `items[].metric_name` | 字符串 | 否 | 指标中文名 |
| `items[].current_status` | 字符串 | 否 | normal/firing/acked/recovered |
| `items[].breach_count` | 整数 | 否 | 当前连续超标次数（未达 duration_n 时不触发） |
| `items[].normal_count` | 整数 | 否 | 当前连续正常次数（达 recovery_n 才恢复） |
| `items[].last_value` | 数值 | 否 | 最近一次评估取到的指标值 |
| `items[].last_eval_time` | 字符串 | 否 | 最近一次评估时间 |
| `items[].firing_device_count` | 整数 | 否 | 该规则当前触发中的设备数 |
| `items[].level_text` | 字符串 | 是 | 级别中文名 |
| `items[].operator_text` | 字符串 | 是 | 比较符中文名 |
| `items[].condition_text` | 字符串 | 是 | 阈值描述，如 CPU使用率 大于 85% |
| `items[].is_silenced` | 布尔 | 是 | 是否处于静默期 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/alerts/rules' \
  -H 'Authorization: Bearer $TOKEN'
```

### 八.10 `POST /api/v1/alerts/rules`

**新增规则（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

新增告警规则。

device_id 留空表示全局规则（对全部设备生效）；
某台设备需要特殊阈值时，再单独建一条绑定该设备的规则。

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | 字符串 | 是 | 规则名称 |
| `device_id` | 整数 | 否 | null 表示全局规则 |
| `metric_key` | 字符串 | 是 | 指标键 |
| `operator` | 字符串 | 否 | gt/lt/ge/le/eq/ne |
| `threshold` | 数值 | 是 | 阈值 |
| `duration_n` | 整数 | 否 | 连续 N 次超标才触发 |
| `recovery_n` | 整数 | 否 | 连续 M 次正常才恢复 |
| `level` | 字符串 | 否 | critical/major/minor/info |
| `converge_window` | 整数 | 否 | 收敛窗口（秒），0=不收敛 |
| `converge_max` | 整数 | 否 | 窗口内最大推送条数 |
| `notify_channels` | 字符串[] | 否 | 通知渠道列表 |
| `silence_until` | 日期时间 | 否 | 静默截止时间 |
| `enabled` | 布尔 | 否 | 是否启用；false 时规则不再参与评估 |
| `remark` | 字符串 | 否 | 备注 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 规则 ID |
| `name` | 字符串 | 否 | 规则名称 |
| `device_id` | 整数 | 否 | null 表示全局规则 |
| `metric_key` | 字符串 | 否 | 指标键 |
| `operator` | 字符串 | 否 | gt/lt/ge/le/eq/ne |
| `threshold` | 数值 | 否 | 阈值 |
| `duration_n` | 整数 | 否 | 连续 N 次超标才触发 |
| `recovery_n` | 整数 | 否 | 连续 M 次正常才恢复 |
| `level` | 字符串 | 否 | critical/major/minor/info |
| `converge_window` | 整数 | 否 | 收敛窗口（秒），0=不收敛 |
| `converge_max` | 整数 | 否 | 窗口内最大推送条数 |
| `notify_channels` | 字符串[] | 否 | 通知渠道列表 |
| `silence_until` | 字符串 | 否 | 静默截止时间 |
| `enabled` | 布尔 | 否 | 是否启用；false 时规则不再参与评估 |
| `remark` | 字符串 | 否 | 备注 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `updated_at` | 字符串 | 否 | 最后修改时间 |
| `device_name` | 字符串 | 否 | 设备名称，全局规则为“全部设备” |
| `metric_name` | 字符串 | 否 | 指标中文名 |
| `current_status` | 字符串 | 否 | normal/firing/acked/recovered |
| `breach_count` | 整数 | 否 | 当前连续超标次数（未达 duration_n 时不触发） |
| `normal_count` | 整数 | 否 | 当前连续正常次数（达 recovery_n 才恢复） |
| `last_value` | 数值 | 否 | 最近一次评估取到的指标值 |
| `last_eval_time` | 字符串 | 否 | 最近一次评估时间 |
| `firing_device_count` | 整数 | 否 | 该规则当前触发中的设备数 |
| `level_text` | 字符串 | 是 | 级别中文名 |
| `operator_text` | 字符串 | 是 | 比较符中文名 |
| `condition_text` | 字符串 | 是 | 阈值描述，如 CPU使用率 大于 85% |
| `is_silenced` | 布尔 | 是 | 是否处于静默期 |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/alerts/rules' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 八.11 `GET /api/v1/alerts/rules/{rule_id}`

**规则详情** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `rule_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 规则 ID |
| `name` | 字符串 | 否 | 规则名称 |
| `device_id` | 整数 | 否 | null 表示全局规则 |
| `metric_key` | 字符串 | 否 | 指标键 |
| `operator` | 字符串 | 否 | gt/lt/ge/le/eq/ne |
| `threshold` | 数值 | 否 | 阈值 |
| `duration_n` | 整数 | 否 | 连续 N 次超标才触发 |
| `recovery_n` | 整数 | 否 | 连续 M 次正常才恢复 |
| `level` | 字符串 | 否 | critical/major/minor/info |
| `converge_window` | 整数 | 否 | 收敛窗口（秒），0=不收敛 |
| `converge_max` | 整数 | 否 | 窗口内最大推送条数 |
| `notify_channels` | 字符串[] | 否 | 通知渠道列表 |
| `silence_until` | 字符串 | 否 | 静默截止时间 |
| `enabled` | 布尔 | 否 | 是否启用；false 时规则不再参与评估 |
| `remark` | 字符串 | 否 | 备注 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `updated_at` | 字符串 | 否 | 最后修改时间 |
| `device_name` | 字符串 | 否 | 设备名称，全局规则为“全部设备” |
| `metric_name` | 字符串 | 否 | 指标中文名 |
| `current_status` | 字符串 | 否 | normal/firing/acked/recovered |
| `breach_count` | 整数 | 否 | 当前连续超标次数（未达 duration_n 时不触发） |
| `normal_count` | 整数 | 否 | 当前连续正常次数（达 recovery_n 才恢复） |
| `last_value` | 数值 | 否 | 最近一次评估取到的指标值 |
| `last_eval_time` | 字符串 | 否 | 最近一次评估时间 |
| `firing_device_count` | 整数 | 否 | 该规则当前触发中的设备数 |
| `level_text` | 字符串 | 是 | 级别中文名 |
| `operator_text` | 字符串 | 是 | 比较符中文名 |
| `condition_text` | 字符串 | 是 | 阈值描述，如 CPU使用率 大于 85% |
| `is_silenced` | 布尔 | 是 | 是否处于静默期 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/alerts/rules/{rule_id}' \
  -H 'Authorization: Bearer $TOKEN'
```

### 八.12 `PUT /api/v1/alerts/rules/{rule_id}`

**编辑规则（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

编辑规则。

注意：修改阈值或比较符会自动重置该规则的连续计数器，
避免"改完阈值立刻触发告警"造成现场误解（详见 alert_service 说明）。

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `rule_id` | 整数 |  |

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | 字符串 | 否 | 规则名称 |
| `device_id` | 整数 | 否 | 适用设备 ID；null 表示全局规则（对所有设备生效） |
| `metric_key` | 字符串 | 否 | 监控指标键，取值见 /api/v1/metrics/catalog |
| `operator` | 字符串 | 否 | 比较符：gt 大于 / ge 大于等于 / lt 小于 / le 小于等于 / eq 等于 / ne 不等于 |
| `threshold` | 数值 | 否 | 阈值；与 operator 组合成判定条件 |
| `duration_n` | 整数 | 否 | 连续多少次超标才触发告警（抖动抑制，避免单点毛刺误报） |
| `recovery_n` | 整数 | 否 | 连续多少次正常才恢复（滞回，避免告警在阈值附近反复） |
| `level` | 字符串 | 否 | 告警级别，取值同 AlertRuleBase：critical/major/minor/info |
| `converge_window` | 整数 | 否 | 收敛窗口（秒）；窗口内同一规则的推送条数受限 |
| `converge_max` | 整数 | 否 | 窗口内最多推送条数，超出部分只落事件不推送 |
| `notify_channels` | 字符串[] | 否 | 通知渠道：email / wecom / dingtalk / wechat，至少选一个 |
| `silence_until` | 日期时间 | 否 | 静默截止时间；到达前只记录事件不推送 |
| `enabled` | 布尔 | 否 | 是否启用；false 时规则不再评估 |
| `remark` | 字符串 | 否 | 备注 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 规则 ID |
| `name` | 字符串 | 否 | 规则名称 |
| `device_id` | 整数 | 否 | null 表示全局规则 |
| `metric_key` | 字符串 | 否 | 指标键 |
| `operator` | 字符串 | 否 | gt/lt/ge/le/eq/ne |
| `threshold` | 数值 | 否 | 阈值 |
| `duration_n` | 整数 | 否 | 连续 N 次超标才触发 |
| `recovery_n` | 整数 | 否 | 连续 M 次正常才恢复 |
| `level` | 字符串 | 否 | critical/major/minor/info |
| `converge_window` | 整数 | 否 | 收敛窗口（秒），0=不收敛 |
| `converge_max` | 整数 | 否 | 窗口内最大推送条数 |
| `notify_channels` | 字符串[] | 否 | 通知渠道列表 |
| `silence_until` | 字符串 | 否 | 静默截止时间 |
| `enabled` | 布尔 | 否 | 是否启用；false 时规则不再参与评估 |
| `remark` | 字符串 | 否 | 备注 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `updated_at` | 字符串 | 否 | 最后修改时间 |
| `device_name` | 字符串 | 否 | 设备名称，全局规则为“全部设备” |
| `metric_name` | 字符串 | 否 | 指标中文名 |
| `current_status` | 字符串 | 否 | normal/firing/acked/recovered |
| `breach_count` | 整数 | 否 | 当前连续超标次数（未达 duration_n 时不触发） |
| `normal_count` | 整数 | 否 | 当前连续正常次数（达 recovery_n 才恢复） |
| `last_value` | 数值 | 否 | 最近一次评估取到的指标值 |
| `last_eval_time` | 字符串 | 否 | 最近一次评估时间 |
| `firing_device_count` | 整数 | 否 | 该规则当前触发中的设备数 |
| `level_text` | 字符串 | 是 | 级别中文名 |
| `operator_text` | 字符串 | 是 | 比较符中文名 |
| `condition_text` | 字符串 | 是 | 阈值描述，如 CPU使用率 大于 85% |
| `is_silenced` | 布尔 | 是 | 是否处于静默期 |

**示例**

```bash
curl -X PUT 'http://127.0.0.1:8011/api/v1/alerts/rules/{rule_id}' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 八.13 `DELETE /api/v1/alerts/rules/{rule_id}`

**删除规则（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

删除规则会同时清理其运行状态；历史告警事件保留供审计

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `rule_id` | 整数 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X DELETE 'http://127.0.0.1:8011/api/v1/alerts/rules/{rule_id}' \
  -H 'Authorization: Bearer $TOKEN'
```

### 八.14 `POST /api/v1/alerts/rules/{rule_id}/silence`

**设置静默期** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

设置静默期（设备检修窗口用）。

静默期内规则仍会评估并更新状态，只是不推送通知；
检修结束后若指标确实异常，会立刻收到告警而不会被吞掉。

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `rule_id` | 整数 |  |

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `minutes` | 整数 | 是 |  | 静默时长（分钟），最长 7 天 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 规则 ID |
| `name` | 字符串 | 否 | 规则名称 |
| `device_id` | 整数 | 否 | null 表示全局规则 |
| `metric_key` | 字符串 | 否 | 指标键 |
| `operator` | 字符串 | 否 | gt/lt/ge/le/eq/ne |
| `threshold` | 数值 | 否 | 阈值 |
| `duration_n` | 整数 | 否 | 连续 N 次超标才触发 |
| `recovery_n` | 整数 | 否 | 连续 M 次正常才恢复 |
| `level` | 字符串 | 否 | critical/major/minor/info |
| `converge_window` | 整数 | 否 | 收敛窗口（秒），0=不收敛 |
| `converge_max` | 整数 | 否 | 窗口内最大推送条数 |
| `notify_channels` | 字符串[] | 否 | 通知渠道列表 |
| `silence_until` | 字符串 | 否 | 静默截止时间 |
| `enabled` | 布尔 | 否 | 是否启用；false 时规则不再参与评估 |
| `remark` | 字符串 | 否 | 备注 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `updated_at` | 字符串 | 否 | 最后修改时间 |
| `device_name` | 字符串 | 否 | 设备名称，全局规则为“全部设备” |
| `metric_name` | 字符串 | 否 | 指标中文名 |
| `current_status` | 字符串 | 否 | normal/firing/acked/recovered |
| `breach_count` | 整数 | 否 | 当前连续超标次数（未达 duration_n 时不触发） |
| `normal_count` | 整数 | 否 | 当前连续正常次数（达 recovery_n 才恢复） |
| `last_value` | 数值 | 否 | 最近一次评估取到的指标值 |
| `last_eval_time` | 字符串 | 否 | 最近一次评估时间 |
| `firing_device_count` | 整数 | 否 | 该规则当前触发中的设备数 |
| `level_text` | 字符串 | 是 | 级别中文名 |
| `operator_text` | 字符串 | 是 | 比较符中文名 |
| `condition_text` | 字符串 | 是 | 阈值描述，如 CPU使用率 大于 85% |
| `is_silenced` | 布尔 | 是 | 是否处于静默期 |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/alerts/rules/{rule_id}/silence' \
  -H 'Authorization: Bearer $TOKEN'
```

### 八.15 `POST /api/v1/alerts/rules/{rule_id}/toggle`

**启停规则** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/alerts.py`

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `rule_id` | 整数 |  |

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `enabled` | 布尔 | 是 |  | true 启用 / false 停用 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | 整数 | 是 | 规则 ID |
| `name` | 字符串 | 否 | 规则名称 |
| `device_id` | 整数 | 否 | null 表示全局规则 |
| `metric_key` | 字符串 | 否 | 指标键 |
| `operator` | 字符串 | 否 | gt/lt/ge/le/eq/ne |
| `threshold` | 数值 | 否 | 阈值 |
| `duration_n` | 整数 | 否 | 连续 N 次超标才触发 |
| `recovery_n` | 整数 | 否 | 连续 M 次正常才恢复 |
| `level` | 字符串 | 否 | critical/major/minor/info |
| `converge_window` | 整数 | 否 | 收敛窗口（秒），0=不收敛 |
| `converge_max` | 整数 | 否 | 窗口内最大推送条数 |
| `notify_channels` | 字符串[] | 否 | 通知渠道列表 |
| `silence_until` | 字符串 | 否 | 静默截止时间 |
| `enabled` | 布尔 | 否 | 是否启用；false 时规则不再参与评估 |
| `remark` | 字符串 | 否 | 备注 |
| `created_at` | 字符串 | 否 | 创建时间 |
| `updated_at` | 字符串 | 否 | 最后修改时间 |
| `device_name` | 字符串 | 否 | 设备名称，全局规则为“全部设备” |
| `metric_name` | 字符串 | 否 | 指标中文名 |
| `current_status` | 字符串 | 否 | normal/firing/acked/recovered |
| `breach_count` | 整数 | 否 | 当前连续超标次数（未达 duration_n 时不触发） |
| `normal_count` | 整数 | 否 | 当前连续正常次数（达 recovery_n 才恢复） |
| `last_value` | 数值 | 否 | 最近一次评估取到的指标值 |
| `last_eval_time` | 字符串 | 否 | 最近一次评估时间 |
| `firing_device_count` | 整数 | 否 | 该规则当前触发中的设备数 |
| `level_text` | 字符串 | 是 | 级别中文名 |
| `operator_text` | 字符串 | 是 | 比较符中文名 |
| `condition_text` | 字符串 | 是 | 阈值描述，如 CPU使用率 大于 85% |
| `is_silenced` | 布尔 | 是 | 是否处于静默期 |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/alerts/rules/{rule_id}/toggle' \
  -H 'Authorization: Bearer $TOKEN'
```

---

## 九、可视化大屏（6 个接口）

> 对应需求：模块4 可视化大屏

### 九.1 `GET /api/v1/dashboard/alert-trend`

**告警趋势（按小时分桶）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/dashboard.py`

近 N 小时告警趋势。

分桶在 Python 里做而不是用 SQL 的 `strftime` / `DATE_FORMAT`：
后者是两种数据库方言，会破坏"SQLite ↔ MySQL 只改 DATABASE_URL"这条约定，
而且这种破坏在 SQLite 上测不出来。详见 `dashboard_service` 的模块说明。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `hours` | 整数 | 否 | `24` | 统计窗口（小时），上限 14 天 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `hours` | 整数 | 否 | 统计窗口（小时） |
| `points` | 对象[] | 否 | 按小时分桶，从早到晚 |
| `points[].time` | 字符串 | 否 | 时间标签，如 14:00 |
| `points[].total` | 整数 | 否 | 该时间点新增告警总数 |
| `points[].critical` | 整数 | 否 | 紧急级别告警数 |
| `points[].major` | 整数 | 否 | 重要级别告警数 |
| `points[].minor` | 整数 | 否 | 一般级别告警数 |
| `points[].info` | 整数 | 否 | 信息级（含未知级别兜底） |
| `points[].recovered` | 整数 | 否 | 该时间点恢复数 |
| `peak` | 整数 | 否 | 单小时最大新增数，前端据此决定 Y 轴上限 |
| `total` | 整数 | 否 | 窗口内新增总数 |
| `recovered` | 整数 | 否 | 窗口内恢复总数 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/dashboard/alert-trend' \
  -H 'Authorization: Bearer $TOKEN'
```

### 九.2 `GET /api/v1/dashboard/bundle`

**整屏打包（首屏一次拿全）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/dashboard.py`

整屏数据打包：`{overview, devices, trend, distribution}`。

大屏首屏只发这一个请求，好处是**要么整屏可用、要么整屏报同一个错**，
不会出现"三块有数据、一块空白"这种让人无法判断是"真没数据"还是"接口挂了"的状态。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `hours` | 整数 | 否 | `24` | 趋势窗口（小时） |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/dashboard/bundle' \
  -H 'Authorization: Bearer $TOKEN'
```

### 九.3 `GET /api/v1/dashboard/devices`

**大屏设备状态灯矩阵** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/dashboard.py`

设备状态灯矩阵（大屏中部）。

返回的 `status` 是**四态**：online / offline / unknown / **disabled**。
注意 disabled 只在显示层存在 —— 停用采集的设备在库里仍是三态之一，
若大屏直接把库里的 online_status 画出来，一台"已停用采集"的设备会显示成"在线"。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `limit` | 整数 | 否 | `200` | 最多返回多少台设备 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `[].id` | 整数 | 是 | 设备 ID |
| `[].name` | 字符串 | 否 | 设备名称 |
| `[].ip` | 字符串 | 否 | 管理 IP |
| `[].device_type` | 字符串 | 否 | 设备类型编码 |
| `[].type_text` | 字符串 | 否 | 设备类型中文 |
| `[].protocol` | 字符串 | 否 | 采集协议编码 |
| `[].protocol_text` | 字符串 | 否 | 采集协议中文 |
| `[].status` | 字符串 | 否 | online/offline/unknown/disabled |
| `[].status_text` | 字符串 | 否 | 在线状态中文 |
| `[].status_color` | 字符串 | 否 | 状态灯颜色（颜色统一由后端给，前端零色值常量） |
| `[].location` | 字符串 | 否 | 物理位置 |
| `[].cpu` | 数值 | 否 | CPU 使用率（%） |
| `[].mem` | 数值 | 否 | 内存使用率（%） |
| `[].latency` | 数值 | 否 | Ping 延迟（ms） |
| `[].last_collect_at` | 字符串 | 否 | 上次采集时间 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/dashboard/devices' \
  -H 'Authorization: Bearer $TOKEN'
```

### 九.4 `GET /api/v1/dashboard/distribution`

**分布统计（环形图）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/dashboard.py`

设备类型 / 资产状态 / 告警级别 / 厂商 分布。

几乎不变，前端 5 分钟刷一次即可 —— 它是台账属性，不是实时指标。

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `device_type` | 对象[] | 否 | 设备类型分布 |
| `device_type[].code` | 字符串 | 否 | 状态编码，如 online |
| `device_type[].text` | 字符串 | 否 | 中文标签，如 在线 |
| `device_type[].count` | 整数 | 否 | 数量 |
| `device_type[].color` | 字符串 | 否 | 建议颜色（主题色板统一由后端给） |
| `asset_status` | 对象[] | 否 | 资产状态分布 |
| `asset_status[].code` | 字符串 | 否 | 状态编码，如 online |
| `asset_status[].text` | 字符串 | 否 | 中文标签，如 在线 |
| `asset_status[].count` | 整数 | 否 | 数量 |
| `asset_status[].color` | 字符串 | 否 | 建议颜色（主题色板统一由后端给） |
| `alert_level` | 对象[] | 否 | 历史告警级别分布 |
| `alert_level[].code` | 字符串 | 否 | 状态编码，如 online |
| `alert_level[].text` | 字符串 | 否 | 中文标签，如 在线 |
| `alert_level[].count` | 整数 | 否 | 数量 |
| `alert_level[].color` | 字符串 | 否 | 建议颜色（主题色板统一由后端给） |
| `vendor` | 对象[] | 否 | 厂商分布（信创适配情况一目了然） |
| `vendor[].code` | 字符串 | 否 | 状态编码，如 online |
| `vendor[].text` | 字符串 | 否 | 中文标签，如 在线 |
| `vendor[].count` | 整数 | 否 | 数量 |
| `vendor[].color` | 字符串 | 否 | 建议颜色（主题色板统一由后端给） |
| `colors` | 对象 | 否 | 编码 → 颜色，统一色板 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/dashboard/distribution' \
  -H 'Authorization: Bearer $TOKEN'
```

### 九.5 `GET /api/v1/dashboard/overview`

**大屏总览（顶部 KPI）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/dashboard.py`

大屏顶部指标卡与状态分布。单次响应约 2 KB，可 10 秒刷新。

返回的每张卡片都已带好 `level` 与 `color`，前端不做任何阈值判断 ——
否则"离线设备该显示红色还是橙色"这件事就会有两处定义。

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kpis` | 对象[] | 否 | 顶部指标卡 |
| `kpis[].key` | 字符串 | 否 | 标识，便于前端固定卡片顺序 |
| `kpis[].label` | 字符串 | 否 | 卡片标题 |
| `kpis[].value` | 数值 | 否 | 主数值 |
| `kpis[].unit` | 字符串 | 否 | 单位 |
| `kpis[].sub` | 字符串 | 否 | 副标题（补充说明，可空） |
| `kpis[].level` | 字符串 | 否 | normal/warning/danger，用于卡片配色 |
| `kpis[].color` | 字符串 | 否 | 建议颜色 |
| `device_total` | 整数 | 否 | 设备总数（含停用） |
| `device_enabled` | 整数 | 否 | 启用采集的设备数（状态统计口径） |
| `device_status` | 对象[] | 否 | 启用设备的在线/离线/未知计数 |
| `device_status[].code` | 字符串 | 否 | 状态编码，如 online |
| `device_status[].text` | 字符串 | 否 | 中文标签，如 在线 |
| `device_status[].count` | 整数 | 否 | 数量 |
| `device_status[].color` | 字符串 | 否 | 建议颜色（主题色板统一由后端给） |
| `alert_total` | 整数 | 否 | 历史告警总数 |
| `alert_active` | 整数 | 否 | 未恢复告警数（firing + acked） |
| `alert_today` | 整数 | 否 | 今日新增告警数 |
| `alert_level` | 对象[] | 否 | 未恢复告警的级别分布 |
| `alert_level[].code` | 字符串 | 否 | 状态编码，如 online |
| `alert_level[].text` | 字符串 | 否 | 中文标签，如 在线 |
| `alert_level[].count` | 整数 | 否 | 数量 |
| `alert_level[].color` | 字符串 | 否 | 建议颜色（主题色板统一由后端给） |
| `asset_total` | 整数 | 否 | 资产总数 |
| `asset_maintain_warning` | 整数 | 否 | 维保即将到期资产数 |
| `collect_success_rate` | 数值 | 否 | 近 24 小时采集成功率（%） |
| `collect_avg_ms` | 数值 | 否 | 近 24 小时平均采集耗时（毫秒） |
| `collect_last_hour` | 整数 | 否 | 近 1 小时采集次数 |
| `server_time` | 字符串 | 否 | 服务器当前时间（大屏时钟基准） |
| `data_fresh_at` | 字符串 | 否 | 最近一次采集完成时间，大屏判断数据是否新鲜 |
| `refresh_seconds` | 整数 | 否 | 建议刷新间隔（秒），由后端告知前端 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/dashboard/overview' \
  -H 'Authorization: Bearer $TOKEN'
```

### 九.6 `GET /api/v1/dashboard/rank`

**指标排行榜（TOP N）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/dashboard.py`

某指标的值最高设备排行（大屏右侧 TOP N）。

端口类指标（如 `if_in_rate`）一台设备只占一行，取该设备**所有端口的最大值**，
并在 `metric_key` 中返回具体端口名。判断规则集中在本接口的实现里，
避免前后端各有一套"多端口怎么合并"的口径。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `metric_key` | 字符串 | 否 | `cpu_usage` | 指标键，如 cpu_usage / ping_latency |
| `limit` | 整数 | 否 | `10` |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `metric_key` | 字符串 | 否 | 请求的指标键 |
| `metric_name` | 字符串 | 否 | 指标中文名 |
| `unit` | 字符串 | 否 | 单位 |
| `items` | 对象[] | 否 | 按值倒序 |
| `items[].device_id` | 整数 | 是 | 设备 ID |
| `items[].device_name` | 字符串 | 否 | 设备名称 |
| `items[].device_ip` | 字符串 | 否 | 设备 IP |
| `items[].metric_key` | 字符串 | 否 | 命中的指标键（可能是端口级，如 if_in_rate:XGE0/0/2） |
| `items[].metric_name` | 字符串 | 否 | 指标中文名 |
| `items[].value` | 数值 | 否 | 指标值 |
| `items[].unit` | 字符串 | 否 | 单位（唯一来源：models/alert.py 的 metric_unit） |
| `items[].level` | 字符串 | 否 | normal/warning/danger，按阈值着色 |
| `items[].color` | 字符串 | 否 | 建议颜色 |
| `empty_reason` | 字符串 | 否 | 无数据时的原因说明（大屏显示友好提示而不是空白） |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/dashboard/rank' \
  -H 'Authorization: Bearer $TOKEN'
```

---

## 10、日志查询（4 个接口）

> 对应需求：模块5 后台管理

### 10.1 `GET /api/v1/logs/alarm`

**告警处理日志** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/logs.py`

告警全链路日志（触发、恢复、确认、通知、收敛）

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | 整数 | 否 | `1` |  |
| `page_size` | 整数 | 否 | `20` |  |
| `device_id` | 整数 | 否 |  |  |
| `action` | 字符串 | 否 |  | trigger/recover/ack/notify/close |
| `start` | 日期时间 | 否 |  | 默认最近 24 小时 |
| `end` | 日期时间 | 否 |  |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `total` | 整数 | 否 | 总记录数 |
| `page` | 整数 | 否 | 当前页码，从 1 开始 |
| `page_size` | 整数 | 否 | 每页条数 |
| `pages` | 整数 | 否 | 总页数 |
| `items` | 对象[] | 否 | 当前页数据 |
| `items[].id` | 整数 | 是 | 日志 ID |
| `items[].event_id` | 整数 | 否 | 关联告警事件 ID |
| `items[].device_id` | 整数 | 否 | 关联设备 ID |
| `items[].action` | 字符串 | 是 | 动作：trigger/notify/recover/ack/converge/close/suppress |
| `items[].channel` | 字符串 | 否 | 通知动作涉及的渠道 |
| `items[].level` | 字符串 | 否 | 级别 |
| `items[].message` | 字符串 | 否 | 日志内容 |
| `items[].operator` | 字符串 | 否 | 操作人；系统自动动作为 system |
| `items[].created_at` | 字符串 | 否 | 发生时间 |
| `items[].action_text` | 字符串 | 是 | 动作中文名 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/logs/alarm' \
  -H 'Authorization: Bearer $TOKEN'
```

### 10.2 `GET /api/v1/logs/collect`

**采集日志** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/logs.py`

采集日志（含耗时与错误原因）。

排查"某台设备为什么采不到数据"的入口：
先按 status=fail 过滤，再看 error_msg 定位是网络、凭据还是协议问题。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | 整数 | 否 | `1` |  |
| `page_size` | 整数 | 否 | `20` |  |
| `device_id` | 整数 | 否 |  |  |
| `status` | 字符串 | 否 |  | success/fail/timeout |
| `protocol` | 字符串 | 否 |  |  |
| `keyword` | 字符串 | 否 |  | 设备名/IP/错误信息 |
| `start` | 日期时间 | 否 |  | 默认最近 24 小时 |
| `end` | 日期时间 | 否 |  |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `total` | 整数 | 否 | 总记录数 |
| `page` | 整数 | 否 | 当前页码，从 1 开始 |
| `page_size` | 整数 | 否 | 每页条数 |
| `pages` | 整数 | 否 | 总页数 |
| `items` | 对象[] | 否 | 当前页数据 |
| `items[].id` | 整数 | 是 | 日志 ID |
| `items[].device_id` | 整数 | 是 | 设备 ID |
| `items[].device_name` | 字符串 | 否 | 设备名称 |
| `items[].device_ip` | 字符串 | 否 | 设备 IP |
| `items[].protocol` | 字符串 | 否 | 采集协议 |
| `items[].status` | 字符串 | 是 | success 成功 / fail 失败 / timeout 超时 |
| `items[].duration_ms` | 整数 | 否 | 耗时（毫秒） |
| `items[].error_msg` | 字符串 | 否 | 失败原因 |
| `items[].metric_count` | 整数 | 否 | 本次采集到的指标数 |
| `items[].collect_time` | 字符串 | 否 | 采集时间 |
| `items[].status_text` | 字符串 | 是 | 状态中文名 |
| `items[].duration_text` | 字符串 | 是 | 耗时展示，如 42ms / 1.3s |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/logs/collect' \
  -H 'Authorization: Bearer $TOKEN'
```

### 10.3 `GET /api/v1/logs/collect-stat`

**采集健康度统计** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/logs.py`

整体采集成功率与平均耗时（采集日志页顶部指标卡）

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `hours` | 整数 | 否 | `24` | 统计最近 N 小时 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `total` | 整数 | 否 | 总采集次数 |
| `success` | 整数 | 否 | 成功次数 |
| `fail` | 整数 | 否 | 失败次数 |
| `timeout` | 整数 | 否 | 超时次数 |
| `success_rate` | 数值 | 否 | 成功率百分比 |
| `avg_duration_ms` | 整数 | 否 | 平均耗时（毫秒） |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/logs/collect-stat' \
  -H 'Authorization: Bearer $TOKEN'
```

### 10.4 `GET /api/v1/logs/system`

**系统日志** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/logs.py`

系统日志（登录记录、操作记录、异常信息）

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | 整数 | 否 | `1` |  |
| `page_size` | 整数 | 否 | `20` |  |
| `level` | 字符串 | 否 |  | INFO/WARNING/ERROR |
| `module` | 字符串 | 否 |  | 来源模块，如 auth/device |
| `keyword` | 字符串 | 否 |  |  |
| `start` | 日期时间 | 否 |  | 默认最近 24 小时 |
| `end` | 日期时间 | 否 |  |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `total` | 整数 | 否 | 总记录数 |
| `page` | 整数 | 否 | 当前页码，从 1 开始 |
| `page_size` | 整数 | 否 | 每页条数 |
| `pages` | 整数 | 否 | 总页数 |
| `items` | 对象[] | 否 | 当前页数据 |
| `items[].id` | 整数 | 是 | 日志 ID |
| `items[].level` | 字符串 | 是 | INFO/WARNING/ERROR |
| `items[].module` | 字符串 | 否 | 模块名 |
| `items[].action` | 字符串 | 否 | 动作名 |
| `items[].message` | 字符串 | 否 | 日志内容 |
| `items[].user_id` | 整数 | 否 | 操作人 ID |
| `items[].username` | 字符串 | 否 | 操作人账号 |
| `items[].ip` | 字符串 | 否 | 来源 IP |
| `items[].created_at` | 字符串 | 否 | 发生时间 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/logs/system' \
  -H 'Authorization: Bearer $TOKEN'
```

---

## 11、系统配置（6 个接口）

> 对应需求：模块5 后台管理

### 11.1 `GET /api/v1/configs`

**获取全部配置（按分组）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/configs.py`

按分组返回全部配置（系统配置页 Tab 数据源）。

敏感项已脱敏为 *** 或空字符串，前端据 is_configured 显示配置状态点。

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `[].group` | 字符串 | 是 | 分组标识 |
| `[].group_text` | 字符串 | 否 | 分组中文名 |
| `[].items` | 对象[] | 否 | 该分组下的配置项 |
| `[].items[].id` | 整数 | 是 | 配置项 ID |
| `[].items[].config_key` | 字符串 | 是 | 配置键，全平台唯一 |
| `[].items[].config_value` | 字符串 | 否 | 配置值；敏感项已替换为 ***，未提供值时为空 |
| `[].items[].config_group` | 字符串 | 是 | 分组标识 |
| `[].items[].value_type` | 字符串 | 否 | 值类型：string/int/bool/json/password |
| `[].items[].is_secret` | 布尔 | 否 | 是否敏感项；为真时出参已脱敏 |
| `[].items[].description` | 字符串 | 否 | 配置项本身的中文说明 |
| `[].items[].updated_at` | 字符串 | 否 | 最后修改时间 |
| `[].items[].is_configured` | 布尔 | 是 | 敏感项是否已配置（前端显示绿点/灰点） |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/configs' \
  -H 'Authorization: Bearer $TOKEN'
```

### 11.2 `PUT /api/v1/configs`

**批量保存配置（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/configs.py`

批量保存配置（前端"保存"按钮一次提交一个分组）。

保存后立即失效进程内配置缓存，新参数无需重启即可生效。

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `items` | 对象[] | 否 | 待更新配置项列表 |
| `items[].config_key` | 字符串 | 是 | 要修改的配置键 |
| `items[].config_value` | 字符串 | 否 | 敏感项传 *** 表示不修改 |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X PUT 'http://127.0.0.1:8011/api/v1/configs' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 11.3 `GET /api/v1/configs/scheduler/status`

**调度器状态与任务列表** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/configs.py`

采集调度器运行状态（系统配置页"定时任务"面板）。

运维最常问的是"采集是不是在跑"，此接口直接给出 running / 任务列表 / 下次执行时间。

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/configs/scheduler/status' \
  -H 'Authorization: Bearer $TOKEN'
```

### 11.4 `POST /api/v1/configs/scheduler/trigger`

**立即执行定时任务（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/configs.py`

立即手动执行某定时任务。

典型场景：改完数据保留策略想立刻清理一次；新接入设备想立刻采集而不是等下一轮。

**查询参数**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `job_id` | 字符串 | 是 |  | 任务 ID：collect_tick / cleanup_data / refresh_maintain_remind |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/configs/scheduler/trigger' \
  -H 'Authorization: Bearer $TOKEN'
```

### 11.5 `POST /api/v1/configs/test`

**测试告警渠道连通性（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/configs.py`

测试告警渠道是否能正常发送。

为什么必须有这个功能：告警渠道配错（Webhook 少一段、SMTP 授权码过期）
在真实故障发生前是完全静默的，等到出事才发现收不到告警就来不及了。
运维配完必须点一次测试。

**请求体**（`application/json`）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `group` | 字符串 | 是 | mail/wecom/dingtalk/wechat |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `success` | 布尔 | 是 | 是否连通成功 |
| `message` | 字符串 | 是 | 结果说明；失败时为可直接展示给人看的原因 |
| `duration_ms` | 整数 | 否 | 耗时（毫秒） |

**示例**

```bash
curl -X POST 'http://127.0.0.1:8011/api/v1/configs/test' \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '<JSON 请求体，字段见上表>'
```

### 11.6 `GET /api/v1/configs/{group}`

**获取指定分组配置** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/configs.py`

取单个分组（collect / mail / wecom / dingtalk / wechat / system）。
分组不存在时返回空列表而非 404，便于前端统一渲染空状态。

**路径参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `group` | 字符串 |  |

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `group` | 字符串 | 是 | 分组标识 |
| `group_text` | 字符串 | 否 | 分组中文名 |
| `items` | 对象[] | 否 | 该分组下的配置项 |
| `items[].id` | 整数 | 是 | 配置项 ID |
| `items[].config_key` | 字符串 | 是 | 配置键，全平台唯一 |
| `items[].config_value` | 字符串 | 否 | 配置值；敏感项已替换为 ***，未提供值时为空 |
| `items[].config_group` | 字符串 | 是 | 分组标识 |
| `items[].value_type` | 字符串 | 否 | 值类型：string/int/bool/json/password |
| `items[].is_secret` | 布尔 | 否 | 是否敏感项；为真时出参已脱敏 |
| `items[].description` | 字符串 | 否 | 配置项本身的中文说明 |
| `items[].updated_at` | 字符串 | 否 | 最后修改时间 |
| `items[].is_configured` | 布尔 | 是 | 敏感项是否已配置（前端显示绿点/灰点） |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/configs/{group}' \
  -H 'Authorization: Bearer $TOKEN'
```

---

## 12、系统信息（3 个接口）

> 对应需求：模块5 后台管理

### 12.1 `GET /api/v1/system/health`

**健康检查（免登录）** ｜ 权限：公开（无需登录） ｜ 定义位置：`app/api/v1/system.py`

健康检查（部署脚本与监控系统调用）。

检查项：数据库连通性 + 调度器状态。
数据库不通时返回 503 语义（status="down"），便于部署脚本判定启动失败。

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/system/health'
```

### 12.2 `GET /api/v1/system/info`

**平台基础信息（免登录）** ｜ 权限：公开（无需登录） ｜ 定义位置：`app/api/v1/system.py`

平台基础信息。

免登录可访问的原因：登录页需要显示平台名称，
大屏页脚与刷新间隔也需要在未登录时（或令牌过期后）仍能获取。
本接口不返回任何敏感数据。

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `platform_name` | 字符串 | 是 | 平台名称 |
| `version` | 字符串 | 是 | 版本号 |
| `company_name` | 字符串 | 是 | 公司名称 |
| `dashboard_refresh` | 整数 | 否 | 大屏自动刷新间隔（秒） |
| `scheduler_running` | 布尔 | 否 | 采集调度器是否在运行 |
| `device_total` | 整数 | 否 | 设备总数 |
| `device_online` | 整数 | 否 | 在线设备数 |
| `alert_active` | 整数 | 否 | 未恢复告警数 |

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/system/info'
```

### 12.3 `GET /api/v1/system/runtime`

**运行状态（仅管理员）** ｜ 权限：登录用户 ｜ 定义位置：`app/api/v1/system.py`

运行状态详情。

包含：采集线程池（并发数/正在采集的设备数）、调度器任务、
采集器已支持协议、以及各类数据的规模统计。
这是"平台到底在不在干活"最直接的答案。

**响应**

统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。

`data` 类型：`任意`

**示例**

```bash
curl -X GET 'http://127.0.0.1:8011/api/v1/system/runtime' \
  -H 'Authorization: Bearer $TOKEN'
```

---


---

## 附录

### A. 重新生成文档

```bash
cd /Users/zhumeizhan/WorkBuddy/Itsm_System
/Users/zhumeizhan/.workbuddy/binaries/python/envs/default/bin/python scripts/gen_api_doc.py

# CI 里校验文档是否与代码同步（不同步则以非零码退出）
/Users/zhumeizhan/.workbuddy/binaries/python/envs/default/bin/python scripts/gen_api_doc.py --check
```

> `--check` 是有意为之的：它把"文档过期"变成一件 CI 能拦住的事，
> 而不是靠人记得执行脚本。脚本之所以能这么判断，正因为输出完全由代码决定 ——
> 同样内容重算一遍必然字节相同。

### B. 在线文档

| 地址 | 内容 |
| --- | --- |
| `/docs` | Swagger UI（可直接在线调试，带"试一下"按钮） |
| `/redoc` | ReDoc（更适合阅读的长文档视图） |
| `/openapi.json` | OpenAPI 3.1 规范 JSON（本文档的数据源） |

### C. 命名约定

| 位置 | 约定 | 示例 |
| --- | --- | --- |
| 路径 | 全小写 + 连字符，资源用复数 | `/api/v1/alert-events` |
| 参数 | snake_case | `page_size`、`metric_key` |
| 布尔字段 | `is_` 前缀 或 形容词 | `enabled`、`is_silenced` |
| 时间字段 | `_at` 后缀 | `created_at`、`last_check_at` |
| 计数后缀 | `_count` | `converge_count` |
| 加密存储字段 | `_enc` 后缀 | `password_enc`（Fernet 对称加密，与登录密码的 bcrypt 单向哈希区分） |
### D. 第 7 步交付清单与发现的问题

**交付物**

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `docs/07-API接口文档.md` | 自动生成 | **本文件**：75 个接口的完整契约 |
| `scripts/gen_api_doc.py` | 约 620 | 从 `FastAPI.openapi()` 生成本文档；`--check` 供 CI 校验同步 |
| `scripts/sync_field_docs.py` | 约 200 | 把字段说明从输入类同步到输出类（省力） |
| `scripts/static_audit.py` | +2 项 | 检查 7（说明漂移）+ 检查 8（脚本假参数） |

同时补齐了 **109 个 Schema、579 个字段**的 `description`，
**字段说明覆盖率从 33.3% 提升到 98.8%**。

**本步发现并修复的问题**

1. **凭印象写枚举，连错三次**（协议多写了 `snmpv3`、角色多写了 `operator`、
   级别把 `major/minor` 写成 `warning/info`）。
   三次都是同一个动作导致的：**在注释/描述里重抄了一遍枚举**。
   抄写的那一刻是对的，但真值表（`VALID_PROTOCOLS` / `VALID_ROLES` / `AlertRuleBase.level`）
   改了它不会跟着变。修法：描述里不再重抄取值，改为指向真值表的位置。

2. **`Resp.data` 的字段全部丢失**（文档里只显示一个"对象"）。
   根因：`data: Optional[T]` 在 OpenAPI 里是 `anyOf: [T, null]`，
   而不处理 `anyOf` 就取不到任何 `properties`。修法：`flatten()` 先拆 `anyOf`/`allOf`/`oneOf`。

3. **回归脚本用了接口根本不存在的参数**（`?hours=2`）。
   FastAPI 会**静默忽略**未声明的 query 参数：请求照样成功，只是参数没生效，
   于是演示数据过期前，这条用例一直在"验证一个不存在的行为"。
   修法：给接口补上 `hours`，并新增**静态检查 8** 守住这一类误用。

4. **回归脚本不可重复执行**：中途中断会留下测试数据，下次 `新增用户` 直接 1001 失败。
   修法：脚本开头加 `purge_previous_run()` 自清理（非 SQLite 环境自动跳过，避免误伤现场库）。

**为什么每个新增检查都要做变异验证**

检查 7、8 都各自注入过人为缺陷并确认它们**真的会报错**。
这一步不能省：一个从未验证过能报警的检查器，和一个从不打印任何内容的检查器，
在 CI 里长得一模一样 —— 都是"全绿"。

更值得注意的是，写伪检查的过程中还出现过**假变异测试**：
第一次验证文档同步守卫时替换字符串没匹配上（改的根本不是目标），
却差点得出"守卫有效"的结论。**变异测试本身也要先确认它真的改到了东西。**
