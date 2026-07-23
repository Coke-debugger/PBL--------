"""core/validate_submission.py — 契约四件套校验（NAME/ENC/TEX/IMG/SCH）"""
from __future__ import annotations
import json
import re
from pathlib import Path


def run(out_dir: str) -> dict:
    """扫描目录，校验所有 *_polished.md / *_process.json 对。
    返回 {"has_fail": bool, "failures": list[str], "warnings": list[str]}
    """
    results = {"has_fail": False, "failures": [], "warnings": []}
    d = Path(out_dir)

    md_files   = list(d.glob("*_polished.md"))
    json_files = list(d.glob("*_process.json"))

    if not md_files and not json_files:
        results["warnings"].append(f"输出目录 {out_dir} 中未找到任何产物文件")
        return results

    # NAME：成对检查
    md_prefixes   = {f.name.replace("_polished.md", "")   for f in md_files}
    json_prefixes = {f.name.replace("_process.json", "") for f in json_files}
    for p in md_prefixes - json_prefixes:
        _fail(results, f"NAME: {p}_polished.md 无对应 _process.json")
    for p in json_prefixes - md_prefixes:
        _fail(results, f"NAME: {p}_process.json 无对应 _polished.md")

    for f in md_files:
        prefix = f.name.replace("_polished.md", "")
        # NAME：命名规则
        r = check_naming(f.name)
        if r == "FAIL":
            _fail(results, f"NAME: {f.name} 学号或样本ID含非法字符")
        # ENC
        r = check_encoding(str(f))
        if r == "FAIL":
            _fail(results, f"ENC: {f.name} 编码非 UTF-8 或文件为空")
        # TEX
        text = f.read_text(encoding="utf-8", errors="replace")
        r = check_latex(text)
        if r == "FAIL":
            _fail(results, f"TEX: {f.name} LaTeX $ 定界符不成对")
        # IMG
        r = check_images(text)
        if r == "WARN":
            results["warnings"].append(f"IMG: {f.name} 含外链图片引用")

    for f in json_files:
        r = check_schema(str(f))
        if r == "FAIL":
            _fail(results, f"SCH: {f.name} process.json 不符合契约 Schema")

    return results


def check_naming(filename: str) -> str:
    """学号/样本ID只含字母数字（和连字符），格式 {学号}_{样本ID}_polished.md"""
    base = filename.replace("_polished.md", "").replace("_process.json", "")
    parts = base.split("_")
    if len(parts) < 2:
        return "FAIL"
    student_id, sample_id = parts[0], "_".join(parts[1:])
    if not re.fullmatch(r"[A-Za-z0-9]+", student_id):
        return "FAIL"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-]*", sample_id):
        return "FAIL"
    return "PASS"


def check_encoding(filepath: str) -> str:
    try:
        text = Path(filepath).read_text(encoding="utf-8")
        if not text.strip():
            return "FAIL"
        return "PASS"
    except UnicodeDecodeError:
        return "FAIL"


def check_latex(text: str) -> str:
    r"""检查 $ 定界符是否成对（排除代码块和 \$）"""
    clean = re.sub(r"```[\s\S]*?```", "", text)
    clean = re.sub(r"`[^`]*`", "", clean)
    dollars = re.findall(r"(?<!\\)\$", clean)
    return "PASS" if len(dollars) % 2 == 0 else "FAIL"


def check_images(text: str) -> str:
    if re.search(r"!\[.*?\]\(https?://", text):
        return "WARN"
    return "PASS"


def check_schema(filepath: str) -> str:
    try:
        data = json.loads(Path(filepath).read_text(encoding="utf-8"))
        required = {"meta", "roles", "discussion", "modifications"}
        if not required.issubset(data.keys()):
            return "FAIL"
        if len(data.get("roles", [])) < 1:
            return "FAIL"
        if len(data.get("modifications", [])) < 1:
            return "FAIL"
        meta = data.get("meta", {})
        if not meta.get("student_id") or not meta.get("sample_id"):
            return "FAIL"
        return "PASS"
    except Exception:
        return "FAIL"


def _fail(results: dict, msg: str):
    results["has_fail"] = True
    results["failures"].append(msg)
