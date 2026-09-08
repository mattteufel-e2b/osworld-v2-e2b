#!/usr/bin/env bash
# Optional shorthand for upstream settings. Native OSWORLD_* values take precedence.
# No agent-model fallback: task judge/simulator defaults remain upstream-owned.
model_override() {
    local source_name="$1" target_name="$2"
    if [ -n "${!source_name:-}" ] && [ -z "${!target_name:-}" ]; then
        export "$target_name=${!source_name}"
    fi
}
model_override EVAL_MODEL OSWORLD_EVAL_MODEL_NAME
model_override EVAL_MODEL_BASE_URL OSWORLD_EVAL_MODEL_BASE_URL
model_override EVAL_MODEL_API_KEY OSWORLD_EVAL_MODEL_API_KEY
model_override USER_SIM_MODEL OSWORLD_USER_SIM_MODEL
model_override USER_SIM_BASE_URL OSWORLD_USER_SIM_BASE_URL
model_override USER_SIM_API_KEY OSWORLD_USER_SIM_API_KEY
model_override USER_SIM_PROVIDER OSWORLD_USER_SIM_PROVIDER
if [ -n "${EVAL_MODEL_BASE_URL:-}" ] && [ -z "${OSWORLD_EVAL_MODEL_PROVIDER:-}" ]; then
    export OSWORLD_EVAL_MODEL_PROVIDER=openai_compatible
fi
if [ -n "${USER_SIM_BASE_URL:-}" ] && [ -z "${OSWORLD_USER_SIM_PROVIDER:-}" ]; then
    export OSWORLD_USER_SIM_PROVIDER=openai_compatible
fi
unset -f model_override
