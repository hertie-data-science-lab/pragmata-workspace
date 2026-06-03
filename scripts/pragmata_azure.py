#!/usr/bin/env python3
"""Thin wrapper around the pragmata CLI that adds azure_openai provider support.

Pragmata's API_KEY_ENV_VARS registry (in pragmata.core.settings.settings_base)
does not include azure_openai, so invoking the CLI with
`--model-provider azure_openai` raises:

    ValueError: Unsupported API key name: azure_openai. Supported: anthropic, ...

This wrapper monkey-patches the registry at import time to map azure_openai →
AZURE_OPENAI_API_KEY, then dispatches to pragmata's Typer app unchanged.

LangChain's init_chat_model already supports Azure natively via AzureChatOpenAI;
it picks up AZURE_OPENAI_ENDPOINT and OPENAI_API_VERSION from the environment,
so nothing else needs adapting. Note the asymmetric env-var naming —
AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT use the AZURE_ prefix but
OPENAI_API_VERSION does not. This is a LangChain quirk, not ours. We therefore do NOT pass --base-url
on the pragmata CLI when using azure_openai (pragmata would forward it as
`base_url=` which AzureChatOpenAI rejects in favour of `azure_endpoint=`).

The wrapper also auto-loads the workspace .env (and config/workspace.env) via
scripts/lib/workspace.py so credentials and endpoint config don't need to be
shell-sourced before each run.

Delete this wrapper when pragmata adds native azure_openai support upstream
(1-line change to API_KEY_ENV_VARS, see workspace README).

Usage:
    .venv/bin/python scripts/pragmata_azure.py querygen gen-queries \\
        --config-path querygen_specs/bildung-und-next-generation.yaml \\
        --model-provider azure_openai \\
        --planning-model gpt-5.4-mini \\
        --realization-model gpt-5.4-mini \\
        --n-queries 50
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import workspace as ws

ws.load_env()  # config/workspace.env + .env; existing env wins. Before any key resolution.

# Register azure_openai in pragmata's provider registry. Must happen BEFORE any
# CLI command resolves an API key.
import pragmata.core.settings.settings_base as _sb

_sb.API_KEY_ENV_VARS.setdefault("azure_openai", "AZURE_OPENAI_API_KEY")

from pragmata.cli.app import app

if __name__ == "__main__":
    app()
