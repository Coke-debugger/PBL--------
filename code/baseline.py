"""baseline.py — 单模型一键改写基线
对照实验用。精心设计的单次 LLM 调用，不使用多角色。
"""
from __future__ import annotations
import argparse
import logging
import sys
import yaml
from pathlib import Path

from core.llm_client import call_llm
from core.preprocessor import Preprocessor
from core.validate_submission import run as validate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

BASELINE_SYSTEM = """你是资深教研员，擅长将普通教案改写为高质量教案。
改写必须覆盖以下6个维度（按重要性排序）：
1. F·素养导向（最重要）：目标行为化（含行为动词+情境+可观察结果），情境非装饰性
2. C·知识准确：修正所有公式/方程式/计算错误，义务是改正确而非删除
3. A·结构完整：PBL课型须含成果交流节和双轨评价量规
4. B·内容丰富：预设回答须含具体内容，活动须到"可执行"颗粒度
5. D·内容一致：目标-活动-评价-学情四者协调统一
6. E·语言逻辑：清晰无冗余，术语一致

直接输出改写后的完整教案 Markdown，不要加任何解释。"""


def main():
    parser = argparse.ArgumentParser(description="单模型基线：一键改写教案")
    parser.add_argument("--lesson",      required=True, help="教案 .md 文件路径")
    parser.add_argument("--profile",     required=True, help="学情 .yaml 文件路径")
    parser.add_argument("--out",         required=True, help="输出目录")
    parser.add_argument("--student-id",  default="STU001")
    parser.add_argument("--sample-id",   default="SAMPLE01")
    parser.add_argument("--config",      default="configs/api.yaml")
    args = parser.parse_args()

    # 读取配置
    cfg = {}
    if Path(args.config).exists():
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    # 预处理
    prep = Preprocessor(args.lesson, args.profile, args.student_id, args.sample_id)
    lesson_data = prep.parse()
    text    = lesson_data["text"]
    profile = lesson_data["profile"]

    logger.info(f"基线改写：{profile.get('subject','')} {profile.get('grade','')} | 模型: {cfg.get('model','haiku')}")

    # 单次 LLM 调用
    user_prompt = (
        f"【学情】学科:{profile.get('subject','')} 年级:{profile.get('grade','')}"
        f" 先验知识:{profile.get('prior_knowledge','')}\n\n"
        f"【原始教案】\n{text}"
    )
    polished = call_llm(
        system=BASELINE_SYSTEM,
        user=user_prompt,
        temperature=cfg.get("temperature", 0.3),
        max_tokens=cfg.get("max_tokens", 2048),
        model=cfg.get("model"),
    )

    # 写出文件
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{args.student_id}_{args.sample_id}"

    polished_path = out_dir / f"{prefix}_polished.md"
    process_path  = out_dir / f"{prefix}_process.json"

    polished_path.write_text(polished, encoding="utf-8")

    # 生成极简 process.json（基线只有1条修改记录）
    import json
    from datetime import datetime
    process = {
        "meta": {
            "student_id": args.student_id,
            "sample_id":  args.sample_id,
            "timestamp":  datetime.now().isoformat(),
        },
        "roles": [
            {"role_id": "r_baseline", "name": "基线模型",
             "expertise": "单模型一键改写，不区分角色"}
        ],
        "discussion": [
            {"round": 1, "role_id": "r_baseline",
             "content": "单模型一键改写，无多角色研讨", "refers_to": None}
        ],
        "modifications": [
            {"mod_id": "M01", "location": "全文", "before_summary": "原始教案",
             "after_summary": "基线改写后教案", "source_role": "r_baseline",
             "rationale": "单模型一键改写基线", "quote_located": True}
        ],
    }
    process_path.write_text(json.dumps(process, ensure_ascii=False, indent=2), encoding="utf-8")

    # 契约校验
    result = validate(str(out_dir))
    if result["has_fail"]:
        logger.error(f"契约校验 FAIL: {result['failures']}")
        return 1

    logger.info(f"=== 基线完成 ===\n  → {polished_path}\n  → {process_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
