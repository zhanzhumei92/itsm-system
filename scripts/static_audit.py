#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FastAPI 项目静态契约自检脚本（配合 fastapi-contract-audit 技能使用）

实现三项 AST 级检查，专门抓「不报错、只在运行时偶发崩溃」的缺陷：

  检查 2  --check-output-inherit
          输出类（*Out/*Response/*Item/*Detail/*VO）不得继承带
          field_validator / model_validator 的基类。
          原因：Pydantic v2 在校验「输出」时同样执行这些校验器，
               库里一条枚举外的历史脏数据就能让整个列表接口 500。

  检查 3  --check-exceptions
          `raise XxxException(...)` 用到的类名必须在本模块内导入或定义。
          pyflakes 抓不到这类问题（它只报未使用导入，不报未定义名的语义组合）。

  检查 4  --check-routes
          FastAPI 按注册顺序匹配路由。若 `/{param}` 先于同形状的静态路径注册，
          静态路径永远无法命中。判据：同 HTTP 方法 + 同段数 + 仅一段差异
          （一边静态、一边 `{param}`），且参数路由注册在前。

  检查 5  --check-unique-constraints
          ORM 模型（Base.metadata，供 create_all / MySQL 初始化使用）声明的
          UNIQUE 约束，必须与 sql/*.sql 建表脚本里的一致。
          原因：同一个表有两处定义（ORM + 手写 DDL），二者一旦漂移，
                依赖 UNIQUE 的 UPSERT 会静默失效 ——
                SQLite 报错（还能发现），MySQL 不报错、退化为普通 INSERT
                （metric_latest 每轮多一批行，直到磁盘写满才被发现）。

  检查 6  --check-metric-units
          app/models/alert.py 的 METRIC_UNITS（告警通知里显示的单位）必须与
          sql/init_data.sql 的 metric_catalog.unit 逐项一致。
          原因：与检查 5 同源 —— 同一件事有两处定义。少一项不报错，
                只是通知里那个指标没有单位（"温度 78" 而不是 "78℃"），
                而键名写错（登记了不存在的键）会让真正要用的指标反而漏掉单位。
                本检查正是由一次真实事故引入：METRIC_UNITS 里写的是
                "temperature"，而字典里真实的键是 "device_temp"。

用法：
    python scripts/static_audit.py --all
    python scripts/static_audit.py --check-output-inherit --schemas app/schemas
    python scripts/static_audit.py --check-routes --app app.main:create_app

退出码：0 干净 / 1 发现问题（可直接挂 CI）
"""

from __future__ import annotations

import argparse
import ast
import importlib
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 判定规则（可按项目习惯调整）
# ---------------------------------------------------------------------------

# 认为是「输出模型」的类名后缀。这些类的实例会被序列化返回给前端。
OUTPUT_CLASS_RE = re.compile(r"(Out|Response|Item|Detail|VO|View)$")

# 认为「携带校验逻辑」的装饰器名。Pydantic v2 是前两个，v1 是第三个。
VALIDATOR_DECORATORS = {"field_validator", "model_validator", "validator", "root_validator"}


def _iter_py_files(root: Path):
    """遍历目录下所有 .py 文件，跳过 __pycache__ 与虚拟环境"""
    skip = {"__pycache__", ".venv", "venv", "node_modules", ".git"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


# ===========================================================================
# 检查 2：输出类校验器污染
# ===========================================================================

def _decorator_names(node: ast.AST) -> set[str]:
    """
    取出装饰器表达式的「最外层名字」。

    支持三种写法：
        @field_validator("x")            -> field_validator
        @pydantic.field_validator("x")   -> field_validator
        @model_validator(mode="after")   -> model_validator
    """
    names: set[str] = set()
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        names.add(node.attr)
    elif isinstance(node, ast.Name):
        names.add(node.id)
    return names


class _ClassInfo:
    __slots__ = ("name", "file", "lineno", "bases", "has_validator")

    def __init__(self, name, file, lineno, bases, has_validator):
        self.name = name
        self.file = file
        self.lineno = lineno
        self.bases = bases
        self.has_validator = has_validator


def collect_classes(root: Path) -> dict[str, _ClassInfo]:
    """
    扫描目录，建立 {类名: _ClassInfo} 索引。

    说明：同类名跨模块时会互相覆盖，但实际项目里模型类名是唯一的，
    这里为简化不做模块级隔离；若真有重名，两处都会被检查到（宁可多报）。
    """
    classes: dict[str, _ClassInfo] = {}
    for py in _iter_py_files(root):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as e:
            print(f"  [语法错误] {py}:{e.lineno} {e.msg}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    bases.append(b.id)
                elif isinstance(b, ast.Attribute):
                    bases.append(b.attr)
            has_validator = False
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in item.decorator_list:
                        if _decorator_names(dec) & VALIDATOR_DECORATORS:
                            has_validator = True
                            break
            classes[node.name] = _ClassInfo(
                node.name, py, node.lineno, bases, has_validator
            )
    return classes


def check_output_inherit(root: Path) -> int:
    print("【检查 2】输出类不得继承带校验器的基类")
    classes = collect_classes(root)
    problems = []
    for info in classes.values():
        if not OUTPUT_CLASS_RE.search(info.name):
            continue
        if info.has_validator:
            continue  # 自己就是入参类，不查
        # 沿继承链向上找
        seen, stack = set(), list(info.bases)
        while stack:
            base_name = stack.pop()
            if base_name in seen:
                continue
            seen.add(base_name)
            base = classes.get(base_name)
            if base is None:
                continue
            if base.has_validator:
                problems.append((info, base))
                break
            stack.extend(base.bases)

    if not problems:
        print(f"  [OK ] 扫描 {len(classes)} 个类，未发现输出类继承校验器基类")
        return 0
    for out, base in problems:
        rel = out.file.relative_to(root.parent) if root.parent in out.file.parents else out.file
        print(f"  [FAIL] {out.name}（{rel}:{out.lineno}）继承自 {base.name}，"
              f"而 {base.name} 定义了校验器")
        print(f"         -> 修法：把校验器抽成 _XxxInputValidators 由 Create/Update 继承，"
              f"{out.name} 改为独立声明字段")
    return len(problems)


# ===========================================================================
# 检查 3：异常类导入完整性
# ===========================================================================

def check_exceptions(root: Path) -> int:
    print("【检查 3】raise 用到的异常类必须已导入或已定义")
    problems = []
    scanned = 0
    for py in _iter_py_files(root):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError:
            continue
        scanned += 1
        defined: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    defined.add(a.asname or a.name)
            elif isinstance(node, ast.ClassDef):
                defined.add(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        defined.add(t.id)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            exc = node.exc
            if isinstance(exc, ast.Call):
                exc = exc.func
            if not isinstance(exc, ast.Name):
                continue
            name = exc.id
            # 只关心自定义异常：以 Exception / Error 结尾，且首字母大写
            if not (name.endswith("Exception") or name.endswith("Error")):
                continue
            if name in ("Exception", "ValueError", "TypeError", "KeyError",
                        "RuntimeError", "NotImplementedError", "PermissionError",
                        "FileNotFoundError", "OSError", "AttributeError",
                        "IndexError", "StopIteration", "ZeroDivisionError",
                        "ConnectionError", "TimeoutError", "AssertionError"):
                continue
            if name not in defined:
                problems.append((py, node.lineno, name))

    if not problems:
        print(f"  [OK ] 扫描 {scanned} 个模块，异常类引用与导入一一对应")
        return 0
    for py, lineno, name in problems:
        rel = py.relative_to(root.parent) if root.parent in py.parents else py
        print(f"  [FAIL] {rel}:{lineno} raise {name} 但本模块未导入/未定义")
    return len(problems)


# ===========================================================================
# 检查 4：路由遮蔽
# ===========================================================================

def _flatten_routes(routes, prefix: str = ""):
    """
    递归展平路由表，返回 [(完整路径, 方法集合, 注册序号)]。

    为什么需要递归：FastAPI 0.116+ 引入了延迟包含路由（`_IncludedRouter`），
    `app.include_router()` 之后 `app.routes` 里放的是一个占位对象，
    真正的子路由藏在 `include_context.included_router.routes` 里。
    直接遍历 `app.routes` 只会看到少数几条顶层路由（本检查会假通过）。
    对老版本 FastAPI（路由已展平）本函数同样适用。
    """
    out = []
    for r in routes:
        ctx = getattr(r, "include_context", None)
        if ctx is not None and hasattr(ctx, "included_router"):
            out.extend(_flatten_routes(
                ctx.included_router.routes, prefix + (ctx.prefix or "")
            ))
            continue
        path = getattr(r, "path", None)
        if path is None:
            continue  # Mount（静态资源）等无 path 的项
        methods = getattr(r, "methods", None) or set()
        if methods:
            out.append((prefix + path, methods))
    return out


def check_routes(app_spec: str) -> int:
    print(f"【检查 4】路由遮蔽检测（{app_spec}）")
    mod_name, _, attr = app_spec.partition(":")
    attr = attr or "app"
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        print(f"  [SKIP] 无法导入 {mod_name}: {type(e).__name__}: {e}")
        return 0
    obj = getattr(mod, attr)
    # 注意：FastAPI 实例本身是可调用的（ASGI: __call__(scope, receive, send)），
    # 所以不能用 callable() 来判断"是不是工厂函数"，否则会去调应用实例并抛
    # TypeError。正确顺序：先看有没有 routes（已是应用实例），再考虑当工厂调用。
    if hasattr(obj, "routes"):
        app = obj
    elif callable(obj):
        app = obj()
    else:
        app = obj
    routes = getattr(app, "routes", None)
    if routes is None:
        print(f"  [SKIP] {app_spec} 上没有 routes 属性（不是 FastAPI 实例？）")
        return 0

    # 收集 (注册序号, 方法, 段列表, 原始路径)，注册序号即匹配优先级
    entries = []
    for idx, (path, methods) in enumerate(_flatten_routes(routes)):
        if path.startswith(("/openapi", "/docs", "/redoc", "/favicon")):
            continue
        for m in methods:
            if m in ("HEAD", "OPTIONS"):
                continue
            entries.append((idx, m, [s for s in path.split("/") if s], path))

    problems = []
    for i in range(len(entries)):
        for j in range(len(entries)):
            idx_a, m_a, seg_a, path_a = entries[i]
            idx_b, m_b, seg_b, path_b = entries[j]
            if m_a != m_b or len(seg_a) != len(seg_b):
                continue
            # b 必须先注册，才能遮蔽 a
            if idx_b >= idx_a:
                continue
            diff = [k for k in range(len(seg_a)) if seg_a[k] != seg_b[k]]
            if len(diff) != 1:
                continue
            k = diff[0]
            a_is_static = not seg_a[k].startswith("{")
            b_is_param = seg_b[k].startswith("{")
            if a_is_static and b_is_param:
                problems.append((m_a, path_a, path_b))

    if not problems:
        print(f"  [OK ] 共 {len(entries)} 条路由，无遮蔽")
        return 0
    for m, path_a, path_b in problems:
        print(f"  [FAIL] {m} {path_a} 被 {m} {path_b} 遮蔽（参数路由注册在前）")
    return len(problems)


# ===========================================================================
# 检查 5：ORM 唯一约束 与 SQL 建表脚本一致性
# ===========================================================================

# CREATE TABLE xxx ( ... );   —— 非贪婪到行首的 ");" 为止，避免跨表误吞
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+[`\"]?(\w+)[`\"]?\s*\((.*?)^\)\s*;",
    re.S | re.I | re.M,
)
# CREATE UNIQUE INDEX name ON table(col, col);
_CREATE_UNIQUE_INDEX_RE = re.compile(
    r"CREATE\s+UNIQUE\s+INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+[`\"]?(\w+)[`\"]?\s+"
    r"ON\s+[`\"]?(\w+)[`\"]?\s*\(([^)]*)\)",
    re.I,
)
# 列内联 UNIQUE：  username TEXT NOT NULL UNIQUE,
_UNIQUE_COL_RE = re.compile(r"^\s*[`\"]?(\w+)[`\"]?\s+[^,\n]*\bUNIQUE\b", re.I | re.M)
# 表级 UNIQUE：    UNIQUE (a, b)      /  UNIQUE KEY uk_x (a, b)
_TABLE_UNIQUE_RE = re.compile(r"\bUNIQUE\s+(?:KEY\s+[`\"]?\w+[`\"]?\s*)?\(([^)]*)\)", re.I)


def _split_cols(raw: str) -> tuple[str, ...]:
    """把 "(a, b, `c`)" 里的列名清洗成元组，忽略长度/排序等修饰"""
    cols = []
    for part in raw.split(","):
        token = part.strip().strip("`\"")
        token = re.sub(r"\s+(ASC|DESC)$", "", token, flags=re.I)
        if token:
            cols.append(token)
    return tuple(cols)


def collect_sql_unique(sql_path: Path) -> dict[str, set[tuple[str, ...]]]:
    """从建表脚本里提取 {表名: {唯一列组合, ...}}"""
    result: dict[str, set[tuple[str, ...]]] = {}
    if not sql_path.exists():
        return result
    text = sql_path.read_text(encoding="utf-8")

    # 1) CREATE UNIQUE INDEX
    for _name, table, cols in _CREATE_UNIQUE_INDEX_RE.findall(text):
        result.setdefault(table.lower(), set()).add(_split_cols(cols))

    # 2) CREATE TABLE 内的内联/表级 UNIQUE
    for table, body in _CREATE_TABLE_RE.findall(text):
        bucket = result.setdefault(table.lower(), set())
        for cols in _TABLE_UNIQUE_RE.findall(body):
            bucket.add(_split_cols(cols))
        for col in _UNIQUE_COL_RE.findall(body):
            bucket.add((col,))
    return result


def collect_orm_unique(models_root: Path) -> dict[str, set[tuple[str, ...]]]:
    """
    从 ORM 模型里提取 {表名: {唯一列组合, ...}}。

    识别三类写法：
        __table_args__ = (Index("uk_a", "c1", "c2", unique=True),)
        __table_args__ = (UniqueConstraint("c1", "c2", name="uk_a"),)
        mapped_column(String(64), unique=True)      # 单列
    注意：这里用**属性名**当作列名 —— 本项目未使用 name= 改写列名，
    属性名与数据库列名一一对应；若将来引入 name= 映射，本检查需同步升级。
    """
    result: dict[str, set[tuple[str, ...]]] = {}
    for py in _iter_py_files(models_root):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError:
            continue
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            table: str | None = None
            bucket: set[tuple[str, ...]] = set()

            for stmt in cls.body:
                # __tablename__ = "xxx"
                if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                    value = stmt.value
                    names = [t.id for t in targets if isinstance(t, ast.Name)]
                    if "__tablename__" in names and isinstance(value, ast.Constant):
                        table = str(value.value).lower()
                    # __table_args__ = (Index(...), UniqueConstraint(...))
                    if "__table_args__" in names and isinstance(value, ast.Tuple):
                        for elt in value.elts:
                            bucket |= _orm_constraint_cols(elt)
                    # 单列 unique=True
                    if "__tablename__" not in names and len(names) == 1:
                        if _mapped_column_is_unique(stmt):
                            bucket.add((names[0],))

            if table:
                result.setdefault(table, set()).update(bucket)
    return result


def _orm_constraint_cols(node: ast.AST) -> set[tuple[str, ...]]:
    """解析 __table_args__ 里的一个元素（Index 或 UniqueConstraint），返回唯一列组合"""
    if not isinstance(node, ast.Call):
        return set()
    func = node.func
    func_name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")

    if func_name == "Index":
        # Index("name", "col1", "col2", unique=True) —— 只有 unique=True 才算唯一约束
        is_unique = any(
            kw.arg == "unique" and isinstance(kw.value, ast.Constant) and kw.value.value
            for kw in node.keywords
        )
        if not is_unique:
            return set()
        cols = [
            a.value for a in node.args[1:]          # args[0] 是索引名
            if isinstance(a, ast.Constant) and isinstance(a.value, str)
        ]
        return {tuple(cols)} if cols else set()

    if func_name == "UniqueConstraint":
        cols = [
            a.value for a in node.args
            if isinstance(a, ast.Constant) and isinstance(a.value, str)
            # name="uk_x" 是关键字参数，不会出现在 args 里
        ]
        return {tuple(cols)} if cols else set()

    return set()


def _mapped_column_is_unique(stmt: ast.stmt) -> bool:
    """判断 `x: Mapped[...] = mapped_column(..., unique=True)` 是否声明了唯一约束"""
    value = getattr(stmt, "value", None)
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    func_name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if func_name != "mapped_column":
        return False
    return any(
        kw.arg == "unique" and isinstance(kw.value, ast.Constant) and kw.value.value
        for kw in value.keywords
    )


def check_unique_constraints(models_root: Path, sql_path: Path) -> int:
    print(f"【检查 5】ORM 唯一约束 与 {sql_path.name} 保持一致")
    if not sql_path.exists():
        print(f"  [SKIP] 找不到 {sql_path}")
        return 0

    sql_unique = collect_sql_unique(sql_path)
    orm_unique = collect_orm_unique(models_root)
    problems = []

    # ⚠ 必须**双向**比对。
    # 只查"SQL 有、ORM 没有"是不够的：反方向（ORM 有、SQL 没有）同样致命 ——
    # 用 sql/*.sql 建库的现场会缺少该唯一索引，UPSERT 一样失效。
    # 这个疏漏最初就出现在本检查的第一版里：人为删掉 SQL 里的唯一索引后它仍然报 OK，
    # 因为当时只做单向差集。检查器本身也必须被人为缺陷验证过，否则等于摆设。
    for table in sorted(set(sql_unique) | set(orm_unique)):
        sql_cols = sql_unique.get(table, set())
        orm_cols = orm_unique.get(table, set())
        for cols in sorted(sql_cols - orm_cols):
            problems.append((table, cols, "orm_missing"))
        for cols in sorted(orm_cols - sql_cols):
            problems.append((table, cols, "sql_missing"))

    if not problems:
        print(f"  [OK ] 比对 {len(set(sql_unique) | set(orm_unique))} 张表，"
              f"唯一约束在两处定义中一致")
        return 0

    for table, cols, kind in problems:
        col_text = ", ".join(cols)
        if kind == "orm_missing":
            print(f"  [FAIL] 表 {table} 的唯一约束 ({col_text}) 只在 SQL 脚本里，ORM 元数据缺失")
            print(f"         -> 在该模型 __table_args__ 补 "
                  f'Index("<与SQL同名的索引名>", {", ".join(repr(c) for c in cols)}, unique=True)')
            print("            否则：init_db()/create_all（以及 MySQL 初始化）建出的表没有该约束，"
                  "依赖它的 UPSERT 失效")
        else:
            print(f"  [FAIL] 表 {table} 的唯一约束 ({col_text}) 只在 ORM 里，SQL 脚本缺失")
            print("         -> 在 sql/schema_sqlite.sql 与 sql/schema_mysql.sql 补齐对应的 "
                  "UNIQUE 声明（否则用 SQL 脚本建库的现场缺少该约束）")
    return len(problems)


# ===========================================================================
# 检查 6：指标单位表 与 metric_catalog 种子数据一致性
# ===========================================================================

# metric_catalog 的 INSERT 行：
#   ('cpu_usage',      'CPU使用率',     '%',    'gauge',   'float', ...),
# 只取前三列 (metric_key, metric_name, unit) 就够了。
_METRIC_ROW_RE = re.compile(
    r"^\s*\(\s*'([a-z_0-9]+)'\s*,\s*'[^']*'\s*,\s*'([^']*)'\s*,",
    re.M,
)

# METRIC_UNITS 定义所在的源码文件（相对项目根）
_DEFAULT_METRIC_UNITS_FILE = "app/models/alert.py"


def collect_catalog_units(sql_path: Path) -> dict[str, str]:
    """
    从 sql/init_data.sql 的 metric_catalog INSERT 段提取 {metric_key: unit}。

    只解析 `INSERT INTO metric_catalog ... VALUES` 到该语句结束（`;`）之间的行，
    避免把 alert_rule / sys_config 的行误当成指标行
    （它们的第一列也是字符串，但列数结构不同 —— 以 metric_key 必须是
    小写字母数字下划线来兜底过滤）。
    """
    if not sql_path.exists():
        return {}
    text = sql_path.read_text(encoding="utf-8")
    match = re.search(
        r"INSERT\s+INTO\s+metric_catalog\b.*?\bVALUES\b(.*?);",
        text, re.S | re.I,
    )
    if not match:
        return {}
    return {key: unit for key, unit in _METRIC_ROW_RE.findall(match.group(1))}


def collect_metric_units(src_file: Path) -> dict[str, str]:
    """
    从 app/models/alert.py 里 AST 解析出 METRIC_UNITS 字面量。

    用 AST 而不是 import：本脚本要能在"应用依赖没装齐"或"模块 import 会连库"
    的环境下照常运行（这是它作为 CI 前置检查的前提）。
    """
    if not src_file.exists():
        return {}
    tree = ast.parse(src_file.read_text(encoding="utf-8"), filename=str(src_file))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "METRIC_UNITS" for t in node.targets):
            continue
        try:
            data = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            return {}
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    return {}


def check_metric_units(src_file: Path, sql_path: Path) -> int:
    print(f"【检查 6】METRIC_UNITS 与 {sql_path.name} 的 metric_catalog 单位一致")
    catalog = collect_catalog_units(sql_path)
    if not catalog:
        print(f"  [SKIP] 未能从 {sql_path} 解析出 metric_catalog 数据")
        return 0

    units = collect_metric_units(src_file)
    if not units:
        print(f"  [SKIP] 未能从 {src_file} 解析出 METRIC_UNITS")
        return 0

    problems = []

    # ⚠ 同样必须**双向**比对（与检查 5 同一个道理）：
    #   ① 字典里有单位、METRIC_UNITS 漏登记 → 通知里该指标没有单位（"温度 78"）
    #   ② METRIC_UNITS 登记了字典里不存在的键 → 纯属死条目，说明当初键名写错了，
    #      而真正要用的那个键仍然没有单位（"temperature" vs "device_temp" 就是这样）
    for key, unit in sorted(catalog.items()):
        if not unit:                 # 状态类指标本来就没有单位，不要求登记
            continue
        if key not in units:
            # 元组第 2/3 位都填 unit：第 2 位是"应然"（来自字典），
            # 第 3 位是"实然"（METRIC_UNITS 里没有，视为空）。
            # 这里曾写成 ("", ...) 导致提示里打印出 '单位是 '''，
            # 运维会照着错误提示去补一个空单位 —— 检查器抓对了、
            # 但把话说错了，比不报还糟。
            problems.append((key, unit, "", "units_missing"))

    for key, unit in sorted(units.items()):
        if key not in catalog:
            problems.append((key, unit, "", "key_unknown"))
        elif catalog[key] != unit:
            problems.append((key, unit, catalog[key], "unit_mismatch"))

    if not problems:
        print(f"  [OK ] 比对 {len(units)} 个指标单位，与字典一致")
        return 0

    for key, expected_unit, actual_unit, kind in problems:
        if kind == "units_missing":
            print(f"  [FAIL] 指标 {key} 在字典里的单位是 {expected_unit!r}，"
                  f"但 METRIC_UNITS 未登记")
            print(f'         -> 在 app/models/alert.py 的 METRIC_UNITS 补 '
                  f'"{key}": "{expected_unit}"')
            print("            否则告警通知里该指标只显示裸数字，没有单位")
        elif kind == "key_unknown":
            print(f"  [FAIL] METRIC_UNITS 里的 {key!r} 在 metric_catalog 中不存在（死条目）")
            print("         -> 多半是键名拼错：真正要用的键没被登记，"
                  "通知里那个指标反而没有单位。请核对采集器实际产出的 metric_key")
        else:
            print(f"  [FAIL] 指标 {key} 单位不一致："
                  f"METRIC_UNITS={expected_unit!r}，字典={actual_unit!r}")
            print("         -> 以字典为准修正 METRIC_UNITS（同一场告警在两个渠道显示不同单位会让人怀疑数据）")
    return len(problems)


# ===========================================================================
# 检查 7：同名字段的 description 在输入类与输出类之间必须一致
# ===========================================================================

# 需要比对的 (文件, 输出类, 输入类)。与 scripts/sync_field_docs.py 的 SYNC_PAIRS 同源。
FIELD_DOC_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("app/schemas/device.py", "DeviceOut", "DeviceBase"),
    ("app/schemas/asset.py", "AssetOut", "AssetBase"),
    ("app/schemas/user.py", "UserOut", "UserCreate"),
    ("app/schemas/alert.py", "AlertRuleOut", "AlertRuleBase"),
)


def _field_descriptions(src: str, cls_name: str) -> dict[str, str]:
    """取某个类里"字面量形式的 description"。动态拼接的跳过（保守，不误判）。"""
    tree = ast.parse(src)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != cls_name:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) \
                    and isinstance(stmt.value, ast.Call):
                for kw in stmt.value.keywords:
                    if kw.arg == "description" and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str):
                        out[stmt.target.id] = kw.value.value
    return out


def check_field_docs(root: Path) -> int:
    """
    **为什么需要这条检查**

    同一个业务字段在本项目里被定义两次，而且**必须**定义两次：

        DeviceBase.ip   ← 入参（带 max_length 等写入侧约束）
        DeviceOut.ip    ← 出参（刻意**不继承** DeviceBase）

    不继承的理由写在 DeviceOut 的 docstring 里：基类上挂着 device_type/protocol 的枚举校验器，
    一旦继承，`model_validate(orm_obj)` 也会执行它们，库里有一条枚举外的历史脏数据，
    **整个设备列表接口就 500**。

    于是形成了"必须分开定义，但含义又必须一致"的两难。字段的 description 一旦漂移，
    自动生成出来的接口文档里就会出现：同一个 `ip` 字段，在"新增设备"一节说"管理 IP"，
    在"设备列表"一节说"IP 地址"——更糟的是新增了一堆 `converge_window` 之类的业务字段后，
    只在一处补了说明，另一处还空着。

    `scripts/sync_field_docs.py` 负责同步（省力），本检查负责**漂移时立刻报错**（防腐）。
    两者缺一不可：只有同步没有检查，迟早有人改了一处忘了跑脚本。
    """
    print("【检查 7】同名字段的 description 在输入类/输出类之间一致")
    problems = 0
    for rel, target, source in FIELD_DOC_PAIRS:
        path = root / rel
        if not path.exists():
            print(f"  [FAIL] 文件不存在：{rel}")
            problems += 1
            continue
        text = path.read_text(encoding="utf-8")
        src_desc = _field_descriptions(text, source)
        out_desc = _field_descriptions(text, target)
        for field, want in sorted(src_desc.items()):
            if field not in out_desc:
                # 输出类里没有同名字段是合法的（并非所有入参都会出参），不报
                continue
            if out_desc[field] != want:
                print(f"  [FAIL] {target}.{field} 与 {source}.{field} 的说明不一致")
                print(f"         {source}: {want!r}")
                print(f"         {target}: {out_desc[field]!r}")
                print("         -> 执行 python scripts/sync_field_docs.py 同步，"
                      "或手工改成一致（同一个字段在两处说法不同会误导对接方）")
                problems += 1

    total = sum(len(_field_descriptions((root / rel).read_text(encoding="utf-8"), source))
                for rel, _t, source in FIELD_DOC_PAIRS if (root / rel).exists())
    if not problems:
        print(f"  [OK ] 比对 {total} 个基础字段，输入类与输出类说明一致")
    return problems


# ===========================================================================
# 检查 8：回归脚本里用到的 query 参数必须是接口真实声明的参数
# ===========================================================================

_DEFAULT_VERIFY_SCRIPT = "scripts/verify_api.py"

# 形如  client.get("/api/v1/devices?page=1&page_size=20")  或带 f-string
_URL_LITERAL = re.compile(r'"([^"\s]*\?[^"]*)"')


def collect_script_query_args(script_path: Path) -> dict[str, set[str]]:
    """
    从回归脚本里提取「路径 → 该请求携带的 query 参数名」。

    只处理字符串字面量（含 f-string 的路径部分），足以覆盖绝大多数用例写法。
    """
    text = script_path.read_text(encoding="utf-8")
    out: dict[str, set[str]] = {}
    for m in _URL_LITERAL.finditer(text):
        url = m.group(1)
        if "?" not in url:
            continue
        path, _, query = url.partition("?")
        for seg in query.split("&"):
            seg = seg.strip()
            if not seg:
                continue
            name = seg.split("=", 1)[0].strip()
            # 值里有 {var} 无妨，关键是参数名；参数名本身是 {var} 的跳过（无法静态确定）
            if not name or name.startswith("{"):
                continue
            out.setdefault(path, set()).add(name)
    return out


def match_route_template(path: str, templates: set) -> str | None:
    """
    把脚本里的具体 URL 匹配到 OpenAPI 的路径模板。

        /api/v1/devices/2/history        → /api/v1/devices/{device_id}/history
        /api/v1/alerts/events/{eid}/ack  → /api/v1/alerts/events/{event_id}/ack

    ⚠ **为什么不能直接字符串相等比较**：
    脚本里写的是实参或 Python 变量名，OpenAPI 里写的是路径模板，
    两者天然不可能相等。第一版就是直接比对的，结果把整条带 ID 的路径都报成
    "接口不存在" —— 检查器自己制造了一堆噪音，很快就没人看了。
    """
    parts = path.strip("/").split("/")
    for tpl in templates:
        tparts = tpl.strip("/").split("/")
        if len(parts) != len(tparts):
            continue
        ok = True
        for a, b in zip(parts, tparts):
            if b.startswith("{") and b.endswith("}"):
                continue          # 模板占位段：接受任何实参
            if a.startswith("{") and a.endswith("}"):
                continue          # 脚本里的 f-string 变量：视同占位
            if a != b:
                ok = False
                break
        if ok:
            return tpl
    return None


def collect_declared_query_params(spec: dict) -> dict[str, set[str]]:
    """从 OpenAPI spec 提取「路径 → 声明的 query 参数名」"""
    out: dict[str, set[str]] = {}
    for path, ops in spec.get("paths", {}).items():
        names = set()
        for _method, op in ops.items():
            for p in op.get("parameters", []):
                if p.get("in") == "query":
                    names.add(p["name"])
        if names:
            out[path] = names
    return out


def _load_openapi(app_target: str) -> dict:
    """复用检查 4 的逻辑加载 OpenAPI（避免两处各自实现一遍 import 细节）"""
    from importlib import import_module

    if ":" in app_target:
        mod_name, attr = app_target.split(":", 1)
    else:  # pragma: no cover
        mod_name, attr = app_target, "app"
    mod = import_module(mod_name)
    obj = getattr(mod, attr)
    app = obj() if callable(obj) and not hasattr(obj, "routes") else obj
    return app.openapi()


def check_verify_query_args(script_path: Path, app_target: str) -> int:
    """
    **为什么要这条检查**

    FastAPI 有一个很容易踩的行为：**请求里出现未声明的 query 参数时，它会被静默忽略**。
    既不报错，也不拒绝，只是当作没看见。

    后果是回归脚本里写错了参数名也能一路通过：

        client.get("/api/v1/devices/2/history?metric_key=cpu_usage&hours=2")
        #  ↑ 如果该接口没声明 hours，这行等价于没传 hours，
        #    走的还是默认 6 小时窗口 —— 而演示数据恰好落在窗口里，于是连续多轮全绿

    这类问题的可怕之处在于：**测试是绿的，所以它不可能被发现，直到演示库的数据过期**。
    等那时候再排查，"为什么要拿到昨天的数据"会变成一个非常难定位的问题。

    本检查把这件事提前：脚本里用到但接口没声明的参数，直接 FAIL。
    """
    print("【检查 8】回归脚本使用的 query 参数必须已由接口声明")
    if not script_path.exists():
        print(f"  [SKIP] 未找到回归脚本：{script_path}")
        return 0

    declared = collect_declared_query_params(_load_openapi(app_target))
    used = collect_script_query_args(script_path)

    templates = set(declared)
    problems = 0
    for path in sorted(used):
        real = match_route_template(path, templates)
        if real is None:
            print(f"  [FAIL] 脚本里的路径未在接口中存在：{path}")
            problems += 1
            continue
        unknown = used[path] - declared[real]
        for name in sorted(unknown):
            print(f"  [FAIL] {path} 未声明 query 参数 `{name}`，但回归脚本里用到了")
            print("         -> FastAPI 会**静默忽略**未声明的参数：请求照样成功，"
                  "只是那个参数根本没生效，测试因此失去了它以为自己在验证的行为")
            print("         -> 要么在接口上补这个参数，要么改脚本用已声明的参数名")
            problems += 1

    checked = sum(len(v) for v in used.values())
    if not problems:
        print(f"  [OK ] 校验 {len(used)} 条路径的 {checked} 个 query 参数，均已在接口中声明")
    return problems


# ===========================================================================
# 入口
# ===========================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="FastAPI 项目静态契约自检")
    ap.add_argument("--all", action="store_true", help="执行全部检查")
    ap.add_argument("--check-output-inherit", action="store_true", help="检查 2：输出类校验器污染")
    ap.add_argument("--check-exceptions", action="store_true", help="检查 3：异常类导入完整性")
    ap.add_argument("--check-routes", action="store_true", help="检查 4：路由遮蔽")
    ap.add_argument("--check-unique-constraints", action="store_true",
                    help="检查 5：ORM 唯一约束与 SQL 脚本一致")
    ap.add_argument("--check-metric-units", action="store_true",
                    help="检查 6：指标单位表与 metric_catalog 一致")
    ap.add_argument("--check-field-docs", action="store_true",
                    help="检查 7：同名字段 description 在输入/输出类之间一致")
    ap.add_argument("--check-verify-args", action="store_true",
                    help="检查 8：回归脚本用到的 query 参数必须已声明")
    ap.add_argument("--src", default="app", help="源码根目录，默认 app")
    ap.add_argument("--schemas", default="app/schemas", help="模型目录，默认 app/schemas")
    ap.add_argument("--models", default="app/models", help="ORM 模型目录，默认 app/models")
    ap.add_argument("--sql", default="sql/schema_sqlite.sql",
                    help="建表脚本，默认 sql/schema_sqlite.sql")
    ap.add_argument("--seed-sql", default="sql/init_data.sql",
                    help="初始化数据脚本，默认 sql/init_data.sql")
    ap.add_argument("--units-file", default=_DEFAULT_METRIC_UNITS_FILE,
                    help="METRIC_UNITS 定义所在文件，默认 app/models/alert.py")
    ap.add_argument("--app", default="app.main:create_app", help="FastAPI 入口，默认 app.main:create_app")
    ap.add_argument("--verify-script", default=_DEFAULT_VERIFY_SCRIPT,
                    help="回归脚本路径，默认 scripts/verify_api.py")
    args = ap.parse_args()

    if not any([args.all, args.check_output_inherit, args.check_exceptions,
                args.check_routes, args.check_unique_constraints,
                args.check_metric_units, args.check_field_docs,
                args.check_verify_args]):
        args.all = True

    sys.path.insert(0, os.getcwd())
    total = 0
    print("=" * 64)
    if args.all or args.check_output_inherit:
        total += check_output_inherit(Path(args.schemas))
    if args.all or args.check_exceptions:
        total += check_exceptions(Path(args.src))
    if args.all or args.check_routes:
        total += check_routes(args.app)
    if args.all or args.check_unique_constraints:
        total += check_unique_constraints(Path(args.models), Path(args.sql))
    if args.all or args.check_metric_units:
        total += check_metric_units(Path(args.units_file), Path(args.seed_sql))
    if args.all or args.check_field_docs:
        total += check_field_docs(Path("."))
    if args.all or args.check_verify_args:
        total += check_verify_query_args(Path(args.verify_script), args.app)
    print("=" * 64)
    print(f"合计发现 {total} 个问题" + ("（干净）" if total == 0 else "（需要修复）"))
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
