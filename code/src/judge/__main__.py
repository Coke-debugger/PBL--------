"""独立运行入口，仅用于单独调试 Judge，不等同于团队接口契约里 P1 负责的
整条磨课流水线入口 run.py。

用法:
    python -m src.judge --lesson <path> --profile <path.yaml|path.json> [--lesson-type PBL|常规课] [--out <path>]

与库调用共用同一实现（本文件只是命令行外壳）：
    from src.judge import Judge
    Judge(profile).evaluate(lesson_text, lesson_type)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .judge import Judge


def _load_profile(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml  # 延迟导入：只有走YAML分支才需要PyYAML依赖

        return yaml.safe_load(text)
    return json.loads(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立运行 LLM-as-Judge 教案评分器（调试用）")
    parser.add_argument("--lesson", required=True, help="教案 Markdown 文件路径")
    parser.add_argument("--profile", required=True, help="学情描述文件路径（.yaml 或 .json）")
    parser.add_argument("--lesson-type", default="常规课", choices=["常规课", "PBL"], help="课型，默认常规课")
    parser.add_argument("--out", default=None, help="评分结果输出JSON路径，缺省打印到stdout")
    args = parser.parse_args(argv)

    lesson_path = Path(args.lesson)
    profile_path = Path(args.profile)
    if not lesson_path.exists():
        print(f"错误: 教案文件不存在: {lesson_path}", file=sys.stderr)
        return 2
    if not profile_path.exists():
        print(f"错误: 学情描述文件不存在: {profile_path}", file=sys.stderr)
        return 2

    lesson_text = lesson_path.read_text(encoding="utf-8")
    profile = _load_profile(profile_path)

    report = Judge(profile).evaluate(lesson_text, lesson_type=args.lesson_type)
    output = json.dumps(report, ensure_ascii=False, indent=2)

    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"评分结果已写入: {args.out}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
