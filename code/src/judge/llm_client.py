"""供应商可配置的LLM调用层。

参考 demo/code/core/llm_client.py 已验证的provider抽象（anthropic /
deepseek / openai三选一，通过配置文件切换），但修复了它的一个问题：
demo的配置路径是相对CWD的 "configs/api.yaml"，谁在哪个目录下跑代码，
路径就可能读不到；这里默认相对本包目录解析，不受调用方所在目录影响。

换模型/换供应商只改 configs/api.yaml，不需要碰这个文件或 sampling.py。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import yaml

_DEFAULT_CONFIG_PATH = Path(__file__).parent / "configs" / "api.yaml"
_DEFAULT_CONFIG = {
    "provider": "anthropic",
    "model": "claude-opus-4-8",
    "max_tokens": 2048,
    "temperature": 0.3,
    "api_key_env": "ANTHROPIC_API_KEY",
}

_config_cache: dict | None = None
_config_cache_path: Path | None = None

MAX_RETRIES = 3


def load_api_config(config_path: Path | str | None = None) -> dict:
    """加载并缓存API配置。传入 config_path 可覆盖默认路径（主要供测试用）。"""
    global _config_cache, _config_cache_path
    path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    if _config_cache is not None and _config_cache_path == path:
        return _config_cache
    if path.exists():
        _config_cache = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        _config_cache = dict(_DEFAULT_CONFIG)
    _config_cache_path = path
    return _config_cache


def reset_config_cache() -> None:
    """重置配置缓存（测试或运行时切换provider时调用）。"""
    global _config_cache, _config_cache_path
    _config_cache = None
    _config_cache_path = None


def call_llm(
    system: str,
    user: str,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    config_path: Path | str | None = None,
) -> str:
    """统一LLM调用接口，按 configs/api.yaml 的 provider 路由到对应实现。"""
    cfg = load_api_config(config_path)
    provider = cfg.get("provider", "anthropic").lower()
    _model = model or cfg.get("model", _DEFAULT_CONFIG["model"])
    _max_tokens = max_tokens or cfg.get("max_tokens", _DEFAULT_CONFIG["max_tokens"])
    _temperature = temperature if temperature is not None else cfg.get("temperature", _DEFAULT_CONFIG["temperature"])

    if provider == "anthropic":
        return _call_anthropic(cfg, system, user, _model, _max_tokens, _temperature)
    if provider in ("deepseek", "openai"):
        return _call_openai_compat(cfg, system, user, _model, _max_tokens, _temperature)
    raise ValueError(f"不支持的 provider: {provider}（支持 anthropic/deepseek/openai）")


def _call_anthropic(cfg: dict, system: str, user: str, model: str, max_tokens: int, temperature: float) -> str:
    import anthropic

    api_key_env = cfg.get("api_key_env", "ANTHROPIC_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"环境变量 {api_key_env} 未设置")
    client = anthropic.Anthropic(api_key=api_key)

    kwargs = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
              "messages": [{"role": "user", "content": user}]}
    if system:
        kwargs["system"] = system

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.messages.create(**kwargs)
            return resp.content[0].text
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Anthropic 调用失败（已重试{MAX_RETRIES}次）: {last_err}") from last_err


def _call_openai_compat(cfg: dict, system: str, user: str, model: str, max_tokens: int, temperature: float) -> str:
    from openai import OpenAI

    provider = cfg.get("provider", "deepseek")
    api_key_env = cfg.get("api_key_env", "DEEPSEEK_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"环境变量 {api_key_env} 未设置")
    base_url = cfg.get("base_url", "https://api.deepseek.com")
    client = OpenAI(api_key=api_key, base_url=base_url)

    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": user})

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=model, max_tokens=max_tokens, temperature=temperature, messages=messages,
            )
            message = resp.choices[0].message
            content = message.content or getattr(message, "reasoning_content", None)
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError(f"{provider} 返回空文本（content=null）")
            return content
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"{provider} 调用失败（已重试{MAX_RETRIES}次）: {last_err}") from last_err
