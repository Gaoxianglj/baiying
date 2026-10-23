"""按配置组装模型网关与 embedding 客户端。"""

from __future__ import annotations

from companion.config import Config
from companion.models.codex_app_server import CodexAppServerBackend
from companion.models.gateway import ModelGateway, PricedBackend
from companion.models.openai_compat import ModelError, OpenAICompatClient
from companion.models.usage import UsageLedger


def build_gateway(config: Config) -> ModelGateway:
    sub_cfg = config.models.subscription
    subscription: CodexAppServerBackend | None = None
    subscription_error: str | None = None
    if not sub_cfg.enabled:
        subscription_error = "配置中已关闭"
    else:
        try:
            subscription = CodexAppServerBackend.from_codex(
                sub_cfg.codex_command,
                config.data_dir / "codex",
                model=sub_cfg.model,
                effort=sub_cfg.effort,
                timeout=sub_cfg.timeout_seconds,
            )
        except ModelError as e:
            subscription_error = str(e)

    fb = config.models.fallback
    fallback = None
    if fb.api_key is not None and fb.model:
        fallback = PricedBackend(
            client=OpenAICompatClient(
                name="fallback",
                base_url=fb.base_url,
                model=fb.model,
                api_key=fb.api_key.get_secret_value().strip(),
                timeout=fb.timeout_seconds,
                extra_body=fb.extra_body,
            ),
            price_in=fb.price_input_per_m,
            price_out=fb.price_output_per_m,
            monthly_budget_usd=fb.monthly_budget_usd,
        )

    local_cfg = config.models.local
    local = None
    if local_cfg.enabled and local_cfg.model:
        local = PricedBackend(
            client=OpenAICompatClient(
                name="local", base_url=local_cfg.base_url, model=local_cfg.model, timeout=300.0
            ),
            price_in=0.0,
            price_out=0.0,
        )

    return ModelGateway(
        subscription=subscription,
        subscription_error=subscription_error,
        switch_at_used_percent=sub_cfg.switch_at_used_percent,
        fallback=fallback,
        local=local,
        ledger=UsageLedger(config.data_dir / "usage.jsonl"),
    )


def build_embedder(config: Config) -> OpenAICompatClient:
    emb = config.models.embedding
    return OpenAICompatClient(
        name="embedding", base_url=emb.base_url, model=emb.model, timeout=emb.timeout_seconds
    )
