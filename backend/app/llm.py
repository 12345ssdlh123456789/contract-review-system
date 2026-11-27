# ==========================================================
#  LLM 客户端
# ----------------------------------------------------------
#  企业级要点：
#  1. 超时分级：连接超时短（10s）、读取超时长（120s）。
#     LLM 首 token 慢是常态，不能用一个 30s 超时糊过去。
#  2. 重试只重试"可重试"的错误（超时、5xx、限流），
#     401/403 重试一万次也没用。
#  3. 所有调用都要能拿到 token 数 —— 成本计量靠它。
#  4. 供应商无关：走 OpenAI 兼容协议，换服务商只改 .env 三行。
# ==========================================================
import json
import re
import time
from dataclasses import dataclass

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from .config import config


@dataclass
class LLMResult:
    ok: bool
    text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    error: str = ""
    model: str = ""


_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.LLM_API_KEY,
            base_url=config.LLM_BASE_URL,
            timeout=config.LLM_READ_TIMEOUT,
            max_retries=0,  # 我们自己控制重试策略，不让 SDK 偷偷重试
        )
    return _client


# 智谱 glm 系列定价（元 / 千 token），用于成本估算。
# 免费模型记 0，但字段仍然要算，因为将来换付费模型时这套机制不用改。
PRICE_PER_1K = {
    "glm-4-flashx": (0.0, 0.0),
    "glm-4-flash": (0.0, 0.0),
    "glm-4-air": (0.001, 0.001),
    "glm-4-plus": (0.05, 0.05),
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    in_price, out_price = PRICE_PER_1K.get(model, (0.0, 0.0))
    return round(
        prompt_tokens / 1000 * in_price + completion_tokens / 1000 * out_price, 6
    )


def chat(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    retries: int = 2,
) -> LLMResult:
    """
    发一次对话请求。带指数退避重试。
    """
    model = model or config.LLM_MODEL
    temperature = config.LLM_TEMPERATURE if temperature is None else temperature
    max_tokens = max_tokens or config.LLM_MAX_TOKENS

    if not config.LLM_API_KEY:
        return LLMResult(ok=False, error="未配置 LLM_API_KEY，请在 backend/.env 中填写")

    last_error = ""
    started = time.perf_counter()

    for attempt in range(retries + 1):
        try:
            resp = get_client().chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            latency = int((time.perf_counter() - started) * 1000)
            usage = resp.usage
            content = (resp.choices[0].message.content or "").strip()
            return LLMResult(
                ok=True,
                text=content,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
                latency_ms=latency,
                model=model,
            )
        except APITimeoutError:
            last_error = f"模型响应超时（{config.LLM_READ_TIMEOUT:.0f}s）"
        except APIConnectionError as exc:
            last_error = f"无法连接模型服务：{exc}"
        except APIStatusError as exc:
            code = exc.status_code
            last_error = f"模型服务返回 {code}：{exc.message}"
            # 4xx（除 429）是不可重试错误，直接退出
            if 400 <= code < 500 and code != 429:
                break
        except Exception as exc:  # noqa: BLE001
            last_error = f"调用模型失败：{exc}"
            break

        if attempt < retries:
            time.sleep(2 ** attempt)  # 1s, 2s 指数退避

    return LLMResult(
        ok=False,
        error=last_error,
        latency_ms=int((time.perf_counter() - started) * 1000),
        model=model,
    )


# ---------------------------------------------------------------- JSON 提取
def extract_json(text: str) -> dict | None:
    """
    从模型输出里抠出 JSON。

    模型经常会：
    - 用 ```json ... ``` 包裹
    - 前面加一句"好的，以下是审查结果："
    - 结尾多一个逗号
    这些都要容错，否则好好的结果就白扔了。
    """
    if not text:
        return None

    # 去掉 markdown 代码块围栏
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)

    # 直接试
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 抠第一个 { 到最后一个 }
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # 去掉尾随逗号再试一次
            candidate2 = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(candidate2)
            except json.JSONDecodeError:
                return None
    return None
