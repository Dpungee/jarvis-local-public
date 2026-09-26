# Local Coding Context threat model

## Purpose and boundary

Local Coding Context gives an Ollama coding-profile turn a small set of relevant
excerpts from operator-registered prior Git projects. It does not train or modify a
model, authorize tools, change routing, share data with remote providers, or make a
source excerpt trusted. The feature is disabled by default and uses a separate local
database under `data/`.

The only model-facing activation path requires all of these conditions:

1. `JARVIS_LOCAL_CODING_CONTEXT=enabled`;
2. the deterministic router marks the request as coding intent; and
3. the selected provider is exactly `ollama`.

`shadow` performs the same bounded lookup but returns no model-facing block. All
other modes, non-coding routes, and providers receive the original message list
unchanged. The coding-intent marker follows a bounded coding task that starts on or
falls back to the local fast model, without changing the selected profile or model.
Retrieval is applied after strategy-transfer preparation and separately for each
provider attempt, so local excerpts cannot enter a later cloud fallback.
The library is forced off while the sealed strategy-transfer trial mode is active,
so it cannot change either experimental arm or invalidate a prompt receipt.

## Protected assets

- Credentials, private identifiers, ignored or untracked files, and repositories
  the operator did not register.
- Cloud/subscription prompts and non-coding Ollama prompts.
- Tool authorization, approval, routing, verification, memory, and policy state.
- Host responsiveness, disk space, RAM, and model context capacity.
- Accurate source provenance and truthful verification status.

## Untrusted inputs

- Every registered repository name, path, file name, source file, comment, string,
  symbol, and Git metadata record.
- The operator query used for retrieval.
- SQLite files or sidecars that may have been replaced outside JARVIS.
- Git configuration and repository metadata. Indexing therefore uses only fixed
  non-mutating Git subcommands through an OS-administered executable, disables
  prompts and hooks, and accepts no repository-supplied command.

Source text may contain prompt injection. It is serialized inside an explicit
`untrusted_local_coding_context` data block that states it has no authority. Runtime
tool scope, approvals, verification, and policy remain deterministic; retrieved text
cannot widen them.

## Deterministic controls

- Registration is explicit, limited to 32 ordinary, link-free Git project roots,
  and binds the root device/inode identity.
- Only current working-tree content at Git-tracked paths is eligible. Paths must be
  relative, canonical UTF-8 paths and remain within the registered root. The file
  digest identifies the exact indexed bytes; the checked-out commit is retained as
  a repository-state anchor and is not claimed to contain uncommitted bytes.
- A closed source-extension/name allowlist and denied directory/name list exclude
  credentials, environment files, dependencies, build output, local data, logs,
  VCS metadata, and common credential stores.
- Each file must be an ordinary, non-linked UTF-8 file of at most 256 KiB and must
  remain unchanged across the bounded read. Secret-shaped content and private local
  identifiers reject the whole file instead of being indexed.
- A project may expose at most 25,000 tracked paths and 200 MiB of accepted source.
  Individual chunks are at most 2,400 characters and 80 lines.
- Each indexed chunk carries a SHA-256 digest that is checked again before prompt
  rendering; mismatched or structurally invalid index rows are omitted.
- Retrieval uses local SQLite FTS5 and symbol/path weighting. It starts no model,
  embedding service, network request, background worker, or project command.
- At most 12 results and 4,096 approximate tokens are configurable; defaults are six
  results and 2,048 tokens. The rendered block is checked against four characters
  per configured token before provider dispatch.
- Model-facing provenance contains only a registration slug, relative path, file
  SHA-256, checked-out commit, and line range. Absolute roots never enter prompts.
- Source is labeled `tracked source; test status not attested`; a commit or test file
  is not misrepresented as evidence that tests passed.
- Retrieval telemetry stores only mode, count, duration, and injected character
  count. It stores no query or retrieved source in the telemetry table.

## Failure and recovery

Missing, malformed, busy, linked, substituted, or unsupported index state fails open
for availability but closed for data: JARVIS sends the unchanged request without
including index content. Indexing errors roll back their SQLite transaction.
Removing a registration deletes its indexed files and search rows without touching
the source project.

Rollback is `JARVIS_LOCAL_CODING_CONTEXT=disabled`; this prevents index access and
restores the pre-feature provider request. The separate database may then be removed
through normal local data maintenance without changing memory or model state.

## Evaluation gates

- Byte-equivalent messages for disabled, shadow, non-coding, and every non-Ollama
  provider route.
- No local context retained by strategy-transfer state or a provider fallback.
- No indexing of untracked, secret-shaped, private-identifier, oversized, binary,
  hard-linked, symlinked, traversal, unsupported, or excluded paths.
- Bounded prompt size, result count, source bytes, file count, and project count.
- Incremental update/removal correctness and exact digest/commit provenance.
- Focused retrieval latency measurement on a synthetic multi-file corpus, followed
  by the complete deterministic test suite.

These gates demonstrate bounded retrieval and isolation. They do not prove that a
9B model matches a larger model, that retrieved code is correct, or that historical
code is appropriate for a new task.
