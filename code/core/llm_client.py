"""core/llm_client.py — LLM客户端抽象层，支持 Anthropic 和 OpenAI 兼容接口（DeepSeek等）"""
from __future__ import annotations
import os
import time
import json
import hashlib
import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_api_config: Optional[dict] = None


def _load_api_config(config_path: str = "configs/api.yaml") -> dict:
    global _api_config
    if _api_config is None:
        p = Path(config_path)
        if p.exists():
            _api_config = yaml.safe_load(p.read_text(encoding="utf-8"))
        else:
            _api_config = {
                "provider":    "anthropic",
                "model":       "claude-haiku-4-5-20251001",
                "max_tokens":  2048,
                "temperature": 0.3,
                "api_key_env": "ANTHROPIC_API_KEY",
            }
    return _api_config


def call_llm(
    system: str,
    user: str,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    model: Optional[str] = None,
) -> str:
    """统一LLM调用接口。支持 anthropic / deepseek / openai 三种 provider。"""
    cfg        = _load_api_config()
    provider   = cfg.get("provider", "anthropic").lower()
    model_env  = cfg.get("model_env", "LLM_MODEL")
    _model     = model or os.environ.get(model_env) or cfg.get("model", "claude-haiku-4-5-20251001")
    _max_tok   = max_tokens  or cfg.get("max_tokens", 2048)
    _temp      = temperature if temperature is not None else cfg.get("temperature", 0.3)

    if provider == "anthropic":
        return _call_anthropic(cfg, system, user, _model, _max_tok, _temp)
    elif provider in ("deepseek", "openai"):
        return _call_openai_compat(cfg, system, user, _model, _max_tok, _temp)
    else:
        raise ValueError(f"不支持的 provider: {provider}")


# ── Anthropic 调用 ────────────────────────────────────────────────────
def _call_anthropic(cfg, system, user, model, max_tokens, temperature):
    import anthropic
    api_key = os.environ.get(cfg.get("api_key_env", "ANTHROPIC_API_KEY")) or cfg.get("api_key", "")
    if not api_key:
        raise RuntimeError(f"环境变量 {cfg.get('api_key_env','ANTHROPIC_API_KEY')} 未设置")
    client = anthropic.Anthropic(api_key=api_key)
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=model, max_tokens=max_tokens, temperature=temperature,
                system=system, messages=[{"role":"user","content":user}],
            )
            logger.debug(f"Anthropic OK | in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
            return resp.content[0].text
        except Exception as e:
            if attempt < 2: time.sleep(2 ** attempt)
            else: raise RuntimeError(f"Anthropic 调用失败: {e}") from e
    return ""


# ── OpenAI 兼容调用（DeepSeek / OpenAI）────────────────────────────────
def _call_openai_compat(cfg, system, user, model, max_tokens, temperature):
    from openai import OpenAI
    provider = cfg.get("provider","deepseek")
    # api_key 优先从环境变量，其次从配置文件（测试用）
    api_key  = os.environ.get(cfg.get("api_key_env","DEEPSEEK_API_KEY")) or cfg.get("api_key","")
    base_url = cfg.get("base_url", "https://api.deepseek.com")
    if not api_key:
        env_name = cfg.get("api_key_env", "OPENAI_API_KEY")
        raise RuntimeError(f"环境变量 {env_name} 未设置")
    client   = OpenAI(api_key=api_key, base_url=base_url)

    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model, max_tokens=max_tokens, temperature=temperature,
                messages=[
                    {"role":"system","content":system},
                    {"role":"user",  "content":user},
                ],
            )
            usage = resp.usage
            logger.debug(
                f"{provider} OK | model={model} "
                f"in={usage.prompt_tokens} out={usage.completion_tokens}"
            )
            message = resp.choices[0].message
            content = message.content or getattr(message, "reasoning_content", None)
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError(f"{provider} 返回空文本（content=null）")
            return content
        except Exception as e:
            status_code = getattr(e, "status_code", None)
            # Key、权限、模型名等 4xx 错误重试也不会成功；429 限流除外。
            if status_code is not None and 400 <= status_code < 500 and status_code != 429:
                raise RuntimeError(f"{provider} 调用失败: {e}") from e
            if attempt < 2: time.sleep(2 ** attempt)
            else: raise RuntimeError(f"{provider} 调用失败: {e}") from e
    return ""


# ── 工具函数 ──────────────────────────────────────────────────────────
def parse_json_safe(text: str | None) -> Optional[list | dict]:
    """宽容JSON解析"""
    import re
    if not isinstance(text, str) or not text.strip():
        logger.error("模型返回空文本，无法解析 JSON")
        return None
    for pattern in [r"```json\s*([\s\S]*?)\s*```", r"```\s*([\s\S]*?)\s*```"]:
        m = re.search(pattern, text)
        if m:
            try: return json.loads(m.group(1))
            except: pass
    try: return json.loads(text.strip())
    except: pass
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try: return json.loads(m.group(0))
        except: pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try: return json.loads(m.group(0))
        except: pass
    return None


def reset_config():
    """重置配置缓存（切换 provider 时调用）"""
    global _api_config
    _api_config = None


def prompt_hash(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\n{user}".encode()).hexdigest()[:12]
