# Public publishing

JARVIS Local is published through a protected, squash-only pull request. Never push
directly from a development checkout and never treat a local pass as release
authority. The reviewed candidate, GitHub-created squash commit, and release tag are
three distinct objects; verify each one before advancing.

## Preconditions

Before pushing a candidate, the operator must verify all of the following:

- GitHub's account setting **Keep my email addresses private** is enabled.
- GitHub's account setting that blocks command-line pushes exposing a private email
  is enabled.
- The publishing checkout uses the approved GitHub no-reply identity for both author
  and committer metadata.
- The release tag is bounded ASCII SemVer: `vMAJOR.MINOR.PATCH` with optional SemVer
  prerelease/build identifiers and no shell metacharacters. The guard rejects any tag
  outside that closed grammar before rendering a copy/paste command.
- The repository permits squash merges only. Merge commits and rebase merges are
  disabled.
- Protected `main` requires these six strict contexts: Secret and privacy scan;
  Windows Python 3.11, 3.12, and 3.13; Coverage, browser syntax, and distribution;
  and the aggregate CodeQL gate backed by the Actions, JavaScript/TypeScript, and
  Python analyses.
- Administrator enforcement, linear history, conversation resolution, and the
  prohibition on force pushes and branch deletion are active.

Account email privacy is a human-verified prerequisite because repository source and
CI cannot prove that private account setting. Stop if it is not confirmed: merely
opening a pull request can cause GitHub to create synthetic commit objects.

## 1. Prepare an isolated candidate

Use a fresh disposable clone containing only the reviewed source branch. Retain the
full candidate and sanitized-root commit IDs from review.

```powershell
$candidate = "C:\path\to\reviewed\jarvis-local"
$publishClone = Join-Path ([IO.Path]::GetTempPath()) ("jarvis-public-" + [guid]::NewGuid())
$approvedCommit = "FULL_40_CHARACTER_APPROVED_COMMIT"
$approvedRoot = "FULL_40_CHARACTER_SANITIZED_ROOT"
$sourceBranch = "codex/release-v1.2.3"
$versionTag = "v1.2.3"
$publicUrl = "https://github.com/OWNER/jarvis-local-public.git"

git clone --no-local --single-branch --no-tags --branch $sourceBranch $candidate $publishClone
Set-Location -LiteralPath $publishClone
git branch -M main
git remote remove origin
git remote add public $publicUrl
```

Removing the source remote keeps development refs out of the publishing clone. Do not
fetch pull-request refs, other branches, tags, or a development remote into it.

Run every local guard before the first public push:

```powershell
python -B scripts/check_public_publish_source.py `
  --repository . `
  --expected-commit $approvedCommit `
  --expected-root $approvedRoot `
  --mode candidate `
  --version-tag $versionTag `
  --remote-url $publicUrl

python -B scripts/check_public_release.py
gitleaks dir --redact --no-banner .
gitleaks git --redact --no-banner .
```

The publish-source guard rejects a dirty tree, an unexpected or multiple history
root, merge history, extra refs or remotes, mismatched destinations, alternate object
storage, unreachable objects, configured push refspecs, and broad ref selection. It
uses the immutable reviewed commit, disables hooks and implicit push behavior, and
requires an absent destination through a creation-only lease. After review, rerun the
same command with `--execute-push`; the guard revalidates and performs the push itself.
Its candidate arguments have this shape:

```powershell
--no-verify --no-follow-tags --no-push-option --recurse-submodules=no --signed=false --force-with-lease=refs/heads/release/v1.2.3: https://github.com/OWNER/jarvis-local-public.git FULL_40_CHARACTER_APPROVED_COMMIT:refs/heads/release/v1.2.3
```

## 2. Open and validate the pull request

Open a pull request from `release/v1.2.3` to protected `main`. Keep the candidate to
one reviewed, non-merge commit directly on the current `main` tip. If `main` moves,
rebuild or update the candidate and repeat the local scans; do not weaken the range
gate.

Before merging, record the exact candidate commit and tree:

```powershell
$candidateCommit = (git rev-parse HEAD).Trim()
$candidateTree = (git rev-parse 'HEAD^{tree}').Trim()
```

All six protected contexts must pass on that exact candidate. Confirm the Secret and
privacy scan covered both the exact one-commit privacy range and exact-range Gitleaks;
the three Windows Python jobs passed; the quality job passed coverage, syntax,
distribution, installed-wheel, and dependency-audit checks; and the aggregate CodeQL
gate passed with its three underlying analyses.

Review the complete pull-request diff and resolve every conversation. Merge only with
GitHub's **Squash and merge** action. Do not substitute a rebase, merge commit, direct
push, or local reconstruction of the squash commit.

## 3. Verify the GitHub-created squash commit

Record the pre-merge `main` tip before merging. After the squash completes, resolve the
new public `main` tip and use a new public-only clone for verification:

```powershell
$preMergeMain = "FULL_40_CHARACTER_PRE_MERGE_MAIN"
$mergedCommit = "FULL_40_CHARACTER_GITHUB_SQUASH_COMMIT"
$verifyClone = Join-Path ([IO.Path]::GetTempPath()) ("jarvis-main-verify-" + [guid]::NewGuid())

git clone --no-local --single-branch --no-tags --branch main $publicUrl $verifyClone
Set-Location -LiteralPath $verifyClone
if ((git rev-parse HEAD).Trim() -ne $mergedCommit) {
  throw "Public main moved or does not equal the recorded squash commit"
}
if ((git rev-parse 'HEAD^{tree}').Trim() -ne $candidateTree) {
  throw "The squash commit tree differs from the reviewed candidate tree"
}

python -B scripts/check_public_release.py `
  --history-ref $mergedCommit `
  --history-base $preMergeMain
gitleaks dir --redact --no-banner .
gitleaks git --redact --no-banner `
  --log-opts="--no-merges --first-parent $preMergeMain..$mergedCommit" .
```

The ranged privacy check verifies that the squash commit is the single non-merge child
of the recorded old `main`, its tree exactly matches the checkout, and its author,
committer, message, paths, and content satisfy the public boundary. A matching tree is
necessary but is not enough by itself; identity and history checks must also pass.

The push to `main` starts a fresh hosted run against the squash commit. Wait for all
six protected contexts to pass again on `$mergedCommit`, including the aggregate
CodeQL result. A green candidate run does not replace this post-merge run. If any
check is missing, pending, cancelled, or failing, stop and do not tag.

## 4. Tag only the green squash commit

Create the lightweight release tag only after the exact squash commit passes the local
post-merge checks and all six hosted contexts. Use another fresh clone of public
`main`; do not reuse the candidate or verification clone.

```powershell
$tagClone = Join-Path ([IO.Path]::GetTempPath()) ("jarvis-tag-" + [guid]::NewGuid())
git clone --no-local --single-branch --no-tags --branch main $publicUrl $tagClone
Set-Location -LiteralPath $tagClone
if ((git rev-parse HEAD).Trim() -ne $mergedCommit) {
  throw "Tag source does not equal the verified squash commit"
}
git remote remove origin
git remote add public $publicUrl
git tag $versionTag $mergedCommit

python -B scripts/check_public_publish_source.py `
  --repository . `
  --expected-commit $mergedCommit `
  --expected-root $approvedRoot `
  --mode tag `
  --version-tag $versionTag `
  --remote-url $publicUrl
python -B scripts/check_public_release.py --history-ref $mergedCommit
gitleaks dir --redact --no-banner .
gitleaks git --redact --no-banner .
```

Use `--execute-push` only after reviewing the validated tag arguments. Their shape is:

```powershell
--no-verify --no-follow-tags --no-push-option --recurse-submodules=no --signed=false --force-with-lease=refs/tags/v1.2.3: https://github.com/OWNER/jarvis-local-public.git FULL_40_CHARACTER_GITHUB_SQUASH_COMMIT:refs/tags/v1.2.3
```

After the push, confirm anonymously that the advertised tag and `main` both resolve to
`$mergedCommit`, verify published checksums and distributions, and create the GitHub
release from that exact tag. A moved branch, changed tag, or mismatched asset stops the
release.

## Future public-history incidents

The repository intentionally ships no generic history-rewrite or force-push mode. A
future incident requires fresh, one-off, independently reviewed tooling and explicit
operator authorization naming every ref mutation. Prior incident scripts, branches,
commit pins, and successful tests grant no authority for another rewrite.
