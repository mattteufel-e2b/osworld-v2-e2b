"""Runner-local Bedrock Runtime API-key authentication for upstream evaluators.

Select ``bedrock_bearer`` as the evaluator or simulator provider and set its
``*_API_KEY_ENV`` to the variable holding the Bedrock bearer key. Model names
must be Bedrock model/inference-profile IDs. Region selection follows the
upstream Bedrock backend: AWS_DEFAULT_REGION, then AWS_REGION, then us-east-1.
"""

from __future__ import annotations

import os


def install() -> None:
    """Register the opt-in backend without changing the pinned upstream files."""
    if "bedrock_bearer" not in (
        os.getenv("OSWORLD_EVAL_MODEL_PROVIDER"),
        os.getenv("OSWORLD_USER_SIM_PROVIDER"),
    ):
        return
    import anthropic

    from desktop_env.evaluators.backends import register_backend
    from desktop_env.evaluators.backends.base import ModelBackend
    from desktop_env.evaluators.backends.bedrock_backend import BedrockBackend

    class BearerAnthropicBedrock(anthropic.AnthropicBedrock):
        def _prepare_request(self, request) -> None:
            # AnthropicBedrock's hook adds SigV4. Keep its URL/body conversion
            # and Messages response parser, replacing only that signing hook.
            request.headers["Authorization"] = self._custom_headers["Authorization"]

    @register_backend("bedrock_bearer")
    class BedrockBearerBackend(BedrockBackend):
        def __init__(self, config) -> None:
            ModelBackend.__init__(self, config)
            if not config.api_key:
                raise ValueError("bedrock_bearer requires a Bedrock Runtime API key")
            self._client = BearerAnthropicBedrock(
                aws_region=(
                    os.getenv("AWS_DEFAULT_REGION")
                    or os.getenv("AWS_REGION")
                    or "us-east-1"
                ),
                default_headers={"Authorization": f"Bearer {config.api_key}"},
            )
            # Require the caller's exact Bedrock ID; alias resolution depends
            # on upstream's independently maintained agent model mapping.
            self._model_name = config.model
