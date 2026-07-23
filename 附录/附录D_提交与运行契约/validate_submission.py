#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_submission.py — 附录 D《提交与运行契约》学生自查脚本 v1.0

用法:
    python validate_submission.py <提交目录>

校验项:
  [1] 文件命名符合规范（{学号}_{样本ID}_polished.md / {学号}_{样本ID}_process.json 成对）
  [2] 教案 md 为 UTF-8 单文件且非空
  [3] LaTeX 定界符配对（$ 与 $$，已排除代码块与 \\$ 转义）；外链图片检查
  [4] 标题结构粗检（一级/二级标题存在；目标类/过程类标题同义词宽松匹配——仅 WARN 不判死）
  [5] process.json 可解析且符合契约 Schema（必填字段齐全、modifications 非空、跨字段一致）
  [6] 输出 PASS / WARN / FAIL 报告，FAIL 项附修复方法

退出码: 0 = 无 FAIL（PASS 或仅 WARN）；1 = 存在 FAIL；2 = 用法错误。

仅依赖 Python 3.8+ 标准库。契约全文见同目录《契约细则.md》。
"""

import json
import os
import re
import sys

# ---------------------------------------------------------------- 常量

NAME_RE_MD = re.compile(r"^(?P<sid>[A-Za-z0-9]+)_(?P<sample>[A-Za-z0-9][A-Za-z0-9-]*)_polished\.md$")
NAME_RE_JSON = re.compile(r"^(?P<sid>[A-Za-z0-9]+)_(?P<sample>[A-Za-z0-9][A-Za-z0-9-]*)_process\.json$")

EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode", "dist", "build"}

# 结构粗检同义词表（宽松子串匹配，命中任一即视为存在）
GOAL_SYNONYMS = ["教学目标", "学习目标", "项目目标", "课时目标", "单元目标", "素养目标", "目标"]
PROCESS_SYNONYMS = ["教学过程", "学习过程", "教学环节", "教学活动", "活动设计", "任务链", "任务设计",
                    "项目实施", "实施过程", "过程", "任务", "环节", "活动", "流程", "实施"]

LEVELS = {"FAIL": 0, "WARN": 1, "PASS": 2}


class Report:
    def __init__(self):
        self.items = []  # (level, code, message, fix_or_None)

    def fail(self, code, msg, fix):
        self.items.append(("FAIL", code, msg, fix))

    def warn(self, code, msg, fix=None):
        self.items.append(("WARN", code, msg, fix))

    def ok(self, code, msg):
        self.items.append(("PASS", code, msg, None))

    @property
    def n_fail(self):
        return sum(1 for i in self.items if i[0] == "FAIL")

    @property
    def n_warn(self):
        return sum(1 for i in self.items if i[0] == "WARN")


# ---------------------------------------------------------------- [1] 文件发现与命名

def discover_files(root):
    """递归收集候选提交文件（basename 以 _polished.md / _process.json 结尾）。"""
    mds, jsons = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn.endswith("_polished.md"):
                mds.append(os.path.join(dirpath, fn))
            elif fn.endswith("_process.json"):
                jsons.append(os.path.join(dirpath, fn))
    return sorted(mds), sorted(jsons)


def check_naming_and_pairing(rep, mds, jsons):
    """命名规范 + 成对性检查。返回 [(prefix, md_path, json_path)] 合法配对列表。"""
    valid_md, valid_json = {}, {}

    for path in mds:
        base = os.path.basename(path)
        m = NAME_RE_MD.match(base)
        if not m:
            rep.fail("NAME-01", "教案文件名不符合规范: %s" % base,
                     "改名为 {学号}_{样本ID}_polished.md；学号仅限字母数字，样本ID仅限字母数字连字符，"
                     "内部不得含下划线、中文、空格。示例: 20250101_MATH01_polished.md")
        else:
            prefix = "%s_%s" % (m.group("sid"), m.group("sample"))
            if prefix in valid_md:
                rep.fail("NAME-02", "同一 {学号}_{样本ID} 存在多个教案文件: %s" % prefix,
                         "每个样本恰好一对提交文件，删除多余变体（_v2 / 副本等）。")
            valid_md[prefix] = path

    for path in jsons:
        base = os.path.basename(path)
        m = NAME_RE_JSON.match(base)
        if not m:
            rep.fail("NAME-03", "过程记录文件名不符合规范: %s" % base,
                     "改名为 {学号}_{样本ID}_process.json，命名字符规则同教案文件。")
        else:
            prefix = "%s_%s" % (m.group("sid"), m.group("sample"))
            if prefix in valid_json:
                rep.fail("NAME-04", "同一 {学号}_{样本ID} 存在多个过程记录文件: %s" % prefix,
                         "每个样本恰好一对提交文件，删除多余变体。")
            valid_json[prefix] = path

    if not mds:
        rep.fail("NAME-05", "目录中未找到任何 *_polished.md 教案文件",
                 "确认提交目录正确，且磨课输出按 {学号}_{样本ID}_polished.md 命名。")
    if not jsons:
        rep.fail("NAME-06", "目录中未找到任何 *_process.json 过程记录文件",
                 "每份教案必须配套研讨过程记录 {学号}_{样本ID}_process.json（契约细则第四节）。")

    pairs = []
    for prefix, md_path in sorted(valid_md.items()):
        if prefix in valid_json:
            pairs.append((prefix, md_path, valid_json[prefix]))
        else:
            rep.fail("NAME-07", "教案 %s_polished.md 缺少配套 %s_process.json" % (prefix, prefix),
                     "补交同前缀的 process.json（G1 保真核对与过程追溯的必需件）。")
    for prefix in sorted(set(valid_json) - set(valid_md)):
        rep.fail("NAME-08", "过程记录 %s_process.json 缺少配套 %s_polished.md" % (prefix, prefix),
                 "补交同前缀的磨课后教案 Markdown 文件。")

    if pairs:
        rep.ok("NAME-OK", "发现 %d 对命名合规且成对的提交文件" % len(pairs))
    return pairs


# ---------------------------------------------------------------- [2][3][4] 教案 md 检查

def read_utf8(rep, path, label):
    """UTF-8 读取；失败/为空报 FAIL。返回文本或 None。"""
    base = os.path.basename(path)
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        rep.fail("IO-01", "%s 无法读取: %s (%s)" % (label, base, e), "检查文件权限与路径。")
        return None
    if raw.startswith(b"\xef\xbb\xbf"):
        rep.warn("ENC-02", "%s 含 UTF-8 BOM: %s" % (label, base),
                 "建议以 UTF-8(无 BOM) 重新保存，避免流水线解析首行标题失败。")
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        rep.fail("ENC-01", "%s 不是合法 UTF-8 编码: %s (位置 %d 附近)" % (label, base, e.start),
                 "用编辑器(VS Code: Save with Encoding)另存为 UTF-8。常见根因: Windows 下默认 GBK 保存。")
        return None
    if not text.strip():
        rep.fail("ENC-03", "%s 内容为空: %s" % (label, base),
                 "确认磨课系统实际写出了内容；空产出按'未产出'计 0。")
        return None
    return text


def strip_code(text):
    """剔除围栏代码块与行内代码，避免代码中的 $ 干扰配对计数。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"~~~.*?~~~", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return text


def check_latex(rep, text, base):
    t = strip_code(text).replace("\\$", "")
    n_dd = len(re.findall(r"\$\$", t))
    if n_dd % 2 != 0:
        rep.fail("TEX-01", "%s: 块级公式定界符 $$ 共 %d 个，不配对" % (base, n_dd),
                 "每个公式块须以成对 $$ 包裹。定位方法: 从文首逐个核对 $$，通常是漏写了结尾 $$ 或多敲一个 $。")
        return
    t_single = t.replace("$$", "")
    n_d = t_single.count("$")
    if n_d % 2 != 0:
        rep.fail("TEX-02", "%s: 行内公式定界符 $ 共 %d 个，不配对" % (base, n_d),
                 "每个行内公式须以成对 $ 包裹；正文里表示货币等的美元符号请转义为 \\$。"
                 "定位方法: 按段落二分排查，或搜索孤立的 ' $ '。")
        return
    rep.ok("TEX-OK", "%s: LaTeX 定界符配对正常（$$ ×%d, $ ×%d）" % (base, n_dd, n_d))


def check_images(rep, text, base):
    t = strip_code(text)
    ext = re.findall(r"!\[[^\]]*\]\(\s*(https?://[^)\s]+)", t)
    ext += re.findall(r"<img[^>]+src=[\"'](https?://[^\"']+)", t, flags=re.I)
    if ext:
        rep.fail("IMG-01", "%s: 检测到 %d 处外链图片（如 %s）" % (base, len(ext), ext[0][:60]),
                 "契约禁止外链图片。删除图片引用，将图示信息转写为文字描述或 Markdown 表格。")
    local = re.findall(r"!\[[^\]]*\]\(\s*(?!https?://)[^)]+\)", t)
    if local:
        rep.warn("IMG-02", "%s: 检测到 %d 处本地图片引用" % (base, len(local)),
                 "教案须为单文件，本地图片不会随文件进入流水线，建议转写为文字/表格。")


def check_structure(rep, text, base):
    """结构基本件粗检——仅 WARN 不判死（结构判定以流水线 LLM 抽取为准）。"""
    headings = re.findall(r"^(#{1,6})\s+(.+?)\s*$", text, flags=re.M)
    h12 = [h[1] for h in headings if len(h[0]) <= 2]
    if not headings:
        rep.warn("STR-01", "%s: 未检测到任何 Markdown 标题（#/##）" % base,
                 "无标题的流水文将大幅增加结构解析'缺核心件'误判风险，强烈建议用 #/## 组织结构件标题。")
        return
    if not h12:
        rep.warn("STR-02", "%s: 存在标题但无一级/二级标题" % base,
                 "建议将结构件（教学目标、教学过程等）提升为 #/## 级标题。")
    scan = h12 if h12 else [h[1] for h in headings]
    joined = "\n".join(scan)
    if not any(k in joined for k in GOAL_SYNONYMS):
        rep.warn("STR-03", "%s: 未发现'目标'类标题（教学目标/学习目标/项目目标…）" % base,
                 "教学目标是核心结构件（量规附则一），缺失将触发 A 维度封顶 2.0，请确认并非漏写或藏于正文。")
    if not any(k in joined for k in PROCESS_SYNONYMS):
        rep.warn("STR-04", "%s: 未发现'过程/任务'类标题（教学过程/任务链/教学环节…）" % base,
                 "教学过程/任务链是核心结构件，请确认标题可被识别。")
    if h12 and any(k in joined for k in GOAL_SYNONYMS) and any(k in joined for k in PROCESS_SYNONYMS):
        rep.ok("STR-OK", "%s: 标题结构粗检通过（检出 %d 个一/二级标题）" % (base, len(h12)))


# ---------------------------------------------------------------- [5] process.json 检查

def _req_str(rep, obj, key, where, base, min_len=1):
    """必填字符串字段检查。返回值或 None。"""
    v = obj.get(key)
    if not isinstance(v, str) or len(v.strip()) < min_len:
        rep.fail("SCH-02", "%s: %s 缺少必填字符串字段 '%s'（或为空/类型错误）" % (base, where, key),
                 "按契约细则 4.2 Schema 补全该字段，值须为非空字符串。")
        return None
    return v


def check_process_json(rep, path, prefix):
    base = os.path.basename(path)
    text = read_utf8(rep, path, "过程记录")
    if text is None:
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        rep.fail("JSN-01", "%s: JSON 解析失败 — 第 %d 行第 %d 列: %s" % (base, e.lineno, e.colno, e.msg),
                 "常见根因: 末尾多余逗号、单引号代替双引号、未转义换行。用 python -m json.tool 定位后修复。")
        return
    if not isinstance(data, dict):
        rep.fail("JSN-02", "%s: 顶层必须是 JSON 对象" % base, "按契约细则 4.2 Schema 组织顶层四字段。")
        return

    missing = [k for k in ("meta", "roles", "discussion", "modifications") if k not in data]
    if missing:
        rep.fail("SCH-01", "%s: 缺少必填顶层字段: %s" % (base, ", ".join(missing)),
                 "补全 meta / roles / discussion / modifications 四个顶层字段（契约细则 4.2）。")
        return

    # --- meta
    meta = data["meta"]
    if not isinstance(meta, dict):
        rep.fail("SCH-02", "%s: meta 必须是对象" % base, "meta 须含 student_id 与 sample_id 字符串字段。")
    else:
        sid = _req_str(rep, meta, "student_id", "meta", base)
        sam = _req_str(rep, meta, "sample_id", "meta", base)
        if sid and not re.match(r"^[A-Za-z0-9]+$", sid):
            rep.fail("SCH-03", "%s: meta.student_id '%s' 含非法字符" % (base, sid),
                     "学号仅限字母数字，与文件名及选课系统一致。")
        if sam and not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]*$", sam):
            rep.fail("SCH-03", "%s: meta.sample_id '%s' 含非法字符" % (base, sam),
                     "样本ID仅限字母数字连字符，与测试样本给定 ID 一致。")
        if sid and sam and prefix and "%s_%s" % (sid, sam) != prefix:
            rep.fail("SCH-04", "%s: meta(student_id=%s, sample_id=%s) 与文件名前缀 %s 不一致"
                     % (base, sid, sam, prefix),
                     "meta 字段必须与文件名 {学号}_{样本ID} 逐字符一致（契约细则第二节第 5 条）。")
        ts = meta.get("timestamp")
        if ts is not None:
            ok_ts = isinstance(ts, str)
            if ok_ts:
                try:
                    from datetime import datetime
                    datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    ok_ts = False
            if not ok_ts:
                rep.warn("SCH-05", "%s: meta.timestamp 不是合法 ISO 8601 字符串" % base,
                         "可选字段，建议形如 2026-07-12T15:30:00+08:00，或直接删除。")

    # --- roles
    role_ids = set()
    roles = data["roles"]
    if not isinstance(roles, list) or not roles:
        rep.fail("SCH-06", "%s: roles 必须是非空数组" % base,
                 "登记所有参与角色（含主持人/整合者/用户仲裁），每项含 role_id/name/expertise。")
    else:
        for i, r in enumerate(roles):
            where = "roles[%d]" % i
            if not isinstance(r, dict):
                rep.fail("SCH-02", "%s: %s 必须是对象" % (base, where), "每个角色为含三字段的对象。")
                continue
            rid = _req_str(rep, r, "role_id", where, base)
            _req_str(rep, r, "name", where, base)
            _req_str(rep, r, "expertise", where, base)
            if rid:
                if rid in role_ids:
                    rep.fail("SCH-07", "%s: role_id '%s' 重复" % (base, rid), "role_id 须全局唯一。")
                role_ids.add(rid)
        if len(roles) < 3:
            rep.warn("SCH-08", "%s: roles 仅 %d 个（工程实现目标要求 ≥3 个差异化专家角色）" % (base, len(roles)),
                     "确认专家角色数量满足需求文档第三节；主持人不计入专家角色数。")

    # --- discussion
    disc = data["discussion"]
    any_refers = False
    if not isinstance(disc, list):
        rep.fail("SCH-09", "%s: discussion 必须是数组" % base, "按 Schema 记录各轮发言。")
    else:
        if not disc:
            rep.warn("SCH-10", "%s: discussion 为空——研讨过程不可追溯" % base,
                     "完整保留各角色发言轨迹，否则'≥1 轮跨角色互评'无凭证，影响基础闭环评分。")
        for i, d in enumerate(disc):
            where = "discussion[%d]" % i
            if not isinstance(d, dict):
                rep.fail("SCH-02", "%s: %s 必须是对象" % (base, where), "每条发言为对象。")
                continue
            rnd = d.get("round")
            if not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1:
                rep.fail("SCH-11", "%s: %s.round 必须是 ≥1 的整数（当前: %r）" % (base, where, rnd),
                         "round 从 1 起编号，整数类型（不要写成字符串）。")
            rid = _req_str(rep, d, "role_id", where, base)
            _req_str(rep, d, "content", where, base)
            if rid and role_ids and rid not in role_ids:
                rep.fail("SCH-12", "%s: %s.role_id '%s' 未在 roles[] 登记" % (base, where, rid),
                         "所有发言角色（含主持人/用户）必须先在 roles[] 登记。")
            rt = d.get("refers_to")
            if rt is not None and rt != "" and rt != []:
                if isinstance(rt, str) or (isinstance(rt, list) and all(isinstance(x, str) for x in rt)):
                    any_refers = True
                else:
                    rep.fail("SCH-13", "%s: %s.refers_to 类型错误（须为字符串/字符串数组/null）" % (base, where),
                             "推荐格式 \"r{round}:{role_id}\"，可为多条数组。")
        if disc and not any_refers:
            rep.warn("SCH-14", "%s: 所有发言的 refers_to 均为空——未见互评引用链" % base,
                     "跨角色互评发言应填 refers_to 指向被回应的发言，否则互评轮次无凭证。")

    # --- modifications
    mods = data["modifications"]
    if not isinstance(mods, list):
        rep.fail("SCH-15", "%s: modifications 必须是数组" % base, "按 Schema 逐条登记修改说明。")
    elif not mods:
        rep.fail("SCH-16", "%s: modifications 为空——G1 内容保真无法核对，属契约违约" % base,
                 "逐条登记每处主要修改（mod_id/location/before_summary/after_summary/source_role/rationale）；"
                 "磨课系统若确实零修改，说明系统未工作，请排查。")
    else:
        mod_ids = set()
        for i, m in enumerate(mods):
            where = "modifications[%d]" % i
            if not isinstance(m, dict):
                rep.fail("SCH-02", "%s: %s 必须是对象" % (base, where), "每条修改说明为含六字段的对象。")
                continue
            mid = _req_str(rep, m, "mod_id", where, base)
            _req_str(rep, m, "location", where, base)
            if not isinstance(m.get("before_summary"), str):
                rep.fail("SCH-02", "%s: %s 缺少必填字符串字段 'before_summary'（或为空/类型错误）" % (base, where),
                         "修改前摘要；新增内容写 \"（新增）\"。")
            _req_str(rep, m, "after_summary", where, base)
            src = _req_str(rep, m, "source_role", where, base)
            _req_str(rep, m, "rationale", where, base)
            if mid:
                if mid in mod_ids:
                    rep.fail("SCH-17", "%s: mod_id '%s' 重复" % (base, mid), "mod_id 须全局唯一，如 M01, M02…")
                mod_ids.add(mid)
            if src and role_ids and src not in role_ids:
                rep.fail("SCH-18", "%s: %s.source_role '%s' 未在 roles[] 登记" % (base, where, src),
                         "修改必须可追溯到已登记角色；用户拍板也须以 role 形式登记（如 role_id=user）。")
        if not any(it[0] == "FAIL" and base in it[2] for it in rep.items):
            rep.ok("JSN-OK", "%s: Schema 校验通过（roles ×%d, discussion ×%d, modifications ×%d）"
                   % (base, len(roles) if isinstance(roles, list) else 0,
                      len(disc) if isinstance(disc, list) else 0, len(mods)))


# ---------------------------------------------------------------- 主流程

def validate(root):
    rep = Report()
    mds, jsons = discover_files(root)
    pairs = check_naming_and_pairing(rep, mds, jsons)

    for prefix, md_path, json_path in pairs:
        text = read_utf8(rep, md_path, "教案")
        if text is not None:
            base = os.path.basename(md_path)
            rep.ok("ENC-OK", "%s: UTF-8 编码、非空（%d 字符）" % (base, len(text)))
            check_latex(rep, text, base)
            check_images(rep, text, base)
            check_structure(rep, text, base)
        check_process_json(rep, json_path, prefix)

    return rep


def print_report(rep, root):
    line = "=" * 64
    print(line)
    print("附录 D 提交契约自查报告")
    print("目标目录: %s" % os.path.abspath(root))
    print(line)

    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    for level in ("FAIL", "WARN", "PASS"):
        group = [i for i in rep.items if i[0] == level]
        if not group:
            continue
        print()
        print("-- %s (%d) %s" % (level, len(group), "-" * 40))
        for _, code, msg, fix in group:
            print("[%s] %-7s %s" % (level, code, msg))
            if fix:
                print("         修复: %s" % fix)

    print()
    print(line)
    if rep.n_fail:
        print("结论: FAIL — %d 项违约必须修复（另有 %d 项警告）。" % (rep.n_fail, rep.n_warn))
        print("按上方'修复'指引处理后重新运行本脚本；提交不符合契约仅有一次 4 小时限时重交机会（契约细则第七节）。")
    elif rep.n_warn:
        print("结论: WARN — 契约层通过，可提交；%d 项警告建议处理（结构类警告不处理的评分后果自负）。" % rep.n_warn)
    else:
        print("结论: PASS — 全部契约校验通过。注意: 脚本自查通过 ≠ 内容达标，量规六维质量另行评审。")
    print(line)


def main(argv):
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 2
    root = argv[1]
    if not os.path.isdir(root):
        print("错误: '%s' 不是目录。用法: python validate_submission.py <提交目录>" % root)
        return 2
    rep = validate(root)
    print_report(rep, root)
    return 1 if rep.n_fail else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    sys.exit(main(sys.argv))
