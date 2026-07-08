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
#   pull <prefix> [dest]  download blob <prefix>/ into data/transfer/<dest> (default <prefix>),
#                         then sha256sum -c the manifest. Refuses any dest outside data/transfer/.
#                         e.g. sync.sh pull predictions
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

cmd_pull() {
  local prefix="${1:-}" dest="${2:-${1:-}}"
  [[ -n "$prefix" ]] || fatal "usage: $0 pull <prefix> [dest]"
  # Ownership guard: received data only ever lands under data/transfer/. Reject
  # anything that could escape it — '..' segments or an absolute path — so
  # `$TRANSFER_ROOT/$dest` is lexically guaranteed to stay inside.
  [[ "$prefix" == *..* || "$dest" == *..* ]] && fatal "prefix/dest must not contain '..'"
  [[ "$dest" == /* ]] && fatal "dest must be relative to data/transfer/, got absolute: $dest"
  local target="$TRANSFER_ROOT/$dest"

  check_disk
  mkdir -p "$target"
  section "pull: $EVAL_BLOB_CONTAINER/$prefix/ -> data/transfer/$dest/"
  # download-batch preserves blob paths, so <prefix>/foo lands at <target>/foo when
  # we point --destination at the parent and strip the prefix via the pattern dir.
  az_blob download-batch --source "$EVAL_BLOB_CONTAINER" --destination "$target" \
    --pattern "$prefix/*" >/dev/null \
    || fatal "blob download-batch failed ($prefix)"
  # Flatten the mirrored <target>/<prefix>/ back to <target>/.
  if [[ -d "$target/$prefix" ]]; then
    cp -a "$target/$prefix/." "$target/" && rm -rf "${target:?}/$prefix" \
      || fatal "could not flatten $target/$prefix"
  fi

  [[ -f "$target/MANIFEST.sha256" ]] || fatal "no MANIFEST.sha256 under $prefix/ — nothing to verify against"
  ( cd "$target" && sha256sum -c MANIFEST.sha256 ) >&2 \
    || fatal "manifest verification FAILED for data/transfer/$dest — transfer is corrupt"
  log "verified data/transfer/$dest against manifest"
}

case "${1:-}" in
  push) shift; cmd_push "$@" ;;
  pull) shift; cmd_pull "$@" ;;
  *) fatal "usage: $0 {push <src> <prefix> | pull <prefix> [dest]}" ;;
esac
