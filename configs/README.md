# configs/

Committed configuration and specs for the pipeline. Full tree in the root
[README Layout](../README.md#layout).

- `settings.conf` — workspace-global operational tunables (loaded by all scripts).
- `annotation/domains/*.yaml` — per-domain pragmata annotation task configs.
- `annotation/querygen_specs/*.yaml` + `_runtime.yaml` — per-domain querygen specs + shared runtime.
- `annotation/users.json` / `users.secrets.json` — roster + passwords (**gitignored**; committed `.example` templates show the shape).
- `eval/` — stub for the evaluation stage.
