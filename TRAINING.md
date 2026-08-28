# JARVIS verified-training workflow

The goal is measurable specialization, not blind self-training. JARVIS keeps serving with known-good Ollama models while a candidate adapter is built and evaluated separately. A candidate is promoted only when it beats the baseline without regressing safety cases. Web-derived rows without a recognized primary or authoritative citation remain auditable but are quarantined from recall and export.

## 0. Build constitutional SFT and preference data

This pipeline is the local Constitutional AI/RLAIF-inspired phase. The fast model proposes an answer, a separate critic checks it against the operator-controlled Constitution, and the reasoning model revises only when needed. Deterministic checks—not the critic's opinion alone—decide what can be exported.

```powershell
python -m jarvis training cai-init
python -m jarvis training cai-generate --candidate-model qwen3.5:9b --critic-model gpt-oss:20b --reviser-model qwen3:30b
python -m jarvis training cai-verify
python -m jarvis training cai-export
python -m jarvis training cai-status
```

Records are hash-chained and bound to the active `CONSTITUTION.md`, exact scenario pack, model roles, and sample number. Hidden canaries, split labels, and principle labels are not shown to the models. A passing original answer becomes SFT data only. A checked revision can become a DPO preference pair. The export carries native assistant tool calls and their exact tool schemas.

The DPO launcher defaults to a read-only readiness audit:

```powershell
python -m jarvis.preference_train `
  --dataset data\constitutional\export\dpo\train.jsonl `
  --constitution CONSTITUTION.md `
  --base-model Qwen/Qwen3.5-9B `
  --output data\training_runs\constitutional-v1
```

It refuses training until the export and Constitution hashes match and there are at least 100 accepted pairs, including 70 train, 10 validation, 10 test, 20 unique scenarios, and 10 families with per-family caps. Only an explicit `--train` launches QLoRA DPO. The result is a candidate adapter; this command never registers it with Ollama or promotes it.
Every real SFT or DPO download also requires `--revision` with the base model's exact 40-character Hugging Face commit hash. Branches and tags are mutable and are rejected for training.

These hard checks establish provenance and enforce known policies. They do not prove broad semantic safety, truthfulness, or frontier-level capability. Keep adversarial held-out evaluations separate from the generated training set.

## 1. Build a verifiable distillation curriculum

The specialization pipeline is separate from ordinary chat-memory collection. It creates grouped task-family splits, withholds tests from the teacher prompt, schema-checks every response, runs independent checks, and assigns reward `1.0` only when every check passes. It stores final code, tool calls, results, and a short completion summary—not private chain-of-thought.

```powershell
python -m jarvis training distill-init
python -m jarvis training distill-generate --model qwen3-coder:30b
python -m jarvis training distill-status
```

A licensed external teacher can be added later by importing the same exact candidate schema, subject to that provider's terms. The local 30B coder is the zero-cost teacher now.

Review `data\specialization\candidates.jsonl` before verification. Generated code is untrusted. This command executes it with your Windows account inside a temporary directory, not a security boundary:

```powershell
python -m jarvis training distill-verify --allow-host-execution
python -m jarvis training distill-export
```

The export produces:

- `data\specialization\sft`: only candidates that earned reward `1.0`, rendered as observable multi-turn tool traces.
- `data\specialization\grpo`: grouped prompts plus hidden reward specifications for a future isolated GRPO runner. Its manifest deliberately says `requires_isolated_sandbox: true`; do not run model-generated code directly on the host during online RL.

## 2. Build the evaluation gate first

Use objective phrases that a correct answer must contain. Benchmarks run with file writes, memory writes, and host execution disabled.

```powershell
python -m jarvis training eval-add identity "State your name and where you run" --expected JARVIS --expected local
python -m jarvis training eval-list
python -m jarvis training benchmark --model qwen3.5:9b
```

Keep the baseline score. Add cases for the workflows you care about, including refusal and honesty behavior, before training on those workflows.

## 3. Collect only verified trajectories

Recurring learning uses a stricter gate than one-off research: the final brief must cite at least two exact fetched URLs from distinct origins. Only a brief that passes that gate becomes searchable long-term memory. Unverified casual replies are not added to the training ledger.

Use JARVIS normally. It records completed tasks locally in `data/jarvis.db`. A coding task is verified only after inspect → write → successful post-write test. A research task is verified only after a public page is fetched and the final answer cites that exact URL. Secrets are not accepted into training examples.

```powershell
python -m jarvis training status
```

Aim for hundreds of diverse, high-quality examples before the first adapter and thousands before expecting a broad behavior shift. Repeating nearly identical prompts is less useful than covering distinct workflows and failure modes.

## 4. Export a reproducible dataset

```powershell
python -m jarvis training export --min-quality 0.8
python -m jarvis.finetune --dataset data\training_export\train.jsonl --output data\training_runs\qwen3.5-9b-v1 --dry-run
```

The version 3 verified-memory export contains `train.jsonl`, `validation.jsonl`, `test.jsonl`, sanitized verification evidence, and a manifest with counts and SHA-256 hashes. The distillation export instead groups splits by task family, preventing closely related task variants from leaking across train and held-out evaluation.

## 5. Train the first SFT QLoRA candidate

Use a separate Python environment. This downloads the Hugging Face base weights and trains a 4-bit QLoRA adapter; it does not replace an Ollama model.

```powershell
python -m venv .training-venv
.\.training-venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[training]"
python -m jarvis.finetune `
  --dataset data\training_export\train.jsonl `
  --base-model Qwen/Qwen3.5-9B `
  --revision YOUR_40_CHARACTER_COMMIT_HASH `
  --output data\training_runs\qwen3.5-9b-v1 `
  --epochs 2 `
  --max-length 768 `
  --gradient-accumulation 16 `
  --rank 16
```

Start with two epochs and rank 16. More training is not automatically better; over-training a small dataset can reduce general ability and amplify mistakes.
The launcher refuses a real run unless `manifest.json` matches the training file, proves at least 100 total examples with 70 train, 10 validation, and 10 test, and receives an immutable 40-character base-model commit through `--revision`. `--dry-run` remains available early so the data format can be checked without downloading model weights.

The current QLoRA implementation masks user, system, and tool-result tokens from loss and trains only assistant turns, including structured tool calls. That matters for distillation: JARVIS learns what action to take without being trained to imitate user text or verifier output.

## 6. Evaluate before promotion

Evaluate the PEFT adapter with the held-out test split and the same evaluation cases. Never score on the training split. Compare correctness, task completion, latency, and safety behavior against the untouched `qwen3.5:9b` baseline.

Do not assume a Qwen PEFT adapter can be attached directly to an Ollama model tag. Use a conversion path that explicitly supports the exact Qwen base and adapter architecture, validate the merged or adapter artifact, and import it under a new Ollama model name. The base model and revision must exactly match training. See [Ollama's import documentation](https://docs.ollama.com/import).

Keep the old model name until the candidate passes. Register the winner under a versioned name such as `jarvis-qwen3.5-9b:v1`, benchmark again through Ollama, then change `JARVIS_FAST_MODEL`. Rollback is restoring the previous model name.

## What this can and cannot do

LoRA can make an 8B model much better at repeated workflows, response format, tool discipline, and domain vocabulary. It cannot reliably turn an 8B base into a frontier model across every domain. JARVIS gains most of its practical capability from routing, tools, verified memory, retrieval, evaluation, and selective fine-tuning.

GRPO comes after SFT and only for task families with deterministic rewards. The reward environment must run in a disposable VM or container with no secrets, no network, strict CPU/memory/time limits, and a fresh filesystem for each rollout. Serious GRPO may require more accelerator memory than a consumer workstation provides; use local hardware for inference, data curation, bounded QLoRA experiments, and evaluation only when the chosen model and batch configuration fit safely.
