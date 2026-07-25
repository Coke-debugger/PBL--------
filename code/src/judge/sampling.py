"""多次采样调用 + 聚合逻辑。

附录A"聚合先证据后分数"的硬性要求：多采样的聚合单位是证据（子指标判定/
错误根因），对齐去重后按计分规则统一算分——不是对分数取中位数。

model_pool 是显式参数而非硬编码模型名：附录A要求C维度"应≥2个模型家族"，
本实现先传单元素列表（同模型多次采样代替），未来接入第二个模型家族只需
调用方传入多元素 model_pool，采样按 model_pool[i % len(model_pool)] 轮流
分配，不需要改这里的聚合逻辑。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from .llm_client import call_llm
from .prompts import build_c_dimension_prompt, build_point_dimension_prompt, load_rubric

# 全局采样并发上限。judge.evaluate 把 6 个维度的所有采样调用（当前 N_SAMPLES
# 下合计 15 次）一起提交到同一个线程池，由这个上限约束瞬时并发，避免请求同时
# 打出撞 429。设为14：略高于总采样数(15)，基本能让全部采样一次性进池跑完，
# 墙钟≈单次最慢调用时间，不再受"先到维度占住 worker、后到维度干等"拖累。
# Round 0 已验证4路、6路、10路均稳定；提到14后若 429 增多/频繁限流回退，
# 调回 10~12。llm_client 内部带重试兜底429。
SAMPLE_WORKERS = 14


def _call_model(prompt: str, model: str, temperature: float = 0.0, max_tokens: int = 4096) -> str:
    """通过 llm_client 发起一次调用。供应商/模型切换只需改 configs/api.yaml，
    这里不再硬编码 anthropic 客户端。

    temperature 默认 0.0：评审要可复现——同一份 polished 多次评应给接近的分。
    此前 0.2 会让单次采样漂移，叠加多采样多数票后仍有 ±几分波动；降到 0.0 把
    评审侧的随机性压到最低（LLM temp 0 仍非完全确定，但已足够稳定到 ±5 目标）。
    C 维度的 0.1 同理在 judge.py 里一并改 0.0。

    max_tokens 默认 4096：长教案（3万字）评审要输出多子指标 evidence + D 的
    mapping_table / F 的 f1_goal_evidence，2048 常在数组中间被截断导致整段
    JSON 解析失败、该维度 0 样本 0 分。4096 能容纳完整结构化输出，避免截断。
    生成延迟主要由输出长度决定，但截断→0分 的损失远大于多生成一点 token 的耗时。"""
    return call_llm(system="", user=prompt, model=model, temperature=temperature, max_tokens=max_tokens)


def _parse_json_object(raw: str | None) -> dict:
    """从模型输出中宽容解析出一个JSON对象。

    多级容错：①代码块JSON ②裸JSON ③截断JSON抢救（max_tokens 不足时模型输出
    可能被截断在 evidence 数组中间，json.loads 会失败；此处尝试补全括号取出
    已完成的子指标分）。即使 mapping_table/f1_goal_evidence 等复杂字段缺，
    只要 sub_indicator_scores 在，聚合层就能用——避免 flash 输出复杂结构时
    整段解析失败导致该维度 0 样本、0 分。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("模型返回空文本，无法解析 JSON 对象")
    # ① 代码块包裹的 JSON
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # ② 裸 JSON（贪心匹配到最后一个 }）
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # ③ 截断抢救：candidate 可能因 max_tokens 在数组中间截断而不闭合。
            # 尝试从尾部回退到最后一个完整对象、再补全括号解析。
            salvaged = _salvage_truncated_json(candidate)
            if salvaged is not None:
                return salvaged
    raise ValueError(f"无法从模型输出中解析出JSON对象: {raw[:200]}...")


def _salvage_truncated_json(candidate: str) -> dict | None:
    """从被 max_tokens 截断的 JSON 片段里抢救出已完成的字段。

    策略：从末尾往前找最后一个 '}' 或 ']'，把后面未闭合的部分截掉，再逐层
    补全 '{'/'[' 对应的右括号。只保留能成功 json.loads 的结果。即使最终只剩
    {"dimension":"D","sub_indicator_scores":{"D1":2,...}} 也有用——聚合层
    能据此打分，不至于该维度 0 样本。
    """
    # 找最后一个完整值结尾（数字、}、]、引号）
    for cut in range(len(candidate), 0, -1):
        chunk = candidate[:cut]
        # 统计未闭合的括号
        opens = []
        in_str = False
        esc = False
        for ch in chunk:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch in "{[":
                opens.append(ch)
            elif ch == "}":
                if opens and opens[-1] == "{":
                    opens.pop()
            elif ch == "]":
                if opens and opens[-1] == "[":
                    opens.pop()
        if opens:
            # 补全右括号
            closers = "".join("}" if o == "{" else "]" for o in reversed(opens))
            try:
                return json.loads(chunk + closers)
            except json.JSONDecodeError:
                continue
        else:
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
    return None


def sample_dimension(
    dim: str,
    lesson_text: str,
    profile: dict,
    model_pool: list[str],
    n: int = 3,
    lesson_type: str = "常规课",
    executor: ThreadPoolExecutor | None = None,
) -> list[dict]:
    """对A/B/D/E/F某一维度做n次采样，返回每次的原始解析结果列表。

    executor 不为空时复用调用方传入的线程池（judge.evaluate 用一个全局池把
    6 个维度的采样并发提交，避免维度间串行）；为空时自建临时池，行为与旧版一致。
    """
    if not model_pool:
        raise ValueError("model_pool 不能为空")
    prompt = build_point_dimension_prompt(dim, lesson_text, profile, lesson_type)

    def _one(i: int):
        model = model_pool[i % len(model_pool)]
        raw = _call_model(prompt, model=model)
        try:
            return _parse_json_object(raw)
        except (ValueError, json.JSONDecodeError):
            return None  # 单次采样解析失败不致命，聚合层按有效样本数计算

    def _collect(pool: ThreadPoolExecutor) -> list[dict]:
        results: list[dict] = []
        for parsed in pool.map(_one, range(n)):
            if parsed is not None:
                results.append(parsed)
        return results

    if executor is not None:
        return _collect(executor)
    workers = min(SAMPLE_WORKERS, n) if n > 0 else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return _collect(pool)


def sample_c_dimension(
    lesson_text: str,
    profile: dict,
    model_pool: list[str],
    n: int = 3,
    executor: ThreadPoolExecutor | None = None,
) -> list[dict]:
    if not model_pool:
        raise ValueError("model_pool 不能为空")
    prompt = build_c_dimension_prompt(lesson_text, profile)

    def _one(i: int):
        model = model_pool[i % len(model_pool)]
        raw = _call_model(prompt, model=model, temperature=0.1)
        try:
            return _parse_json_object(raw)
        except (ValueError, json.JSONDecodeError):
            return None

    def _collect(pool: ThreadPoolExecutor) -> list[dict]:
        results: list[dict] = []
        for parsed in pool.map(_one, range(n)):
            if parsed is not None:
                results.append(parsed)
        return results

    if executor is not None:
        return _collect(executor)
    workers = min(SAMPLE_WORKERS, n) if n > 0 else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return _collect(pool)


def _majority_score(scores: list[int]) -> int:
    """多数判定 + 离群剔除，抗单次采样跑偏。

    规则：
    - 有明确多数（某值出现次数 > 半数）：直接取该值。如 [2,2,2,0]→2。
    - 无多数但有效样本≥3：取中位数。覆盖"全不同"[0,1,2]→1 和"2:2平票"[0,0,2,2]→1
      （中位数比机械取低更抗单次离群）。
    - 样本<3：保守取较低分（样本太少）。
    """
    if not scores:
        return 0
    n = len(scores)
    counts = Counter(scores)
    top = counts.most_common()
    best_count = top[0][1]
    # 明确多数：某值出现超过半数
    if best_count > n / 2:
        return top[0][0]
    # 无多数且样本≥3：取中位数抗离群
    if n >= 3:
        return sorted(scores)[n // 2]
    # 样本<3：平票保守取低
    tied = sorted(v for v, c in top if c == best_count)
    return tied[0]


# judgment 文本 → 子指标点数（0/1/2）。量规约定：满足=2、部分满足=1、不满足=0。
# 模型在 evidence 里用自然语言判定，少数派会在 sub_indicator_scores 里给出与
# 判定相矛盾的分（如判"满足"却填0），这里用于从 evidence 反推真实分作兜底。
_JUDGMENT_TO_SCORE = {
    "满足": 2, "充分满足": 2, "完全满足": 2,
    "部分满足": 1, "基本满足": 1, "部分": 1,
    "不满足": 0, "未满足": 0, "缺失": 0, "无": 0,
}


def _judgment_score(text: str) -> int | None:
    """把 evidence 的 judgment 字段映射成 0/1/2，无法识别返回 None。"""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if t in _JUDGMENT_TO_SCORE:
        return _JUDGMENT_TO_SCORE[t]
    # 容忍"满足（含…）""部分满足，但…"等带后缀写法：按关键词包含判定，先匹配高分档
    if "充分满足" in t or "完全满足" in t or t.startswith("满足"):
        return 2
    if "部分满足" in t or "基本满足" in t:
        return 1
    if "不满足" in t or "未满足" in t or "缺失" in t:
        return 0
    return None


def _evidence_derived_scores(evidence: list[dict], sub_indicators: list[str]) -> dict[str, int]:
    """从 evidence 的 judgment 反推每个子指标的分（多数票）。

    仅作为 sub_indicator_scores 的兜底：当模型在结构化分里自相矛盾（evidence 判
    "满足"却填0）时，用 evidence 的多数判定纠正。模型未必给每个子指标都写
    evidence，缺失的子指标不在此返回，由调用方保留原分。
    """
    per_sub: dict[str, list[int]] = defaultdict(list)
    for item in evidence or []:
        sid = item.get("sub_indicator", "")
        if sid not in sub_indicators:
            continue
        sc = _judgment_score(item.get("judgment", ""))
        if sc is not None:
            per_sub[sid].append(sc)
    return {sid: _majority_score(v) for sid, v in per_sub.items() if v}


def aggregate_point_dimension(dim: str, samples: list[dict], lesson_text: str) -> dict:
    """把A/B/D/E/F某一维度的n次采样聚合成一份判定结果。

    lesson_text 用于对每条 evidence 的原文引用做真实校验（verify_evidence_quotes），
    而不是像早期实现那样只检查 quote 字段非空——引用校验失败是"模型可能在编造
    证据"的信号，聚合结果里的 evidence_verified_ratio 供 judge.py 做保守熔断。
    """
    rubric = load_rubric()
    sub_indicators = [si["id"] for si in rubric["dimensions"][dim]["sub_indicators"]]

    per_sub_scores: dict[str, list[int]] = defaultdict(list)
    all_evidence, all_issues = [], []
    for s in samples:
        raw_scores = s.get("sub_indicator_scores", {})
        for sid in sub_indicators:
            v = raw_scores.get(sid)
            if isinstance(v, int) and not isinstance(v, bool) and v in (0, 1, 2):
                per_sub_scores[sid].append(v)
        all_evidence.extend(s.get("evidence", []) or [])
        all_issues.extend(s.get("issues", []) or [])

    sub_indicator_scores = {sid: _majority_score(per_sub_scores.get(sid, [])) for sid in sub_indicators}
    verified_evidence, verified_ratio = verify_evidence_quotes(all_evidence, lesson_text)

    # 证据反推兜底：模型偶发"evidence 判满足、sub_indicator_scores 却填0"的自相
    # 矛盾。当某子指标结构化分为 0（或无有效结构化分）且 evidence 里有该子指标的
    # 判定时，用 evidence 多数票覆盖。仅覆盖 0→正分（修正模型把"满足"误填0），
    # 不做正分→0 的反向覆盖（避免把模型真心判"不满足"的分抬高）。被覆盖的子指标
    # 记进 evidence_override 便于追溯。这直接修掉 B 维度"evidence 全满足却得0分"
    # 的问题，且不改变模型自洽输出的计分。
    evidence_derived = _evidence_derived_scores(all_evidence, sub_indicators)
    overrides: dict[str, int] = {}
    for sid in sub_indicators:
        structured = sub_indicator_scores.get(sid, 0)
        derived = evidence_derived.get(sid)
        if derived is not None and derived > structured:
            sub_indicator_scores[sid] = derived
            overrides[sid] = derived

    result = {
        "dimension": dim,
        "sub_indicator_scores": sub_indicator_scores,
        "evidence": verified_evidence,
        "evidence_verified_ratio": verified_ratio,
        "issues": all_issues,
        "n_valid_samples": len(samples),
    }
    if overrides:
        result["evidence_override"] = overrides

    if dim == "F":
        result["f1_goal_evidence"] = _aggregate_f1_goal_evidence(samples)
    if dim == "D":
        result["mapping_table"] = _merge_mapping_tables(samples)

    return result


def verify_evidence_quotes(evidence: list[dict], lesson_text: str) -> tuple[list[dict], float]:
    """对每条 evidence 的 quote 做原文子串校验（复用 _normalize 归一化）。

    返回 (标记了 quote_verified 的 evidence 列表, 已验证比例)。已验证比例只统计
    "带非空 quote" 的条目——没给 quote 的条目不计入分母，避免和"完全没引用"混淆。
    这不是判0/1/2的直接依据（那是模型自己在 sub_indicator_scores 里给的），而是
    judge.py 的保守熔断信号：quote 大量核实不了，说明模型可能在编造证据，应该
    整体压低对该维度分数的信任度，而不是逐条精确甄别（逐条对应到具体哪个子指标
    在rubric里没有强约束，勉强对应反而更脆弱）。
    """
    normalized_text = _normalize(lesson_text)
    out = []
    checked = 0
    verified = 0
    for item in evidence:
        item = dict(item)
        quote = item.get("quote", "")
        if quote:
            checked += 1
            ok = _quote_matches(str(quote), normalized_text)
            item["quote_verified"] = ok
            if ok:
                verified += 1
        out.append(item)
    ratio = (verified / checked) if checked else 1.0  # 没有可核验引用时不惩罚（如E维度部分子指标本就无quote）
    return out, ratio


def _quote_matches(quote: str, normalized_text: str) -> bool:
    """判断 quote 是否能在教案原文中找到对应，容忍 flash 模型的改写引用。

    flash 常把原文引用改写成"语义同、字面略异"（插入标点、调语序、补省略），
    严格逐字子串匹配会把这类合理引用判为"未核实"，导致 evidence_verified_ratio
    虚低、触发保守封顶、把好教案压成分。这里分级匹配：
      1. 归一化后逐字子串命中 → 直接通过；
      2. 否则取 quote 中最长(≥5字)的连续片段在原文中搜，命中任一即算通过——
         这能放行"改写但保留了关键名词短语"的引用，仍能挡住纯编造的引用
         （编造的引用关键片段在原文里找不到）。
    """
    norm_quote = _normalize(quote)
    if not norm_quote:
        return False
    if norm_quote in normalized_text:
        return True
    # 模糊：滑动取 quote 的连续片段（5~15字），命中任一即算通过
    window = 5
    if len(norm_quote) < window:
        return norm_quote in normalized_text
    for start in range(0, len(norm_quote) - window + 1):
        chunk = norm_quote[start:start + window]
        if chunk in normalized_text:
            return True
    return False


def _aggregate_f1_goal_evidence(samples: list[dict]) -> list[dict]:
    """按 goal_quote 归一化后做多数投票，判断该目标是否有活动证据支撑。"""
    by_goal: dict[str, list[bool]] = defaultdict(list)
    for s in samples:
        for item in s.get("f1_goal_evidence", []) or []:
            q = _normalize(str(item.get("goal_quote", "")))
            if not q:
                continue
            by_goal[q].append(bool(item.get("has_evidence", False)))
    out = []
    for q, votes in by_goal.items():
        true_count = sum(votes)
        out.append({"goal_quote": q, "has_evidence": true_count * 2 >= len(votes)})
    return out


def _merge_mapping_tables(samples: list[dict]) -> list[dict]:
    """按 (goal_quote, activity_quote) 去重合并各采样的映射表。"""
    seen: dict[tuple, dict] = {}
    for s in samples:
        for entry in s.get("mapping_table", []) or []:
            key = (_normalize(str(entry.get("goal_quote", ""))), _normalize(str(entry.get("activity_quote", ""))))
            if key not in seen:
                seen[key] = entry
    return list(seen.values())


def _root_cause_key(rc: str) -> str:
    """根因归一化：取最长连续中文/字母片段作为簇键，让措辞不同但指同一问题的
    根因合并（如"密闭空间燃烧缺氧中毒"与"密闭空间燃烧实验危险"应归一簇）。"""
    norm = _normalize(rc)
    if not norm:
        return ""
    # 取所有长度>=4的连续片段里最长的，没有就退回全文
    chunks = re.findall(r"[一-龥a-zA-Z]{4,}", norm)
    return max(chunks, key=len) if chunks else norm


def _semantic_dedup_issues(issues: list[dict], model: str) -> list[dict] | None:
    """用模型对并集 issues 做语义去重：把指同一根因的错误（措辞不同）合并成1个。

    多次采样常对同一错误用不同措辞各报1次（如"酥油比例矛盾"报5次），字面合并
    （关键词簇/公共片段）不准。这里调模型一次看完所有 issues，让它判断哪些指同一
    根因并合并，输出独立错误清单。用模型的语义能力做字面规则做不好的事。

    返回去重后的 issues（每条含 root_cause/error_type/quote/location），失败返回 None
    （调用方回退到字面合并）。
    """
    if not issues:
        return []
    import json as _json
    # 只传必要字段，减小输入
    compact = [
        {"root_cause": str(i.get("root_cause", "")),
         "error_type": str(i.get("error_type", "")),
         "quote": str(i.get("quote", ""))[:60]}
        for i in issues
    ]
    prompt = (
        "以下是对同一份教案多次评审汇总发现的问题清单。其中有些问题指同一个根因（只是措辞不同），"
        "请把指同一根因的问题合并成1条（取最严重的 error_type，root_cause 用最清晰的一条），"
        "保留真正独立的不同问题。只输出合并后的清单，不要解释。\n\n"
        "错误类型档位（严重程度）：重大知识性错误 > 符号/公式实质问题 > 一般性不严谨 > 格式合规问题。\n"
        "合并时若同一根因被报成不同档位，取最重的档位。\n\n"
        f"问题清单（共{len(compact)}条）：\n{_json.dumps(compact, ensure_ascii=False, indent=1)}\n\n"
        "严格输出JSON数组，每条含 root_cause/error_type/quote。只输出JSON。"
    )
    try:
        raw = _call_model(prompt, model=model, temperature=0.0, max_tokens=4096)
        parsed = _parse_json_object(raw)
        # 兼容模型返回数组 或 dict包数组(如{"issues":[...]})
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    parsed = v
                    break
        if isinstance(parsed, list) and parsed:
            # 只保留含 root_cause 的有效条目
            parsed = [it for it in parsed if isinstance(it, dict) and it.get("root_cause")]
            if not parsed:
                return None
            # 补 location（模型输出可能没有，从原 issues 按quote匹配回填）
            quote_to_loc = {str(i.get("quote", ""))[:60]: i.get("location", "") for i in issues}
            for item in parsed:
                if "location" not in item:
                    item["location"] = quote_to_loc.get(str(item.get("quote", ""))[:60], "")
            return parsed
        return None
    except Exception:
        return None


def _literal_dedup_confirm(all_issues: list[dict], dedup_table: dict) -> list[dict]:
    """字面合并回退：root_cause 关键词簇 + quote 公共片段并查集合并，取最重档位。

    语义去重失败时用。不如模型语义去重准，但保证有结果。
    """
    severity_rank = {"重大知识性错误": 3, "符号/公式实质问题": 2, "一般性不严谨": 1, "格式合规问题": 0}
    # root_cause 关键词簇
    root_groups: dict[str, list[dict]] = defaultdict(list)
    for issue in all_issues:
        key = _root_cause_key(str(issue.get("root_cause", "")))
        if key:
            root_groups[key].append(issue)
    group_items = list(root_groups.items())
    group_quotes = [_normalize(str(iss[0].get("quote", "")) or k) for k, iss in group_items]

    def _common_cn_len(a: str, b: str) -> int:
        best = 0
        for i in range(len(a)):
            for j in range(len(b)):
                k = 0
                while (i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]
                       and "一" <= a[i + k] <= "鿿"):
                    k += 1
                if k > best:
                    best = k
        return best

    n = len(group_items)
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue
            if _common_cn_len(group_quotes[i], group_quotes[j]) >= 4:
                parent[find(j)] = find(i)

    quote_groups: dict[int, list[dict]] = defaultdict(list)
    for idx, (k, iss) in enumerate(group_items):
        quote_groups[find(idx)].extend(iss)

    confirmed = []
    for q_key, issues in quote_groups.items():
        error_types = Counter(i.get("error_type", "") for i in issues)
        best_type = max(error_types.keys(), key=lambda t: severity_rank.get(t, 0))
        confirmed.append({
            "root_cause": str(issues[0].get("root_cause", "")),
            "error_type": best_type,
            "quote": issues[0].get("quote", ""),
            "location": issues[0].get("location", ""),
            "hit_count": len(issues),
            "dedup": "literal",
        })
    return confirmed


def aggregate_c_dimension(samples: list[dict], n_samples: int, model: str = "") -> dict:
    """C维度专用聚合：多次采样取并集 → 语义去重（模型合并同义错误）→ 全额扣分。

    规则（用户定）：并集取错误，同根因合并1个，每个错误全额扣对应额度，
    格式合规封顶1.0，其余无上限，累加到扣完(到0)。
    去重优先用模型语义去重（_semantic_dedup_issues），失败回退字面合并。
    """
    rubric = load_rubric()
    dedup_table = {d["type"]: d for d in rubric["dimensions"]["C"]["deduction_table"]}

    all_issues = []
    for s in samples:
        all_issues.extend(s.get("issues", []) or [])

    # 优先语义去重：调模型把措辞不同但同根因的错误合并。失败回退字面合并。
    deduped = _semantic_dedup_issues(all_issues, model) if model and all_issues else None
    if deduped is not None:
        # 模型去重成功：直接用去重后的清单作为 confirmed
        confirmed = []
        for item in deduped:
            et = str(item.get("error_type", ""))
            if et not in dedup_table:
                continue
            confirmed.append({
                "root_cause": str(item.get("root_cause", "")),
                "error_type": et,
                "quote": str(item.get("quote", "")),
                "location": str(item.get("location", "")),
                "hit_count": 1,
                "dedup": "semantic",
            })
    else:
        # 回退：字面合并（root_cause关键词簇 + quote公共片段）
        confirmed = _literal_dedup_confirm(all_issues, dedup_table)

    total_deduction = 0.0
    format_deduction = 0.0
    for item in confirmed:
        rule = dedup_table.get(item["error_type"])
        if rule is None:
            continue
        if "cap" in rule:
            # 格式合规问题：累加后封顶 cap（量规规定全文档一次性封顶-1）
            format_deduction += rule["deduction"]
        else:
            # 重大/一般/符号：每个错误全额扣，无上限，累加到扣完
            total_deduction += rule["deduction"]
    format_deduction = min(format_deduction, dedup_table.get("格式合规问题", {}).get("cap", format_deduction))
    total_deduction += format_deduction

    has_verifiable = any(s.get("has_verifiable_content") for s in samples) if samples else False
    score = max(0.0, 5.0 - total_deduction)
    if not has_verifiable:
        score = min(score, 3.0)  # 内容回避封顶条款

    return {
        "dimension": "C",
        "score": round(score, 2),
        "confirmed_issues": confirmed,
        "all_issues": all_issues,
        "has_verifiable_content": has_verifiable,
        "n_valid_samples": len(samples),
    }


def _normalize(text: str) -> str:
    """去空白/标点，用于原文子串模糊匹配与根因归一化对齐。"""
    return re.sub(r"[\s,，。.、;；:：!！?？'\"'\"“”‘’()（）\[\]【】]", "", text)


def verify_mapping_table(mapping: list[dict], lesson_text: str) -> list[dict]:
    """校验映射表中的quote是否为原文逐字子串（归一化后）。校验失败标记invalid。"""
    normalized_text = _normalize(lesson_text)
    out = []
    for entry in mapping:
        goal_ok = _normalize(str(entry.get("goal_quote", ""))) in normalized_text
        activity_ok = _normalize(str(entry.get("activity_quote", ""))) in normalized_text
        eval_quote = entry.get("eval_quote")
        eval_ok = True
        if eval_quote:
            eval_ok = _normalize(str(eval_quote)) in normalized_text
        entry = dict(entry)
        entry["valid"] = bool(goal_ok and activity_ok and eval_ok)
        if not entry["valid"]:
            entry["invalid_reason"] = "quote未在原文中命中，证据作废"
        out.append(entry)
    return out
