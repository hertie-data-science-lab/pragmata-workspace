#!/bin/bash
# scripts/eval/sync.sh {push|pull} ...
#
# Moves eval data between the CPU annotation box (BSt Azure) and the GPU eval box
# (Hertie) over a shared Azure Blob container — the two VMs have no route to each
# other, but both reach Blob over HTTPS. Git carries code (control plane); this
# carries data (data plane). Every transfer is pinned by a sha256 manifest and
# re-verified on the receiving end.
#
#   push <src> <prefix>   upload a local tree to blob <prefix>/ (+ <prefix>/MANIFEST.sha256)
#                         reads pragmata tool trees in place; never writes into them.
#                         e.g. sync.sh push data/annotation/exports exports
#   pull <prefix>         download blob <prefix>/ into data/transfer/<prefix>/, then verify.
#                         e.g. sync.sh pull predictions
#   verify <prefix>       re-check data/transfer/<prefix>/ against its manifest (sha256sum -c)
#
# Auth: EVAL_BLOB_ACCOUNT / EVAL_BLOB_CONTAINER / EVAL_BLOB_SAS in .env (SAS is
# data-plane, no ARM rights needed). Requires the `az` CLI on both boxes.

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
cd_root
require_env EVAL_BLOB_ACCOUNT EVAL_BLOB_CONTAINER EVAL_BLOB_SAS

command -v az >/dev/null 2>&1 \
  || fatal "az CLI not found. Install the Azure CLI (or adapt this to azcopy) — needed on both boxes."

TRANSFER_ROOT="$DATA_DIR/transfer"

# az invocation shared args (account + sas + container).
az_blob() { az storage blob "$@" --account-name "$EVAL_BLOB_ACCOUNT" --sas-token "$EVAL_BLOB_SAS"; }

# Sorted per-file sha256 manifest of a tree, relative to it, to a given output file.
# Excludes any existing MANIFEST.sha256 so the manifest never lists itself.
write_manifest() {
  local root="$1" out="$2"
  ( cd "$root" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum ) > "$out" \
    || fatal "manifest computation failed for $root"
}

cmd_push() {
  local src="${1:-}" prefix="${2:-}"
  [[ -n "$src" && -n "$prefix" ]] || fatal "usage: $0 push <src> <prefix>"
  [[ -d "$src" ]] || fatal "no such source tree: $src"
  [[ "$prefix" == *..* ]] && fatal "prefix must not contain '..': $prefix"

  local manifest; manifest="$(mktemp)"
  trap 'rm -f "$manifest"' RETURN
  write_manifest "$src" "$manifest"
  local nfiles snap
  nfiles="$(wc -l < "$manifest" | tr -d ' ')"
  snap="$(sha256sum "$manifest" | cut -d' ' -f1)"

  section "push: $src -> $EVAL_BLOB_CONTAINER/$prefix/ ($nfiles files)"
  az_blob upload-batch --destination "$EVAL_BLOB_CONTAINER" --destination-path "$prefix" \
    --source "$src" --overwrite >/dev/null \
    || fatal "blob upload-batch failed ($src -> $prefix)"
  az_blob upload --container-name "$EVAL_BLOB_CONTAINER" --name "$prefix/MANIFEST.sha256" \
    --file "$manifest" --overwrite >/dev/null \
    || fatal "manifest upload failed ($prefix/MANIFEST.sha256)"

  log "pushed $nfiles files"
  # The pin line for a future reproducibility bundle: one hash for the whole snapshot.
  printf 'snapshot %s: sha256:%s  (%s files)\n' "$prefix" "$snap" "$nfiles"
}

# Ownership guard: resolve the intended path and assert it stays under
# data/transfer/, so received data can never land in a tool's own tree (or
# anywhere else) regardless of '..' segments or an absolute-looking prefix.
transfer_target() {
  local prefix="${1:?}" target
  target="$(realpath -m "$TRANSFER_ROOT/$prefix")"
  [[ "$target" == "$TRANSFER_ROOT"/* ]] || fatal "refusing path outside data/transfer/: $prefix"
  printf '%s\n' "$target"
}

cmd_pull() {
  local prefix="${1:-}"
  [[ -n "$prefix" ]] || fatal "usage: $0 pull <prefix>"
  transfer_target "$prefix" >/dev/null   # guard before any I/O

  check_disk
  mkdir -p "$TRANSFER_ROOT"
  section "pull: $EVAL_BLOB_CONTAINER/$prefix/ -> data/transfer/$prefix/"
  # download-batch preserves the blob path, so <prefix>/foo lands directly at
  # data/transfer/<prefix>/foo — no flattening needed.
  az_blob download-batch --source "$EVAL_BLOB_CONTAINER" --destination "$TRANSFER_ROOT" \
    --pattern "$prefix/*" >/dev/null \
    || fatal "blob download-batch failed ($prefix)"
  cmd_verify "$prefix"
}

cmd_verify() {
  local prefix="${1:-}"
  [[ -n "$prefix" ]] || fatal "usage: $0 verify <prefix>"
  local target; target="$(transfer_target "$prefix")"
  [[ -f "$target/MANIFEST.sha256" ]] || fatal "no MANIFEST.sha256 under data/transfer/$prefix — nothing to verify against"
  ( cd "$target" && sha256sum -c MANIFEST.sha256 ) >&2 \
    || fatal "manifest verification FAILED for data/transfer/$prefix — data is corrupt"
  log "verified data/transfer/$prefix against manifest"
}

case "${1:-}" in
  push)   shift; cmd_push "$@" ;;
  pull)   shift; cmd_pull "$@" ;;
  verify) shift; cmd_verify "$@" ;;
  *) fatal "usage: $0 {push <src> <prefix> | pull <prefix> | verify <prefix>}" ;;
esac
