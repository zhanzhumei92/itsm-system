"""
同步同名字段的 description：Base(输入) → Out(输出)
===============================================================================
**背景（为什么会有这个脚本）**

同一个业务字段在项目中出现两次，是本项目反复踩过的坑：

    DeviceBase.ip    ← 入参定义（带校验范围、max_length）
    DeviceOut.ip     ← 出参定义（刻意不继承 Base，见 DeviceOut 的 docstring）

这两处**必须分开**（继承会把 Base 上的枚举校验器带到输出路径，一条脏数据能拖垮整个列表接口），
但它们的**字段含义又必须一致**（否则文档里同一个字段在"新增设备"和"设备列表"两处说的是两回事）。

于是形成了典型的两难：既要分开定义，又要保持一致。

**解法**

1. 写新的 description 时只在 **Base 侧**写一次，然后跑本脚本同步到 Out 侧。
2. 漂移由 `scripts/static_audit.py --all` 的**检查 7** 兜住：
   同名 SwitchMap 在两个模型里 description 不一致就直接报错。

也就是说，本脚本负责"省力"，那条静态检查负责"不腐化"——两者缺一不可。

**用法**

    python scripts/sync_field_docs.py            # 同步（会改源码）
    python scripts/sync_field_docs.py --check    # 只报告不同步的字段，不改文件

===============================================================================
"""
from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent

# 同步方向：(目标文件, 输出类, 参考类, 参考类所在模块)。
# 只登记"业务含义完全一致、且 Out 刻意不继承 Base"的那一批，
# 含义不同的字段（如 AssetBase.name="设备名称" vs AssetOut.name="资产名称"）不要放进来。
SYNC_PAIRS: List[Tuple[str, str, str, str]] = [
    ("app/schemas/device.py", "DeviceOut", "DeviceBase", "app/schemas/device.py"),
    ("app/schemas/asset.py", "AssetOut", "AssetBase", "app/schemas/asset.py"),
    ("app/schemas/user.py", "UserOut", "UserCreate", "app/schemas/user.py"),
    ("app/schemas/alert.py", "AlertRuleOut", "AlertRuleBase", "app/schemas/alert.py"),
]


def _field_desc_from_source(src: str, cls_name: str) -> Dict[str, str]:
    """
    从源码里解析某个类中各字段的 description。

    ⚠ **为什么用 AST 而不是正则处理的反向操作**：
    这里不需要非常精确（只取"字面量形式的 description"），但要能容忍各种写法。
    AST 比正则稳；遇到带变量拼接的动态 description 就跳过（保守）。
    """
    tree = ast.parse(src)
    out: Dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != cls_name:
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                continue
            field = stmt.target.id
            value = stmt.value
            if not isinstance(value, ast.Call):
                continue
            for kw in value.keywords:
                if kw.arg == "description" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    out[field] = kw.value.value
    return out


# 给一个已经存在 description="..." 的字段行替换描述；
# 给还没有 description 的字段行追加一个 description 关键字参数。
_RE_HAS_DESC = re.compile(r'description\s*=\s*(?:"[^"]*"|\'[^\']*\')')

# 三种字段写法，必须都能处理：
#   ① `ip: str = Field(..., max_length=64)`                → 已有 Field 调用
#   ② `name: str = ""`                                     → 裸默认值
#   ③ `id: int`                                            → 连默认值都没有
_RE_FIELD_CALL = re.compile(r"=\s*([A-Za-z_][\w.]*)\((.*)\)\s*$")


def _apply_desc(line: str, desc: str) -> str:
    esc = desc.replace("\\", "\\\\").replace('"', '\\"')
    if _RE_HAS_DESC.search(line):
        return _RE_HAS_DESC.sub(f'description="{esc}"', line, count=1)

    stripped = line.rstrip()
    trailing = line[len(stripped):]
    m = _RE_FIELD_CALL.search(stripped)
    if m:
        # ① 已有 Field(...)：把 description 作为一个新关键字参数追加进去
        inner = m.group(2)
        new_inner = f'{inner.rstrip().rstrip(",")}, description="{esc}"' if inner.strip() \
            else f'description="{esc}"'
        return f"{stripped[: m.start(2)]}{new_inner}){trailing}"

    # ②③ 没有 Field 调用：整行重写为 Field(...)
    m2 = re.match(r"^(\s*)([A-Za-z_]\w*)\s*:\s*(.+?)\s*(?:=\s*(.*?))?\s*$", stripped)
    if not m2:
        return line
    indent, name, typ, default = m2.groups()
    if default is None:
        return f'{indent}{name}: {typ} = Field(description="{esc}"){trailing}'
    return f'{indent}{name}: {typ} = Field(default={default}, description="{esc}"){trailing}'


def sync_one(path: Path, target_cls: str, source_cls: str, source_path: Path) -> Tuple[int, List[str]]:
    src = target_path_text = path.read_text(encoding="utf-8")
    source_desc = _field_desc_from_source(source_path.read_text(encoding="utf-8"), source_cls)
    if not source_desc:
        return 0, [f"{source_cls} 里没有解析到任何 description"]

    lines = src.split("\n")
    # 定位目标类的字段区
    start = None
    for i, l in enumerate(lines):
        if l.startswith(f"class {target_cls}(") or l.startswith(f"class {target_cls}:"):
            start = i
            break
    if start is None:
        return 0, [f"未找到类 {target_cls}"]
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j]
        if s and not s[0].isspace() and not s.startswith(")"):
            if s.startswith("class ") or s.startswith("@"):
                end = j
                break

    changed = 0
    report: List[str] = []
    for j in range(start, end):
        line = lines[j]
        # ⚠ 跳过多行 Field(...) 的续行：只处理"一行写得完"的字段。
        # 跨行写法动它没有把握（可能把括号配对改坏），宁可跳过让人手工补。
        if line.rstrip().endswith(("(", ",", "\\")):
            continue
        m = re.match(r"^(\s{4})([A-Za-z_]\w*)\s*:\s*(.+?)\s*(=\s*.*)?$", line)
        if not m:
            continue
        field = m.group(2)
        if field.startswith("_"):
            continue
        want = source_desc.get(field)
        if not want:
            continue
        cur = _RE_HAS_DESC.search(line)
        if cur:
            # 已有描述：内容不同才动
            quoted = cur.group(0).split("=", 1)[1].strip().strip('"').strip("'")
            if quoted == want:
                continue
        new_line = _apply_desc(line, want)
        if new_line != line:
            lines[j] = new_line
            changed += 1
            report.append(f"  {target_cls}.{field}")

    if changed:
        tmp = "\n".join(lines)
        if tmp != target_path_text:
            path.write_text(tmp, encoding="utf-8")
    return changed, report


def main() -> int:
    ap = argparse.ArgumentParser(description="把同名字段的描述从输入类同步到输出类")
    ap.add_argument("--check", action="store_true", help="只报告不同步的字段，不修改文件")
    args = ap.parse_args()

    total = 0
    for rel_path, target_cls, source_cls, source_rel in SYNC_PAIRS:
        path = ROOT / rel_path
        source_path = ROOT / source_rel
        if args.check:
            # check 模式下不改文件：先同步到内存副本比较
            original = path.read_text(encoding="utf-8")
            changed, report = sync_one(path, target_cls, source_cls, source_path)
            if changed:
                path.write_text(original, encoding="utf-8")
                print(f"[不同步] {target_cls} ← {source_cls}：{changed} 处")
                total += changed
        else:
            changed, report = sync_one(path, target_cls, source_cls, source_path)
            print(f"[同步] {target_cls} ← {source_cls}：{changed} 处")
            for r in report:
                print(r)
            total += changed

    if args.check and total:
        print(f"\n共 {total} 处不同步，请执行：python scripts/sync_field_docs.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
