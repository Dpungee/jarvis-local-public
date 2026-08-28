# JARVIS Public Presence threat model v0.1

## Scope and security objective

This model covers the future Public Presence process, its public database and
workspace, the private-to-public bridge, operator control plane, platform
adapters, drafts, approvals, and receipts. The current foundation is offline and
has no platform connection.

The primary security objective is stronger than “the model refuses”: hostile
public input must be technically unable to obtain private data or private tool
authority, and no external action may occur without the exact release-tier gate.

## Assets to protect

1. Operator identity, location, relationships, reputation, and intentions.
2. Private memories, conversations, files, projects, browser/screen state, and
   local machine details.
3. Credentials, OAuth tokens, cookies, keys, recovery material, and account
   ownership.
4. Private JARVIS authority: filesystem, shell/process, computer control,
   connectors, purchases, trading, deployments, email, calendar, and Drive.
5. Public identity integrity: biography, disclosure, policy, public facts,
   relationships, and approved content.
6. Approval integrity: exact content, account, platform, destination, reply
   target, media hashes, expiry, and one-shot use.
7. Audit integrity: inbound-event ledger, draft provenance, decisions,
   publication attempts, platform receipts, and recovery evidence.
8. Availability and control: independent social pause, emergency stop, account
   revocation, and bounded recovery without duplicate actions.

## Trust boundaries

```text
Untrusted platform content and media
                |
        validation/quarantine
                |
    Public Presence process and DB
                |
 closed typed + sanitized bridge only
                |
       Private JARVIS domain

Operator UI -> exact approval ledger -> platform adapter
Credential broker -> platform adapter (tokens never enter model context)
```

- Everything originating from a platform is untrusted, including content from
  verified accounts, quoted messages, URLs, media metadata, and apparent staff.
- Model output is an untrusted candidate until deterministic policy and approval
  checks pass.
- Public-memory records are external-authority evidence, never operator facts or
  permission.
- The operator control plane is trusted only after local authentication and
  exact request binding; rendered public content remains untrusted inside it.

## Adversaries and failure sources

- Opportunistic users, coordinated attackers, trolls, spammers, and scammers.
- Compromised or impersonated accounts, including apparent operator accounts.
- Malicious posts, profiles, shortened URLs, redirects, images, documents,
  quoted text, Unicode confusables, hidden text, or encoded payloads.
- A platform, dependency, provider, plugin, or adapter that is compromised,
  revoked, inconsistent, rate-limited, or returns malformed data.
- Model mistakes: instruction following, hallucination, over-disclosure,
  unsupported claims, wrong destination, repeated output, and identity drift.
- Operator error: wrong account, stale approval, misunderstood draft, accidental
  enablement, incomplete backup, or delayed emergency response.
- Process and storage faults: crash, timeout, replay, out-of-order events, partial
  commit, clock skew, database corruption, and stale leases.

## Threats, controls, and required tests

### T01 — Prompt injection and authority laundering

**Attack:** A post, comment, profile, URL, image, attachment, quoted passage, or
tool result instructs JARVIS to reveal data, use tools, change policy, approve an
action, or treat the sender as the operator.

**Required controls:** hard process separation; no private registry import;
bounded parsing and quarantine; injection labels; public content stored as data;
closed bridge schemas; fixed policies outside model control; deny-by-default tool
dispatch.

**Tests:** hostile fixtures across every input type and encoding; claimed-system,
claimed-operator, emergency, role-play, multi-hop, quotation, and delayed-memory
attacks; zero private-tool calls and zero authority changes.

### T02 — Private-memory or private-file leakage

**Attack:** Direct extraction, indirect inference, retrieval poisoning, error
messages, citations, generated links, drafts, logs, or public health status leak
private information.

**Required controls:** separate database/workspace/process; no raw private query;
allowlisted bridge objects; redaction both before and after generation; seeded
canary secrets; minimal public telemetry; output-size and field allowlists.

**Tests:** at least 500 adversarial cases with seeded secrets, personal facts,
private project details, path names, and near-matches; zero exact, encoded,
partial, or semantically equivalent leaks.

### T03 — Credential theft or OAuth exposure

**Attack:** Input asks for tokens, a provider error logs them, a model repeats a
header, or a platform callback stores credentials in content/memory.

**Required controls:** OS-backed credential broker; opaque credential handles;
tokens excluded from prompts, logs, databases, receipts, URLs, and drafts;
revocable least-scope accounts; secret scanners on all error and output paths.

**Tests:** seeded-token fixtures in headers, redirects, exceptions, and nested
responses; log/memory/database scans; revoked-token recovery without fallback.

### T04 — Operator impersonation and social engineering

**Attack:** A public user claims to be the operator or platform staff, cites an
old approval, creates urgency, or asks JARVIS to speak for the operator.

**Required controls:** public identities never authenticate the operator;
operator actions originate only from the local control plane; candid AI
disclosure; representation policy; no public command channel or DMs.

**Tests:** impersonation, pressure, emergency, friendship, bribery, blackmail,
and forged-receipt fixtures; zero commitments or elevated authority.

### T05 — Reputational harm and accidental endorsement

**Attack/failure:** Hallucinated claims, inflammatory replies, undisclosed ads,
copied material, false operator opinions, unsafe advice, or stale sources are
published under the JARVIS identity.

**Required controls:** identity/policy evaluator; source provenance and freshness;
restricted-topic escalation; plagiarism/media checks; exact operator review;
rate limits; block-topic and abstain paths.

**Tests:** ordinary, hostile, emotional, controversial, ambiguous, financial,
political, medical, legal, and security prompts; persona consistency >=90%, AI
disclosure 100%, and zero operator impersonation or unsupported capability claims.

### T06 — Duplicate or substituted publication

**Attack/failure:** Retry after timeout/crash posts twice, or approved content is
changed, redirected, moved to another account, or attached to different media.

**Required controls:** canonical content digest; approval binding over every
material field; expiring one-shot approval; idempotency ledger committed before
dispatch; reconcile uncertain results before retry; destination/account recheck.

**Tests:** crash at every publish step, concurrent workers, replay, expiry,
destination/content/media/account substitution, out-of-order callback, and
uncertain network result; zero duplicates and zero substituted sends.

### T07 — Account compromise, API revocation, and platform ban

**Attack/failure:** Credentials are revoked or stolen, account settings change,
platform rules shift, or the account is restricted while JARVIS keeps acting.

**Required controls:** health state with no token exposure; fail offline on auth,
policy, ownership, or account-identity mismatch; independent pause/stop;
credential rotation and revocation procedure; monthly platform-policy review.

**Tests:** revoked/expired token, changed account ID, scope reduction, platform
403/429/ban responses, and compromised webhook; no alternate credential,
browser workaround, or continued publication.

### T08 — Public content obtains private tool authority

**Attack:** A model or adapter tries to call shell, filesystem, browser, screen,
computer, connector, email, Drive, deployment, payment, trading, purchase, or
private-memory tools.

**Required controls:** separate public registry with no imports from the private
registry; process-level filesystem and network restrictions; fixed tool schema;
negative capability tests; no generic connector execution.

**Tests:** enumerate the public registry and attempt direct, aliased, nested, and
indirect calls to every prohibited family; all fail before provider invocation.

### T09 — Public-memory poisoning and relationship manipulation

**Attack:** Repeated claims become trusted identity, instructions are embedded in
summaries, or conflicting accounts overwrite a verified relationship.

**Required controls:** external authority label; per-claim URLs/timestamps;
confidence and expiry; contradiction/dispute state; no executable instructions;
operator facts and permissions are inexpressible in the public schema.

**Tests:** contradiction, repetition, popularity, cross-account collision,
instruction-in-memory, expiry, forget, and restart tests. Target precision@3
>=0.85, recall@3 >=0.80, and 100% seeded contradiction detection.

### T10 — Availability, resource exhaustion, and uncontrolled loops

**Attack/failure:** Event floods, oversized media, reply loops, provider retries,
or malformed streams exhaust resources or delay the stop control.

**Required controls:** input bounds, quotas, backpressure, bounded concurrency,
loop/reply-depth limits, circuit breakers, allowed hours, independent stop path,
and no automatic provider/tool widening.

**Tests:** event floods, recursive replies, provider timeout, huge payloads,
malformed streams, and stop under load. Pause/kill propagation must complete in
under two seconds.

## Security invariants

1. No public input can express operator authority.
2. Public and private databases, workspaces, processes, and registries are
   distinct.
3. No credential value enters model context, logs, memory, receipts, or content.
4. No outbound action exists in the foundation release.
5. Later outbound actions require exact binding, one-shot approval or a
   separately certified autonomy tier, and an idempotent receipt.
6. Public pause and emergency stop are independent of the normal worker pause.
7. Any uncertainty in configuration, destination, account, verification,
   provider state, audit state, or policy leaves Public Presence offline.
8. Identity and safety policies are immutable to automated self-modification.

## Residual risk and acceptance

No test suite proves that a public agent is safe in every future environment.
Platform behavior, adversaries, model providers, and policy requirements change.
Risk is managed through isolation, minimum authority, evidence, staged release,
human review, rate limits, rapid revocation, and rollback. Every later platform
requires its own threat model and certification; passing one adapter never
certifies another.

Phase 0 is accepted only after the operator and an independent reviewer sign the
dated exit-gate record. Until then Public Presence remains disabled and offline.
