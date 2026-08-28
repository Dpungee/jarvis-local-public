---
name: local-ai-engineering
description: Hardware-aware local inference, quantization, model routing, context management, tool calling, evaluation, and performance optimization.
version: 1.0.0
---
# Local AI Engineering

## When to use

Use for Ollama or other local inference servers, GPU/CPU sizing, quantization, context windows, KV cache, throughput, latency, model routing, RAG, tool calling, and agent reliability.

## Workflow

1. Record hardware, usable VRAM/RAM, operating system, inference runtime/version, model architecture and parameter count, quantization, context, concurrency, and latency target.
2. Measure cold start, warm time-to-first-token, decode tokens/second, peak VRAM/RAM, prompt length, output length, and task success. Never optimize from one subjective chat.
3. Fit the working set: model weights, KV cache, runtime buffers, vision/projector components, and concurrent requests. Leave memory headroom for the OS and applications.
4. Improve in order: right-size model/profile routing; reduce unnecessary context; choose a validated quantization; tune batch/concurrency; control keep-alive; then consider CPU/GPU offload.
5. Evaluate quality on held-out tasks that match actual use, including tool-call schema adherence, long-context retrieval, coding verification, hallucination, and refusal behavior.
6. Keep model selection separate from safety and verification gates. A faster model may plan or summarize, but consequential actions still use the same deterministic controls.
7. Promote changes only when repeatable benchmarks improve the target metric without unacceptable quality, stability, thermals, or desktop responsiveness regressions.

## Diagnostic checklist

Distinguish model inference, prompt construction, network/API latency, tool execution, disk I/O, context compaction, retries, and UI streaming. Capture timestamps around each boundary before changing hardware or models.

## Verification

Report the benchmark corpus, repetitions, percentiles, resource peaks, quality gate, and exact before/after configuration. Mark vendor/version-sensitive advice for current documentation lookup.
