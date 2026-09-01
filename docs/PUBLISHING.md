# Public publishing

Publish JARVIS Local only from a disposable clone that contains the reviewed public
snapshot. Never publish directly from a development checkout. Use three separate,
fail-closed phases: first push one candidate branch for a protected pull request; after
that exact candidate commit passes every required check, promote only that commit to
public `main`; then create and push only the release tag from a fresh clone of public
`main`. Each disposable clone has one remote named `public`, pointing at the approved
public GitHub repository.

> **Privacy-history repair exception:** do not use the ordinary `v0.6.3` variables,
> candidate command, promotion, or tag flow below. For the reviewed pre-Phase-6
> repair, follow [Replace privacy-affected public history](#replace-privacy-affected-public-history)
> from its first preparation command through its final verification. That flow uses
> only `v0.6.4-phase6-baseline` and never creates a release tag.

## Prepare the isolated source

The example below assumes the reviewed source branch descends only from the approved
sanitized public root. Use a fresh temporary directory and keep both the full approved
candidate commit and sanitized-root hashes from the release review.

```powershell
$candidate = "C:\path\to\reviewed\jarvis-local"
$publishClone = Join-Path ([IO.Path]::GetTempPath()) ("jarvis-public-" + [guid]::NewGuid())
$approvedCommit = "FULL_40_CHARACTER_APPROVED_COMMIT"
$approvedRoot = "FULL_40_CHARACTER_SANITIZED_ROOT"
$sourceBranch = "public-v0.6.3-ready"
$versionTag = "v0.6.3"
$publicUrl = "https://github.com/OWNER/jarvis-local-public.git"

git clone --no-local --single-branch --no-tags --branch $sourceBranch $candidate $publishClone
Set-Location -LiteralPath $publishClone
git branch -M main
git remote remove origin
git remote add public $publicUrl
```

`--no-local`, single-branch cloning, and removing the source remote prevent the
publishing checkout from borrowing or retaining development-history object storage.
Do not fetch another branch, tag, pull-request ref, or development remote into this
clone.

## Run the fail-closed guard

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

The guard rejects a dirty tree, an unapproved or multiple history root, merge history,
any extra local or remote-tracking ref, any extra remote, a mismatched destination,
alternate object storage, unreachable objects, configured push refspecs, and broad
push modes. A successful check prints the one exact candidate ref that was reviewed.
The privacy and Gitleaks scans are mandatory pre-push gates; CI repeats them after the
candidate branch is published. Never rely on the post-push checks as the first scan.

For an explicitly approved privacy-history repair, CI does not treat the old public
tip as an ordinary parent. It fetches that exact GitHub-provided commit without
creating a ref, proves the reviewed old and rewritten segments have the same ordered
trees, messages, authors, timestamps, and protected headers, requires each new
committer to equal the already-approved author identity, scans identity and message
metadata throughout the sanitized reachable history, and scans every divergent
commit tree and blob after the pinned trusted common ancestor. The reviewed common
ancestor and rewritten old-tip counterpart are pinned in the workflow. A changed
tree, message, author, time, topology, pin, identity, or clone shape fails closed
without printing mailbox values. CI also runs Gitleaks over an explicit range ending
at the same full commit ID already accepted by the privacy gate; event-derived commit
lists cannot narrow that authoritative scan.

## Publish the candidate through branch protection

Use only the command printed by the guard. Its shape is:

```powershell
git push public HEAD:refs/heads/release/v0.6.3
```

Open a pull request from `release/v0.6.3` to protected `main` so review and required
checks run against the exact candidate commit. Do not merge the pull request through
GitHub: after every required check passes, promote that exact commit as described below,
then close the pull request. Do not publish the version tag from the candidate clone.

## Promote the exact checked commit

GitHub-hosted squash and rebase merges can create a new commit or rewrite committer
identity. For a privacy-sensitive release, promote the exact candidate commit whose
author and committer were already verified as approved no-reply identities. Recreate
the candidate as a fresh disposable public-only clone, then run the guard with
`--mode promotion`. The guard resolves `public` `main` without creating a tracking ref
and requires that tip to exist in the checked history and be an ancestor of `HEAD`.
This makes the printed update fast-forward-only; the command contains no force option.

The promotion guard prints only this exact command:

```powershell
git push public HEAD:refs/heads/main
```

Run it only after all required checks have passed for that exact `HEAD`, and only under
the repository's existing protection policy. If GitHub rejects the update, stop: do
not weaken or bypass branch protection during a release, and do not substitute a broad
push. Re-check the repository rules through a separately reviewed governance change.

## Replace privacy-affected public history

This exceptional mode is only for an operator-approved repair of a reproduced public-
history privacy finding. It is not a release merge shortcut. The complete candidate
preparation for the reviewed pre-Phase-6 repair is:

```powershell
$candidate = "C:\path\to\reviewed\jarvis-local"
$candidateClone = Join-Path ([IO.Path]::GetTempPath()) ("jarvis-repair-candidate-" + [guid]::NewGuid())
$approvedCommit = "FULL_40_CHARACTER_APPROVED_COMMIT"
$approvedRoot = "FULL_40_CHARACTER_SANITIZED_ROOT"
$approvedOldMain = "FULL_40_CHARACTER_REVIEWED_OLD_MAIN"
$sourceBranch = "codex/phase6-secure-baseline-candidate"
$versionTag = "v0.6.4-phase6-baseline"
$publicUrl = "https://github.com/OWNER/jarvis-local-public.git"
$publicRepository = "OWNER/jarvis-local-public"
$approvedProtectionSnapshot = "C:\outside-the-repository\main-protection-before.json"

git clone --no-local --single-branch --no-tags --branch $sourceBranch $candidate $candidateClone
Set-Location -LiteralPath $candidateClone
git branch -M main
git remote remove origin
git remote add public $publicUrl

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
git push public HEAD:refs/heads/release/v0.6.4-phase6-baseline
```

Use only the candidate command emitted by the guard. Open or update the pull request
from `release/v0.6.4-phase6-baseline` to protected `main`, obtain every required hosted
test, privacy, build, audit, and CodeQL result on that exact commit, and keep the pull
request open. Do not merge it through GitHub. Re-confirm that public `main` still equals
the reviewed old tip.

After the hosted checks pass, create a second disposable clone directly from the
public candidate ref. Do not reuse the candidate-push clone. Run the local privacy and
secret scans again before the source guard's dedicated mode. For the reviewed
pre-Phase-6 repair, `$versionTag` remains exactly `v0.6.4-phase6-baseline`, matching
the already checked candidate ref:

```powershell
$replacementClone = Join-Path ([IO.Path]::GetTempPath()) ("jarvis-repair-publish-" + [guid]::NewGuid())
git clone --no-local --single-branch --no-tags `
  --branch "release/$versionTag" $publicUrl $replacementClone
Set-Location -LiteralPath $replacementClone
git branch -M main
git remote remove origin
git remote add public $publicUrl

if ((git rev-parse HEAD).Trim() -ne $approvedCommit) {
  throw "Public candidate no longer equals the approved commit"
}
python -B scripts/check_public_release.py --history-ref $approvedCommit
gitleaks dir --redact --no-banner .
gitleaks git --redact --no-banner .
python -B scripts/check_public_publish_source.py `
  --repository . `
  --expected-commit $approvedCommit `
  --expected-root $approvedRoot `
  --mode history-replacement `
  --version-tag $versionTag `
  --remote-url $publicUrl `
  --expected-remote-main $approvedOldMain
```

The guard requires the live candidate ref to equal `$approvedCommit`, the live public
tip to equal `$approvedOldMain`, and every ordinary disposable-clone invariant above.
It emits only this single-ref, explicit-lease shape:

```powershell
git push --force-with-lease=refs/heads/main:FULL_OLD_COMMIT public FULL_NEW_COMMIT:refs/heads/main
```

Run that exact command only when the final operator approval names both full hashes
and the repository protection policy already permits the narrowly reviewed repair.
Changing protection is a separate governance mutation: it must be explicitly approved,
time-bounded, recorded before execution, and restored and verified immediately after
the lease update. Before any approved policy mutation, save the exact live protection
response outside the repository without printing it:

```powershell
$protectionBefore = gh api "repos/$publicRepository/branches/main/protection"
if ($LASTEXITCODE -ne 0 -or -not $protectionBefore) {
  throw "Could not record the live main protection policy"
}
[IO.File]::WriteAllText(
  $approvedProtectionSnapshot,
  $protectionBefore,
  [Text.UTF8Encoding]::new($false)
)
```

Run only the separately approved, time-bounded policy mutation and the exact command
emitted by the history-replacement guard. Whether the lease push succeeds or fails,
run the separately approved restoration request immediately. Then compare the restored
policy with the saved pre-change response and explicitly confirm that administrator
enforcement is enabled and force pushes are disabled:

```powershell
$expectedProtection = (
  Get-Content -Raw -LiteralPath $approvedProtectionSnapshot |
    ConvertFrom-Json | ConvertTo-Json -Depth 100 -Compress
)
$protectionAfterRaw = gh api "repos/$publicRepository/branches/main/protection"
if ($LASTEXITCODE -ne 0 -or -not $protectionAfterRaw) {
  throw "Could not verify restored main protection"
}
$protectionAfter = $protectionAfterRaw | ConvertFrom-Json
$normalizedProtectionAfter = $protectionAfter | ConvertTo-Json -Depth 100 -Compress
if ($normalizedProtectionAfter -cne $expectedProtection) {
  throw "Main protection was not restored exactly"
}
if (-not $protectionAfter.enforce_admins.enabled -or
    $protectionAfter.allow_force_pushes.enabled) {
  throw "Main protection safety properties are not restored"
}
```

Finally, verify the result without credentials from a third fresh clone. The ref audit
below accepts only advertised branch/tag targets that are commits in the sanitized
history at `$approvedCommit`; it handles annotated tags through their peeled targets
and does not print ref names or object values:

```powershell
$verifyClone = Join-Path ([IO.Path]::GetTempPath()) ("jarvis-repair-verify-" + [guid]::NewGuid())
$env:GIT_TERMINAL_PROMPT = "0"
git -c credential.helper= -c http.extraHeader= clone --no-local --single-branch --no-tags `
  --branch main $publicUrl $verifyClone
if ($LASTEXITCODE -ne 0) { throw "Anonymous main clone failed" }
Set-Location -LiteralPath $verifyClone
if ((git rev-parse HEAD).Trim() -ne $approvedCommit) {
  throw "Published main does not equal the approved commit"
}

python -B scripts/check_public_release.py --history-ref $approvedCommit
gitleaks dir --redact --no-banner .
gitleaks git --redact --no-banner .

$advertisedLines = @(
  git -c credential.helper= -c http.extraHeader= ls-remote $publicUrl `
    'refs/heads/*' 'refs/tags/*'
)
if ($LASTEXITCODE -ne 0 -or $advertisedLines.Count -eq 0) {
  throw "Could not audit advertised public refs"
}
$advertised = @{}
foreach ($line in $advertisedLines) {
  $fields = @($line -split '\s+')
  if ($fields.Count -ne 2 -or
      $fields[0] -notmatch '^[0-9a-fA-F]{40}$' -or
      $fields[1] -notmatch '^refs/(heads|tags)/' -or
      $advertised.ContainsKey($fields[1])) {
    throw "Public remote returned malformed or duplicate ref data"
  }
  $advertised[$fields[1]] = $fields[0].ToLowerInvariant()
}
if ($advertised['refs/heads/main'] -ne $approvedCommit) {
  throw "Advertised main does not equal the approved commit"
}
foreach ($refName in $advertised.Keys) {
  if ($refName.EndsWith('^{}')) { continue }
  $peeledName = "$refName^{}"
  $target = if ($refName.StartsWith('refs/tags/') -and
                $advertised.ContainsKey($peeledName)) {
    $advertised[$peeledName]
  } else {
    $advertised[$refName]
  }
  git cat-file -e "${target}^{commit}" 2>$null
  if ($LASTEXITCODE -ne 0) { throw "Advertised ref is outside sanitized history" }
  git merge-base --is-ancestor $target $approvedCommit
  if ($LASTEXITCODE -ne 0) { throw "Advertised ref is outside sanitized history" }
}
```

A stale lease, changed candidate ref, rejected update, failed policy restoration,
unexpected remote tip, failed anonymous scan, or divergent advertised ref stops the
process. Do not publish a tag as part of a history repair. Required push-triggered CI
and CodeQL checks must also pass on the exact published main commit before Phase 6
begins. Hidden hosting retention, pull-request refs, forks, caches, and previously
downloaded clones remain outside the advertised-ref proof.

## Tag the verified promotion from a new clone

Record the exact promoted `main` commit as `$approvedCommit`, then create another fresh,
single-branch, tag-free clone directly from the public repository. Remove its source
remote, add the credential-free destination as `public`, create a lightweight tag at
`$approvedCommit`, and run the same guard with `--mode tag`. The tag clone must contain
only local `main` and the intended tag. The reviewed command has this shape:

```powershell
git push public refs/tags/v0.6.3:refs/tags/v0.6.3
```

Do not add implicit or broad ref-selection options. After tagging, compare the public
branch and tag commit IDs with `$approvedCommit`, run release checks against that exact
commit, and inspect the repository without authentication. Keep each temporary clone
only until its phase passes, then discard it.
