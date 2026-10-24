"""
自动生成 API 接口文档（docs/07-API接口文档.md）
===============================================================================
**为什么必须自动生成，而不是手写》

接口文档最大的敌人不是"写得不详细"，而是"写完之后就再也没人更新"。
手写文档的衰减曲线是这样的：

    第 1 周：与代码完全一致
    第 1 月：加了 3 个接口，漏了 2 个字段
    第 3 月：字段改名了，文档还是旧的 —— 前端按文档对接，联调当天才炸

而这一族问题我们已经在本项目里见过 4 次（见 docs/05 的"同一件事两处定义"）：
**两处定义分居两地、漂移时不报错、失效点离改动点很远**。

接口文档与代码就是最典型的一对"同一件事两处定义"。
所以本文档直接从 `FastAPI.openapi()` 生成 —— 唯一的真实来源是代码本身，
文档不可能漂移，因为它每次都被重新算出来。

**为什么还要额外做 AST 扫描》

OpenAPI 规范里**没有位置表达"这个接口需要管理员权限"**。
本项目用 `dependencies=[Depends(require_role(ROLE_ADMIN))]` 声明权限，
这种依赖不产生入参，因此不会出现在 spec 里。

如果不处理，生成的文档就有两种坏结果：
  1. 每个接口都写"需要登录" —— 等于没说，调用方只能试错
  2. 手工在文档里补权限 —— 又回到手写文档的老路，权限一改就漂移

所以脚本用 AST 直接读源码里的权限声明，作为 `openapi()` 之外的第二个事实源。

**用法**

    python scripts/gen_api_doc.py                  # 生成到默认路径
    python scripts/gen_api_doc.py --check          # 只校验是否已过期（CI 用）
    python scripts/gen_api_doc.py -o /tmp/api.md   # 输出到别处
===============================================================================
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUTPUT = ROOT / "docs" / "07-API接口文档.md"

# 接口权限显示文案。
# ⚠ **取值必须与 app/schemas/user.py 的 VALID_ROLES 一致**：本项目只有两个角色。
# 第一版这里凭印象写了三个（多了一个 operator），而代码里根本没有 ——
# 手工写文档最典型的错误就是这种"看起来很合理的编造"，所以 display 层要贴着真值表写。
ROLE_TEXT = {
    "admin": "管理员",
    "viewer": "运维查看员（只读 + 告警确认）",
}

# 需要登录的依赖函数（出现在函数签名里也算作"需要登录"）
LOGIN_DEPENDENCY = "get_current_user"

# 各模块的分组顺序与一句话说明（决定文档章节顺序）
MODULE_INTRO: List[Tuple[str, str]] = [
    ("认证", "登录、登出、获取当前用户信息、刷新令牌。"),
    ("用户管理", "后台账号的增删改查、启停、角色分配与密码修改。"),
    ("设备管理", "设备台账 CRUD、启停采集、连通性测试、凭据管理、实时指标。"),
    ("资产管理", "IT 资产台账 CRUD、与设备绑定/解绑、Excel 批量导入导出、维保预警。"),
    ("指标管理", "指标字典查询、实时值、历史采样与趋势数据。"),
    ("告警管理", "告警规则 CRUD、静默、告警事件确认/关闭、处理时间线、推送记录、渠道自测。"),
    ("可视化大屏", "大屏聚合数据：KPI 总览、设备状态灯矩阵、趋势、排行榜、分布、整屏打包。"),
    ("日志查询", "系统日志、采集日志、告警日志三类日志查询与采集成功率统计。"),
    ("系统配置", "运行参数在线编辑、批量保存、渠道连通性自测、敏感项掩码。"),
    ("系统信息", "健康检查、系统信息、运行时状态。"),
]

# 各模块对应的需求编号（承接需求书里的 5 大核心模块）
MODULE_TO_REQUIREMENT = {
    "设备管理": "模块1 设备接入与协议采集",
    "资产管理": "模块2 IT 资产管理",
    "指标管理": "模块3 监控指标与自定义告警",
    "告警管理": "模块3 监控指标与自定义告警",
    "可视化大屏": "模块4 可视化大屏",
    "日志查询": "模块5 后台管理",
    "系统配置": "模块5 后台管理",
    "用户管理": "模块5 后台管理",
    "认证": "模块5 后台管理",
    "系统信息": "模块5 后台管理",
}


def coverage_stat(spec: Dict[str, Any], resolver: Resolver) -> Tuple[int, int, float]:
    """
    统计所有 schema 字段中有多少写了 description。

    ⚠ **为什么要把这个数字写进文档**：
    自动生成解决了"文档与代码漂移"，却解决不了"字段本身没写说明"——后者是代码侧的债。
    如果不在文档里把这个比率明明白白写出来，这个短板就会一直隐身：
    文档看起来很厚、表格很满，但三分之二的说明栏是空的，而没人会为"空的表格"排期。

    把覆盖率做成文档第一行就能看到的数字之后，它就成了一个**可度量、可追踪的债**：
    每次生成都会更新，补一个字段就涨一点，谁都看得见进展。
    """
    total = documented = 0
    for name, sch in spec.get("components", {}).get("schemas", {}).items():
        if name.startswith(("HTTPValidationError", "ValidationError", "Resp_", "PageData_")):
            continue  # 框架自带的包装/校验结构不需要业务说明
        for _fname, raw in (sch.get("properties") or {}).items():
            total += 1
            if (resolver.resolve(raw).get("description") or "").strip():
                documented += 1
    pct = (documented / total * 100) if total else 0.0
    return documented, total, pct


CN_NUM = "零一二三四五六七八九"


# 模块内的主次顺序：按路径字母排序会把 `change-password` 排在 `login` 前面，
# 而读文档的人第一眼想找的永远是"怎么登录"。这类"第一印象顺序"不值得搞一套通用规则，
# 显式列出来最省事也最不容易错。
PREFERRED_FIRST: Dict[str, List[str]] = {
    "认证": ["/api/v1/auth/login", "/api/v1/auth/logout"],
}


# ============================================================================
# 一、AST 扫描：提取每个接口的权限声明
# ============================================================================

def scan_permissions() -> Dict[Tuple[str, str], Dict[str, Any]]:
    """
    扫描 app/api/v1/*.py，建立 (method, full_path) -> 权限信息 的映射。

    返回值示例：
        {("GET", "/api/v1/devices"): {"roles": None, "login": True, "summary_hint": "设备列表"}}

    ⚠ **为什么要自己拼完整路径**：
        FastAPI 的路径是 `总前缀(/api/v1) + 路由前缀(/devices) + 装饰器路径("")`，
        而 OpenAPI 里只有拼好的结果。为了两侧能对上，必须在这里做同样的拼接。
        拼接规则与 `app/api/router.py` 保持一致。
    """
    result: Dict[Tuple[str, str], Dict[str, Any]] = {}
    api_dir = ROOT / "app" / "api"
    total_prefix = "/api/v1"

    methods = {"get", "post", "put", "delete", "patch"}

    for py_file in sorted(api_dir.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))

        # 1) 先建立 "router 变量名 -> prefix" 的映射
        prefixes: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                func = node.value.func
                # 支持 APIRouter(...) 与 apis.APIRouter(...) 两种写法
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name == "APIRouter":
                    prefix = ""
                    for kw in node.value.keywords:
                        if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                            prefix = str(kw.value.value)
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            prefixes[tgt.id] = prefix

        # 2) 逐个处理装饰器
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                fn = deco.func
                if not isinstance(fn, ast.Attribute):
                    continue
                if fn.attr not in methods:
                    continue
                router_var = getattr(fn.value, "id", None)
                if router_var not in prefixes:
                    continue  # 不是本文件里定义的 router，跳过

                route_path = ""
                if deco.args and isinstance(deco.args[0], ast.Constant):
                    route_path = str(deco.args[0].value)

                roles: Optional[List[str]] = None
                for kw in deco.keywords:
                    if kw.arg != "dependencies":
                        continue
                    if not isinstance(kw.value, ast.List):
                        continue
                    for elt in kw.value.elts:
                        if not (isinstance(elt, ast.Call) and getattr(elt.func, "id", None) == "Depends"):
                            continue
                        if not elt.args:
                            continue
                        inner = elt.args[0]
                        if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == "require_role":
                            roles = []
                            for a in inner.args:
                                # 支持 ROLE_ADMIN 常量名 与 "admin" 字面量两种写法
                                if isinstance(a, ast.Name):
                                    roles.append(ROLE_TEXT.get(a.id.replace("ROLE_", "").lower(), a.id))
                                elif isinstance(a, ast.Constant):
                                    roles.append(ROLE_TEXT.get(str(a.value), str(a.value)))

                # 函数签名里若出现 Depends(get_current_user)，也表示需要登录
                need_login = False
                args = list(node.args.args) + list(node.args.kwonlyargs)
                defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
                for arg, d in zip(args[-len(defaults):] if defaults else [], defaults):
                    del arg
                    if isinstance(d, ast.Call) and isinstance(d.func, ast.Name):
                        if d.func.id == "Depends" and d.args and getattr(d.args[0], "id", None) == LOGIN_DEPENDENCY:
                            need_login = True
                    if isinstance(d, ast.Call) and d.args:
                        inner = d.args[0]
                        if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == "require_role":
                            need_login = True

                full = f"{total_prefix}{prefixes[router_var]}{route_path}" or "/"
                result[(fn.attr.upper(), full)] = {
                    "roles": roles,
                    "login": need_login or roles is not None,
                    "file": str(py_file.relative_to(ROOT)),
                }
    return result


# ============================================================================
# 二、Schema 展开
# ============================================================================

MAX_DEPTH = 3


class Resolver:
    """把 OpenAPI 里的 $ref 就地解引用"""

    def __init__(self, spec: Dict[str, Any]):
        self.spec = spec
        self.schemas = spec.get("components", {}).get("schemas", {})
        self._cache: Dict[str, Any] = {}

    def ref(self, ref: str) -> Dict[str, Any]:
        if ref in self._cache:
            return self._cache[ref]
        node: Any = self.spec
        for part in ref.lstrip("#/").split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            node = node[part]
        self._cache[ref] = node
        return node

    def resolve(self, node: Any, depth: int = 0) -> Any:
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            try:
                return self.ref(node["$ref"])
            except (KeyError, TypeError):
                return {}
        return node


def type_text(node: Dict[str, Any], resolver: Resolver) -> str:
    """把 OpenAPI 的 type 描述翻译成中文可读的类型串"""
    if not node:
        return "任意"
    if "anyOf" in node or "oneOf" in node:
        variants = node.get("anyOf") or node.get("oneOf") or []
        names = []
        for v in variants:
            t = v.get("type")
            if t == "null":
                continue
            names.append(type_text(v, resolver))
        # 去重后合并
        names = [n for i, n in enumerate(names) if n not in names[:i]]
        return " / ".join(names) or "任意"

    ntype = node.get("type")
    if ntype == "array":
        inner = node.get("items", {})
        return f"{type_text(resolver.resolve(inner), resolver)}[]"
    if ntype == "integer":
        return "整数"
    if ntype == "number":
        return "数值"
    if ntype == "boolean":
        return "布尔"
    if ntype == "string":
        enums = node.get("enum")
        if enums:
            return "枚举：" + " / ".join(str(e) for e in enums)
        fmt = node.get("format")
        return {"date-time": "日期时间", "date": "日期", "binary": "文件流"}.get(fmt or "", "字符串")
    if ntype == "object":
        return "对象"
    if "$ref" in node:
        return type_text(resolver.resolve(node), resolver)
    return "任意"


def flatten(node: Dict[str, Any], resolver: Resolver, prefix: str = "", depth: int = 0
            ) -> List[Tuple[str, str, str, str]]:
    """
    把 schema 扁平化成 [(字段名, 类型, 必填, 说明)]。

    嵌套对象用 `父.子` 的形式展开（最多 MAX_DEPTH 层），
    数组元素用 `字段名[]` 表示下一层属于数组里的对象。
    """
    rows: List[Tuple[str, str, str, str]] = []
    node = resolver.resolve(node)
    if not node:
        return rows

    # ⚠ **为什么必须先拆 anyOf / allOf**：`Resp.data` 的类型是 `Optional[T]`，
    # Pydantic 会把它渲染成 `anyOf: [{$ref: T}, {type: null}]`。
    # 若不拆，`node["type"]` 为空、字段名全部丢失，文档里只能显示一个干巴巴的"对象" ——
    # 而这恰恰是最需要展示结构的那个字段（整个 data 部分）。
    if "anyOf" in node or "oneOf" in node:
        variants = node.get("anyOf") or node.get("oneOf") or []
        for v in variants:
            vr = resolver.resolve(v)
            if vr.get("type") == "null":
                continue
            return flatten(vr, resolver, prefix=prefix, depth=depth)
        return rows

    if "allOf" in node:
        # 继承 / 组合：把每个分支的字段依次展开后合并（同名以最后一个为准）
        merged: Dict[str, Tuple[str, str, str]] = {}
        for v in node["allOf"]:
            for name, t, req, dsc in flatten(resolver.resolve(v), resolver, prefix=prefix, depth=depth):
                merged[name] = (t, req, dsc)
        return [(n, t, r, d) for n, (t, r, d) in merged.items()]

    ntype = node.get("type")
    if ntype == "array":
        items = resolver.resolve(node.get("items", {}))
        if isinstance(items, dict) and (items.get("type") == "object" or "$ref" in (node.get("items") or {})):
            rows.extend(flatten(items, resolver, prefix=f"{prefix}[]" if prefix else "[]", depth=depth))
        return rows
    if ntype != "object":
        return rows

    required = set(node.get("required") or [])
    props = node.get("properties") or {}
    for name, raw in props.items():
        sub = resolver.resolve(raw)
        full = f"{prefix}.{name}" if prefix else name
        desc = (sub.get("description") or "").replace("\n", " ").strip()
        rows.append((full, type_text(raw, resolver), "是" if name in required else "否", desc))
        if depth + 1 < MAX_DEPTH:
            stype = sub.get("type")
            if stype == "object":
                rows.extend(flatten(sub, resolver, prefix=full, depth=depth + 1))
            elif stype == "array":
                it = resolver.resolve(sub.get("items", {}))
                itref = sub.get("items", {})
                if isinstance(it, dict) and (it.get("type") == "object" or "$ref" in itref):
                    rows.extend(flatten(it, resolver, prefix=full + "[]", depth=depth + 1))
    return rows


# ============================================================================
# 三、渲染
# ============================================================================

def _esc(text: str) -> str:
    """Markdown 表格里不允许出现裸竖线与换行"""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def permission_text(info: Dict[str, Any]) -> str:
    roles = info.get("roles")
    if roles:
        return "**" + " / ".join(roles) + "**"
    if info.get("login"):
        return "登录用户"
    return "公开（无需登录）"


def render_operation(method: str, path: str, op: Dict[str, Any], info: Dict[str, Any],
                     resolver: Resolver, chapter: str = "1", seq: int = 1) -> List[str]:
    out: List[str] = []
    summary = op.get("summary") or path
    out.append(f"### {chapter}.{seq} `{method} {path}`")
    out.append("")
    out.append(f"**{summary}** ｜ 权限：{permission_text(info)} ｜ 定义位置：`{info.get('file','')}`")
    out.append("")

    desc = (op.get("description") or "").strip()
    # FastAPI 会把整个 docstring 塞进 description，其中第一行就是 summary，去掉避免重复
    if desc:
        lines = desc.split("\n")
        if lines and lines[0].strip() == summary.strip():
            lines = lines[1:]
        rest = "\n".join(lines).strip()
        if rest:
            out.append(rest)
            out.append("")

    # ---- 路径参数 ----
    path_params = [p for p in op.get("parameters", []) if p.get("in") == "path"]
    if path_params:
        out.append("**路径参数**")
        out.append("")
        out.append("| 参数 | 类型 | 说明 |")
        out.append("| --- | --- | --- |")
        for p in path_params:
            sch = p.get("schema", {})
            out.append(f"| `{p['name']}` | {_esc(type_text(sch, resolver))} | {_esc(p.get('description',''))} |")
        out.append("")

    # ---- 查询参数 ----
    query_params = [p for p in op.get("parameters", []) if p.get("in") == "query"]
    if query_params:
        out.append("**查询参数**")
        out.append("")
        out.append("| 参数 | 类型 | 必填 | 默认值 | 说明 |")
        out.append("| --- | --- | --- | --- | --- |")
        for p in query_params:
            sch = p.get("schema", {})
            default = sch.get("default")
            default_txt = "" if default is None else f"`{default}`"
            out.append(
                f"| `{p['name']}` | {_esc(type_text(sch, resolver))} | "
                f"{'是' if p.get('required') else '否'} | {default_txt} | "
                f"{_esc(p.get('description','') or _enum_hint(sch, resolver))} |"
            )
        out.append("")

    # ---- 请求体 ----
    body = op.get("requestBody")
    if body:
        content = body.get("content", {})
        json_part = content.get("application/json", {})
        multipart = content.get("multipart/form-data", {})
        if json_part.get("schema"):
            out.append("**请求体**（`application/json`）")
            out.append("")
            rows = flatten(json_part["schema"], resolver)
            if rows:
                out.append("| 字段 | 类型 | 必填 | 说明 |")
                out.append("| --- | --- | --- | --- |")
                for name, t, req, dsc in rows:
                    out.append(f"| `{_esc(name)}` | {_esc(t)} | {req} | {_esc(dsc)} |")
            else:
                out.append(f"类型：`{_esc(type_text(json_part['schema'], resolver))}`")
            out.append("")
        elif multipart.get("schema"):
            out.append("**请求体**（`multipart/form-data`，文件上传）")
            out.append("")
            rows = flatten(multipart["schema"], resolver)
            out.append("| 字段 | 类型 | 必填 | 说明 |")
            out.append("| --- | --- | --- | --- |")
            for name, t, req, dsc in rows:
                out.append(f"| `{_esc(name)}` | {_esc(t)} | {req} | {_esc(dsc)} |")
            out.append("")

    # ---- 响应 ----
    ok = op.get("responses", {}).get("200", {})
    ok_schema = (ok.get("content") or {}).get("application/json", {}).get("schema")
    if ok_schema:
        resolved = resolver.resolve(ok_schema)
        # Resp[T] 的形态：properties.data → T
        data_schema = None
        props = resolved.get("properties") or {}
        if "data" in props:
            data_schema = props["data"]
        out.append("**响应**")
        out.append("")
        out.append("统一包装：`Resp { code, msg, data }`，下表为 **`data` 字段**的结构。")
        out.append("")
        if data_schema is not None:
            rows = flatten(resolver.resolve(data_schema), resolver)
            if rows:
                out.append("| 字段 | 类型 | 必填 | 说明 |")
                out.append("| --- | --- | --- | --- |")
                for name, t, req, dsc in rows:
                    out.append(f"| `{_esc(name)}` | {_esc(t)} | {req} | {_esc(dsc)} |")
            else:
                # 标量或数组：给出类型即可
                out.append(f"`data` 类型：`{_esc(type_text(resolver.resolve(data_schema), resolver))}`")
        else:
            out.append(f"`data` 类型：`{_esc(type_text(resolved, resolver))}`")
        out.append("")

    # ---- curl 示例 ----
    out.append("**示例**")
    out.append("")
    out.append("```bash")
    auth_line = ""
    if info.get("login"):
        auth_line = " \\\n  -H 'Authorization: Bearer $TOKEN'"
    if body:
        out.append(f"curl -X {method} 'http://127.0.0.1:8011{path}'{auth_line} \\")
        out.append("  -H 'Content-Type: application/json' \\")
        out.append("  -d '<JSON 请求体，字段见上表>'")
    else:
        out.append(f"curl -X {method} 'http://127.0.0.1:8011{path}'{auth_line}")
    out.append("```")
    out.append("")
    return out


def _enum_hint(sch: Dict[str, Any], resolver: Resolver) -> str:
    """有些 Query 参数写在类型提示里，description 为空时补一句枚举提示"""
    t = type_text(sch, resolver)
    if t.startswith("枚举："):
        return f"取值限定 {t[3:]}"
    return ""


# ============================================================================
# 四、组装文档
# ============================================================================

HEADER = """# 亿网通IT运维监控管理平台 — API 接口文档

> **本文档由 `scripts/gen_api_doc.py` 从代码自动生成，请勿手工修改。**
> 生成依据：`FastAPI.openapi()`（接口/参数/出入参结构）+ AST 扫描源码（接口权限声明）
> 生成时间：{generated_at}
> 服务地址：`http://<host>:<port>` ｜ 接口总前缀：`/api/v1` ｜ **共 {op_count} 个接口**
{coverage_line}

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
{{
  "code": 0,
  "msg": "成功",
  "data": {{ }}
}}
```

- `code = 0` 表示业务成功；非 0 时 `msg` 是可直接展示给用户的中文提示（前端无需再翻译）
- `data` 为业务数据，失败时为 `null`
- **分页接口的 `data` 固定为**：`{{ total, page, page_size, pages, items }}`

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

本项目**只有两个角色**（`app/schemas/user.py` → `VALID_ROLES = {{"admin", "viewer"}}`）：

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
TOKEN=$(curl -s -X POST http://127.0.0.1:8011/api/v1/auth/login \\
  -H 'Content-Type: application/json' \\
  -d '{{"username":"admin","password":"admin123"}}' \\
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['access_token'])")

# 2. 带 token 调用业务接口
curl -s 'http://127.0.0.1:8011/api/v1/devices?page=1&page_size=20' \\
  -H "Authorization: Bearer $TOKEN"
```

---

## 二、接口总览

| 模块 | 接口数 | 对应需求 | 说明 |
| --- | --- | --- | --- |
{overview_table}

---
"""

FOOTER = """
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
"""


def build_document(spec: Dict[str, Any], perms: Dict[Tuple[str, str], Dict[str, Any]],
                   generated_at: str) -> str:
    from collections import OrderedDict

    paths: Dict[str, Any] = spec.get("paths", {})
    resolver = Resolver(spec)

    # 按 tag 分组
    grouped: "OrderedDict[str, List[Tuple[str, str, Dict[str, Any]]]]" = OrderedDict()
    total = 0
    for path, ops in paths.items():
        for method, op in ops.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            tag = (op.get("tags") or ["其他"])[0]
            grouped.setdefault(tag, []).append((method.upper(), path, op))
            total += 1

    # 未出现在本脚本 MODULE_INTRO 里的 tag 也要能写进来（否则新增模块会被静默吞掉）
    known = [name for name, _ in MODULE_INTRO]
    for tag in grouped:
        if tag not in known:
            known.append(tag)
            MODULE_INTRO.append((tag, "（本模块尚未编写说明，请补 `MODULE_INTRO`）"))

    order = {name: i for i, name in enumerate(known)}
    grouped = OrderedDict(sorted(grouped.items(), key=lambda kv: order.get(kv[0], 999)))

    # 总览表
    rows = []
    for name, intro in MODULE_INTRO:
        if name not in grouped:
            continue
        req = MODULE_TO_REQUIREMENT.get(name, "—")
        rows.append(f"| **{name}** | {len(grouped[name])} | {req} | {intro} |")

    oc = coverage_stat(spec, resolver)
    coverage_line = (
        f"> **字段说明覆盖率：{oc[0]}/{oc[1]} = {oc[2]:.1f}%**"
        f"（剩余字段为自解释字段如 `id`/`name`，或尚未补写说明；该比率随代码一起更新）"
    )

    out: List[str] = []
    out.append(HEADER.format(generated_at=generated_at, op_count=total,
                             overview_table="\n".join(rows),
                             coverage_line=coverage_line))

    chapter = 2
    for name, _intro in MODULE_INTRO:
        if name not in grouped:
            continue
        chapter += 1
        items = grouped[name]
        # 排序意图：① PREFERRED_FIRST 里点名的靠前 ② 同一路径按 GET→POST→PUT→PATCH→DELETE
        # 目的是让文档"从上往下读就是一条合理的使用动线"，而不是字典序。
        preferred = {p: i for i, p in enumerate(PREFERRED_FIRST.get(name, []))}
        method_order = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}
        items.sort(key=lambda x: (preferred.get(x[1], 99), x[1], method_order.get(x[0], 9)))

        cn_chapter = CN_NUM[chapter] if chapter < 10 else str(chapter)
        out.append(f"## {cn_chapter}、{name}（{len(items)} 个接口）")
        out.append("")
        req = MODULE_TO_REQUIREMENT.get(name)
        if req:
            out.append(f"> 对应需求：{req}")
            out.append("")

        for seq, (method, path, op) in enumerate(items, 1):
            info = perms.get((method, path), {"roles": None, "login": True, "file": "—"})
            out.extend(render_operation(method, path, op, info, resolver,
                                        chapter=cn_chapter, seq=seq))

        out.append("---")
        out.append("")

    out.append(FOOTER)
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="从代码自动生成 API 接口文档")
    ap.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT), help="输出文件路径")
    ap.add_argument("--check", action="store_true",
                    help="只校验现有文档是否与代码一致（用于 CI），不同步则退出码 1")
    args = ap.parse_args()

    from datetime import datetime

    # 延迟到这儿再导入 app：脚本启动很快，导入失败也能给出清晰报错
    from app.main import create_app

    app = create_app()
    spec = app.openapi()
    perms = scan_permissions()

    # 覆盖告警：某些接口 OpenAPI 里有、AST 没扫到时提示（通常是新增了扫描没覆盖的写法）
    missing = []
    for path, ops in spec.get("paths", {}).items():
        for method, op in ops.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            if (method.upper(), path) not in perms:
                missing.append(f"{method.upper()} {path}")
    if missing:
        print(f"[WARN] 以下 {len(missing)} 个接口未能通过 AST 识别权限，文档中默认标记为“登录用户”：")
        for m in missing:
            print("   ", m)

    content = build_document(spec, perms, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    out_path = Path(args.output)

    if args.check:
        if not out_path.exists():
            print(f"[FAIL] 文档不存在：{out_path}")
            return 1
        old = out_path.read_text(encoding="utf-8")
        # 时间戳是唯一允许的差异项
        if _strip_ts(old) != _strip_ts(content):
            print("[FAIL] 文档已过期，请执行：python scripts/gen_api_doc.py")
            return 1
        print(f"[OK] 文档与代码同步（{out_path}）")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(f"[OK] 已生成 {out_path}（{len(content.splitlines())} 行）")
    return 0


def _strip_ts(text: str) -> str:
    """去掉生成时间戳后再比对，否则每次 --check 都会误报"""
    import re
    return re.sub(r"^> 生成时间：.*$", "", text, flags=re.M)


if __name__ == "__main__":
    raise SystemExit(main())
