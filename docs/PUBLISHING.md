# Public publishing

Publish JARVIS Local only from a disposable clone that contains the reviewed public
snapshot. Never publish directly from a development checkout. Use two separate,
fail-closed phases: first push one candidate branch for a protected pull request; after
that pull request passes and merges, create and push only the release tag from a fresh
clone of public `main`. Each disposable clone has one remote named `public`, pointing
at the approved public GitHub repository.

## Prepare the isolated source

The example below assumes the reviewed source branch descends only from the approved
sanitized public root. Use a fresh temporary directory and keep both the full approved
candidate commit and sanitized-root hashes from the release review.

```powershell
$candidate = "C:\path\to\reviewed\jarvis-local"
$publishClone = Join-Path ([IO.Path]::GetTempPath()) ("jarvis-public-" + [guid]::NewGuid())
$approvedCommit = "FULL_40_CHARACTER_APPROVED_COMMIT"
$approvedRoot = "FULL_40_CHARACTER_SANITIZED_ROOT"
$sourceBranch = "public-v0.6.1-ready"
$versionTag = "v0.6.1"
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
```

The guard rejects a dirty tree, an unapproved or multiple history root, merge history,
any extra local or remote-tracking ref, any extra remote, a mismatched destination,
alternate object storage, unreachable objects, configured push refspecs, and broad
push modes. A successful check prints the one exact candidate ref that was reviewed.

## Publish the candidate through branch protection

Use only the command printed by the guard. Its shape is:

```powershell
git push public HEAD:refs/heads/release/v0.6.1
```

Open a pull request from `release/v0.6.1` to protected `main`. Merge only after every
required check passes. Do not bypass protection or publish the version tag from the
candidate clone.

## Tag the verified merge from a new clone

Record the exact merged `main` commit as `$approvedCommit`, then create another fresh,
single-branch, tag-free clone directly from the public repository. Remove its source
remote, add the credential-free destination as `public`, create a lightweight tag at
`$approvedCommit`, and run the same guard with `--mode tag`. The tag clone must contain
only local `main` and the intended tag. The reviewed command has this shape:

```powershell
git push public refs/tags/v0.6.1:refs/tags/v0.6.1
```

Do not add implicit or broad ref-selection options. After tagging, compare the public
branch and tag commit IDs with `$approvedCommit`, run release checks against that exact
commit, and inspect the repository without authentication. Keep each temporary clone
only until its phase passes, then discard it.
