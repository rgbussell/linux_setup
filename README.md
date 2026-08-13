# Linux ML Workstation Setup

## Step 1 — System packages

### 1a. Base packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y build-essential git git-lfs curl wget unzip ca-certificates \
                    pkg-config libgl1
git lfs install
```

`git-lfs` matters if any repo stores large binaries through LFS: a clone without it silently gives you pointer files instead of data. `libgl1` is the usual missing shared library behind `ImportError: libGL.so.1` from OpenCV or ITK inside otherwise-fine installs.

Note there is no `python3-pip` or `python3-venv` in that list. `uv` manages interpreters and environments itself (Step 4), so the system Python stays untouched — which is the point.

- [ ] `git --version` and `git lfs version` both report

### 1b. Operational tooling

None of these is required to train a model. Each answers a question you will ask within the first week of trying:

```bash
sudo apt install -y tmux htop ncdu iotop tree jq ripgrep smartmontools nvme-cli
```

| Tool | The question it answers |
|---|---|
| `tmux` | "My SSH dropped — did that kill the training run?" (see 1c) |
| `htop` | What is using CPU and RAM right now |
| `ncdu` | Which directory filled the disk — far faster than `du` on an imaging tree |
| `iotop` | Whether the GPU is idle because the data loader is I/O-starved |
| `nvme-cli` | `nvme list` — drive identity, namespaces, wear (Step 9) |
| `smartmontools` | `smartctl -i` — is a candidate drive healthy before you trust data to it |
| `jq` | Reading DVC and MLflow JSON without opening a Python session |

For the GPU, `nvidia-smi` ships with the driver but prints snapshots. `nvtop` gives an htop-style live view of utilization and per-process VRAM, which is what distinguishes a compute-bound epoch from a loader-starved one:

```bash
sudo apt install -y nvtop
```

### 1c. tmux — do this before anything long-running

**Set this up before the first training run, not after losing one.** An SSH session is the parent process of everything launched inside it. When the connection drops — laptop sleeps, VPN reconnects, Wi-Fi hiccups — the shell gets `SIGHUP` and takes your job down with it. A multi-hour preprocessing run dies at hour three and leaves a half-written dataset that looks complete. This is the most common way remote ML work is lost and it is entirely preventable.

tmux keeps the session alive **on the server**, independent of your connection:

```bash
tmux new -s train          # start a named session
# ... launch the long job ...
# detach: Ctrl-b then d     — the job keeps running

tmux ls                    # list sessions
tmux attach -t train       # reattach from anywhere, after any disconnect
```

Detaching is not backgrounding. The process keeps its terminal, so progress bars, prompts, and training output are all still there when you return.

A minimal config, because two defaults will bite you:

```bash
cat > ~/.tmux.conf <<'EOF'
set -g mouse on                    # scroll, and click between panes
set -g history-limit 100000        # default 2000 silently eats your training log
setw -g mode-keys vi
bind | split-window -h
bind - split-window -v
EOF
tmux kill-server                   # config is read when the server starts
```

`history-limit` is the one that matters. The default 2000 lines discards the *beginning* of a long training log, which is exactly where the config dump and first-epoch losses live.

Enough keys to be useful: `Ctrl-b |` and `Ctrl-b -` split panes with the config above, `Ctrl-b` plus an arrow key moves between them, `Ctrl-b c` opens a window and `Ctrl-b n`/`p` cycles. A workable layout is one pane training, one running `nvtop`, one free.

**tmux is not a substitute for a service.** It survives a dropped connection, not a reboot — for that use a systemd user unit (the MLflow example in 11c). One further trap: a long-lived tmux session does not pick up environment variables added to `~/.bashrc` after it started, which is a recurring source of `KeyError: 'nnUNet_raw'` in a session that has been attached for a week.

- [ ] `tmux new -s test`, detach with `Ctrl-b d`, then `tmux attach -t test` returns you to it

### 1d. cmux — parallel coding agents

Disambiguate before installing: at least four unrelated projects use this name, and only some run on Linux.

| Project | What it is | Runs on |
|---|---|---|
| [manaflow-ai/cmux](https://github.com/manaflow-ai/cmux) | Native Swift/AppKit terminal — vertical tabs, split panes, embedded browser, built for watching several coding agents at once | **macOS only** |
| [craigsc/cmux](https://github.com/craigsc/cmux) | Pure-bash wrapper giving each agent its own git worktree | macOS and Linux |
| [soheilhy/cmux](https://github.com/soheilhy/cmux) | Go library for multiplexing protocols on one port — nothing to do with terminals | n/a (library) |

If you drive this Linux box from a Mac, the macOS app belongs **on the Mac**. It is a terminal emulator: it replaces the thing you type into, so it has no server-side component and nothing about it belongs in this runbook except the warning not to look for it here.

```bash
# on the Mac — NOT on the Linux box
brew tap manaflow-ai/cmux
brew install --cask cmux
```

What does install on the Linux box is the worktree manager, which needs only bash, git, and the Claude CLI:

```bash
curl -fsSL https://github.com/craigsc/cmux/releases/latest/download/install.sh | sh
echo '.worktrees/' >> .gitignore      # in each repo you use it in
```

Despite the "tmux for Claude Code" tagline it neither uses nor requires tmux — the isolation is a git worktree per agent, not a pane per agent. The two compose rather than compete: tmux keeps sessions alive across a dropped SSH connection, cmux keeps parallel agents from editing the same working tree. Read that install script before piping it to a shell, as with any `curl | sh`.

---

## Step 2 — Git identity and defaults

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
git config --global init.defaultBranch main
git config --global pull.rebase false
git config --global core.editor "vim"
```

Use the email attached to your git-host account, otherwise commits show as unlinked to your profile.

## Step 3 — Git hosting over SSH

### 3a. Generate a dedicated key

```bash
ssh-keygen -t ed25519 -C "you@example.com" -f ~/.ssh/id_ed25519_bitbucket
```

```bash
cat >> ~/.ssh/config <<'EOF'
Host bitbucket.org
    User git
    IdentityFile ~/.ssh/id_ed25519_bitbucket
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
```

### 3c. Register the public key

```bash
cat ~/.ssh/id_ed25519_bitbucket.pub
```

```bash
ssh -vT git@bitbucket.org 2>&1 | grep -E 'Connection established|Offering public key|Permission denied|authenticated'
```

No `Connection established` means you never reached the server; no `Offering public key` means SSH never sent a key (go back to 3b); `Offering public key` followed by `Permission denied` means the server rejected it (go to 3c).

To convert an existing HTTPS clone: `git remote set-url origin git@bitbucket.org:<workspace>/<repo>.git`

### 3e. HTTPS alternative (CI, or where SSH egress is blocked)

Bitbucket Cloud app passwords have been superseded by **Atlassian API tokens**. Create one under Atlassian account settings → Security → API tokens, scoped `read:repository:bitbucket` (add `write:repository:bitbucket` to push). Let git prompt for it as the password:

```bash
git clone https://x-bitbucket-api-token-auth@bitbucket.org/<workspace>/<repo>.git
```

Never embed a token in a remote URL on a workstation — it lands in `.git/config` in plaintext and leaks into any shell history or screen share. Store it in a credential helper instead; prefer `libsecret` over the plaintext `store` helper:

```bash
sudo apt install -y libsecret-1-0 libsecret-1-dev
sudo make -C /usr/share/doc/git/contrib/credential/libsecret
git config --global credential.helper \
  /usr/share/doc/git/contrib/credential/libsecret/git-credential-libsecret
```

### 3g. GitHub alongside Bitbucket

Both hosts coexist in one config — SSH picks the block by hostname, and `IdentitiesOnly` keeps each from offering the other's key:

```bash
ssh-keygen -t ed25519 -C "you@example.com" -f ~/.ssh/id_ed25519_github

cat >> ~/.ssh/config <<'EOF'

Host github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_github
    IdentitiesOnly yes
EOF
```

## Step 4 — uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc          # installer adds ~/.local/bin to PATH
uv --version
```

### Let uv own the Python version

nnU-Net v2 requires **Python ≥3.10**, the strictest constraint in this stack. Rather than fighting the distro Python or adding a PPA, have uv install one:

```bash
uv python install 3.12
uv python list
```

### Two install modes, and when to use each

**`uv tool install`** puts a *command-line tool* in its own isolated environment with its executables on PATH. Use it for things you invoke as commands and never import — DVC is the case here.

**`uv pip install`** installs into the *project virtualenv* you have activated. Use it for anything you import in code or that must share the same torch build — nnU-Net, TotalSegmentator, MLflow's client.

---

## Step 5 — Project environment

One environment per project, never a global install. nnU-Net writes environment variables and TotalSegmentator caches weights; keeping them separate saves you from one dependency conflict taking out the box.

```bash
uv venv --python 3.12 ~/venvs/spineseg
source ~/venvs/spineseg/bin/activate
```

Add the `source` line to `~/.bashrc` if this box has one purpose. Inside a repo, `uv venv` with no arguments creates `.venv/` in the project root, which is the more common pattern for code repos.

uv deliberately refuses to install into a system interpreter without `--system`. If you see that error, no venv is active — activate one rather than reaching for the flag.

---

## Step 6 — AWS CLI and credentials

### 6a. Install CLI v2

Do **not** install this with uv or pip. The `awscli` on PyPI is v1 and drifts from v2 behavior; use the official bundle:

```bash
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install
aws --version
```

On ARM use `awscli-exe-linux-aarch64.zip`. To upgrade later, rerun with `sudo /tmp/aws/install --update`.

### 6b. Configure credentials

**Preferred — SSO / Identity Center**, because it issues short-lived credentials that expire on their own:

```bash
aws configure sso
# SSO start URL, SSO region, then pick account + role, name the profile
aws sso login --profile <profile>
export AWS_PROFILE=<profile>          # add to ~/.bashrc
```

SSO sessions expire (typically 8–12h). When DVC starts throwing credential errors mid-morning, `aws sso login` again — that is the usual cause, not a DVC misconfiguration.

**Fallback — long-lived access keys**, only if SSO isn't offered:

```bash
aws configure --profile <profile>
```

Keys land in `~/.aws/credentials` in plaintext. Confirm `chmod 600 ~/.aws/credentials`, never commit that file, and rotate on a schedule.

```bash
aws sts get-caller-identity --profile <profile>
aws s3 ls --profile <profile>
```

### 6c. Guardrails for regulated data

If this machine will hold clinical or otherwise regulated data, confirm before pulling anything real: that the bucket is covered by the appropriate agreement (a BAA, for HIPAA-regulated data), the de-identification status of what you are pulling, and that the local disk is encrypted.

De-identification is not a property you can assume from a filename. Identifiers that embed a date and a person's initials remain quasi-identifiers even when an obvious name field has been stripped, and re-identification risk survives naive anonymization. Treat "is this actually de-identified?" as a question with an owner and a written answer.

- [ ] Bucket covered by the required agreement
- [ ] Local disk encryption confirmed
- [ ] Clear on what data this machine may hold

---

## Step 7 — DVC on S3

DVC is how data reaches this machine: pointer files commit to git, bytes live in an S3 bucket, and `dvc pull` reconciles the two. A fresh clone without a pull gives you a repo full of stubs.

### 7a. Install

DVC is a CLI, so install it as a tool rather than into the project venv. Backends are extras and must be requested at install time:

```bash
uv tool install "dvc[s3]"
uv tool update-shell        # ensures ~/.local/bin is on PATH; then restart the shell
dvc --version
```

To add a backend later, rerun `uv tool install` with the fuller extras list — it replaces the tool environment rather than adding to it.

If a project's `dvc.yaml` stages import DVC's Python API rather than shelling out, that project also needs `uv pip install "dvc[s3]"` inside its venv. Shelling out to `dvc` from a stage does not.

### 7b. Create and harden the bucket

Skip if the bucket exists — but verify the four settings below, because the defaults are not what you want for versioned data.

```bash
export BUCKET=<org>-ml-dvc
export REGION=us-west-2

aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION"
```

In `us-east-1` only, omit `--create-bucket-configuration` entirely — that region rejects it.

```bash
# 1. Block all public access
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# 2. Default encryption (SSE-KMS; use AES256 if you have no CMK)
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms","KMSMasterKeyID":"<key-arn>"},"BucketKeyEnabled":true}]}'

# 3. Versioning — recovers an object deleted by a bad `dvc gc`
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

# 4. Refuse unencrypted transport
aws s3api put-bucket-policy --bucket "$BUCKET" --policy '{
  "Version":"2012-10-17",
  "Statement":[{
    "Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*",
    "Resource":["arn:aws:s3:::'"$BUCKET"'","arn:aws:s3:::'"$BUCKET"'/*"],
    "Condition":{"Bool":{"aws:SecureTransport":"false"}}
  }]
}'
```

`BucketKeyEnabled` is not cosmetic. DVC stores content-addressed objects, so a large pull is tens of thousands of small GETs; without a bucket key each one is a separate KMS call you pay for and get throttled on.

A lifecycle rule expiring noncurrent versions after 90 days is cheap insurance against unbounded growth.

### 7c. IAM: least privilege

DVC needs exactly four actions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DvcListBucket",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<bucket>"
    },
    {
      "Sid": "DvcObjectAccess",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::<bucket>/*"
    }
  ]
}
```

`ListBucket` is on the bucket ARN; the object actions are on `/*`. Getting that split wrong produces an `Access Denied` on `dvc push` that looks like a credential problem but isn't.

If the bucket uses SSE-KMS, the principal also needs `kms:Encrypt`, `kms:Decrypt`, and `kms:GenerateDataKey` on the key — otherwise pushes fail at upload with a KMS error buried under a generic DVC traceback.

For read-only consumers (analysts, CI that only pulls), drop `PutObject` and `DeleteObject`. Withholding `DeleteObject` from everyone except a maintainer is also what makes an accidental `dvc gc --cloud` a no-op instead of an incident.

### 7d. Point DVC at S3

```bash
cd <repo>
dvc remote add -d storage s3://<bucket>/<prefix>
dvc remote modify storage profile <profile>
dvc remote modify storage region <region>
```

`-d` makes it the default so bare `dvc push`/`dvc pull` use it. The `<prefix>` lets several repos share one bucket — use the repo name.

DVC reads credentials through boto3, so the AWS profile from Step 6 is picked up with no secrets in DVC config at all. That is the configuration you want: `.dvc/config` stays committable.

If the bucket enforces SSE-KMS, declare it so DVC sends the right headers on write:

```bash
dvc remote modify storage sse aws:kms
dvc remote modify storage sse_kms_key_id <key-arn>
dvc remote modify storage jobs 16          # more parallelism for large objects
```

**On secrets**: if you must use static keys instead of a profile, they go in `.dvc/config.local` via `--local`, never in the committed config:

```bash
dvc remote modify --local storage access_key_id '<id>'
dvc remote modify --local storage secret_access_key '<secret>'
```

Keys that always require `--local`: `access_key_id`, `secret_access_key`, `session_token`, `credentialpath`, `configpath`. A profile *name* is not a secret and can stay committed.

```bash
git add .dvc/config && git commit -m "dvc: point remote at s3"
```

### 7e. Pull data

```bash
dvc remote list          # confirm the URL and that it is default (shows *)
dvc status -c            # compare local cache against the remote, no transfer
dvc pull                 # fetch everything for the current commit
```

Narrower forms, since a full pull on an imaging repo can be hundreds of GB:

```bash
dvc pull data/cohort.dvc        # one tracked target
dvc pull -r storage             # a non-default remote by name
dvc pull --all-commits          # every version in history, not just HEAD
dvc pull -j 16                  # more parallel transfers
```

Verify you got bytes rather than pointers:

```bash
du -sh data/            # real size, not a few KB of stubs
dvc status              # "Data and pipelines are up to date."
```

`aws s3 ls s3://<bucket>/<prefix>/` shows content-addressed hash directories, not filenames. That is expected: DVC stores by hash and human-readable names live in the `.dvc` files. Do not browse the bucket looking for a specific file.

### 7f. Migrating from another remote

DVC has no server-side remote-to-remote transfer, so migration is a copy through a machine that holds the data. Run from a box that can still reach the old remote:

```bash
cd <repo>
dvc pull --all-commits                   # ensure the local cache holds everything
dvc remote add s3store s3://<bucket>/<repo-name>
dvc remote modify s3store profile <profile>
dvc remote modify s3store region <region>
dvc push -r s3store --all-commits
dvc status -c -r s3store                 # expect "up to date" BEFORE proceeding
dvc remote default s3store
dvc remote remove <old-remote>
git add .dvc/config && git commit -m "dvc: migrate remote to s3"
```

Two cautions. `--all-commits` requires that every historical version still resolves on the old remote; if some are gone, the pull reports the missing hashes and you migrate what remains — decide consciously whether that history matters. And leave the old remote's storage intact until a colleague has cloned fresh and pulled from S3 successfully. Removing the old remote is the irreversible step and it costs nothing to defer.

**A note on Google Drive as a DVC remote**: it works via `dvc[gdrive]`, but `dvc-gdrive` last shipped January 2024 while `dvc-s3` shipped January 2026, so it is comparatively unmaintained. It also authenticates through a shared default OAuth client that gets rate-limited, needs a browser for consent (awkward on headless boxes, though `GDRIVE_CREDENTIALS_DATA` or a service account works around it), and is not an appropriate home for regulated data. Prefer S3 where you have the choice.

### 7g. Daily workflow

```bash
dvc pull                      # fetch bytes for the pointers in this commit
dvc add data/cohort.nii.gz    # track a file/dir → writes a .dvc pointer
git add data/cohort.nii.gz.dvc .gitignore && git commit -m "track cohort"
dvc push                      # upload bytes to the remote
dvc status                    # local vs remote drift
dvc repro                     # re-run dvc.yaml stages whose deps changed
```

The failure that costs the most time is committing a pointer without pushing the bytes: collaborators get `No file hash info found` on pull. Make `dvc push` reflexively follow `git push`. Setting `autostage = true` in `.dvc/config` stages the `.dvc` file for you and removes one step.

### 7h. Bringing a new dataset under DVC

7a–7g assume the repo already tracks data. This is the other direction: you have a directory of images and want it versioned.

**Put the cache on the right disk first.** DVC's cache defaults to `.dvc/cache` inside the repo, which lands every byte on whatever disk holds `$HOME` — typically the OS drive, and typically the one with the least room. Set it before the first `dvc add`, because moving it afterwards means re-hashing everything:

```bash
cd <repo>
dvc init                                     # requires an existing git repo
dvc cache dir /data/dvc-cache                # the large disk chosen in Step 9
dvc config cache.type reflink,hardlink,symlink
git add .dvc/config && git commit -m "dvc: cache on the data disk"
```

`cache.type` is what stops each dataset from occupying twice the space. By default DVC **copies** files out of the cache into the working tree, so a 200GB cohort costs 400GB. Reflink is best — instant and copy-on-write, but needs XFS or Btrfs; hardlink is the usual ext4 fallback; DVC walks the list until one works. This is where the Step 9 filesystem test pays off: on exFAT or NTFS none of them work and you are silently back to full copies.

The tradeoff is real. Hardlinked and symlinked working files share storage with the cache, so editing one in place corrupts the cached copy — DVC guards against this by making them read-only. When you genuinely need to modify a tracked file, run `dvc unprotect <path>` first to turn it back into a private copy.

**Then add the data.** The granularity choice matters more than it appears:

```bash
dvc add data/raw/                 # one pointer for the whole directory
dvc add data/raw/case001.nii.gz   # one pointer per file
```

A directory gives one `.dvc` file and an all-or-nothing `dvc pull`. Per-file pointers let a collaborator fetch one case without the other 900, at the cost of 900 `.dvc` files in git. For imaging cohorts the directory form is nearly always right, with the split made at the *cohort* level — `data/train/`, `data/holdout/` — so a V&V holdout can be withheld from a machine that should not see it. That split is easy now and painful to retrofit.

```bash
git add data/raw.dvc data/.gitignore
git commit -m "track raw cohort"
dvc push
```

DVC writes the `.gitignore` entry itself so git never sees the bytes. Commit both files, or a collaborator gets a pointer with nothing behind it.

**Exclude what should never be hashed** via `.dvcignore`, which takes `.gitignore` syntax and saves DVC from walking scratch output on every status check:

```
*.tmp
**/__pycache__/
**/.ipynb_checkpoints/
```

**Make derived data a stage, not a tracked artifact.** Anything you can regenerate should be reproducible rather than merely stored — this is what makes a retraining defensible later:

```bash
dvc stage add -n preprocess \
  -d scripts/preprocess.py -d data/raw \
  -o data/preprocessed \
  python scripts/preprocess.py data/raw data/preprocessed

git add dvc.yaml dvc.lock && git commit -m "add preprocess stage"
dvc repro
```

`dvc.lock` records the exact input hashes that produced the output, so `dvc repro` re-runs a stage only when a dependency really changed, and the committed lock file is the evidence of which raw data produced which derived set.

**Before any of this touches real patient data**, the Step 6c questions apply — bucket agreement, de-identification status, disk encryption. `dvc push` is an upload: it moves bytes off this machine to whichever remote is default. Confirm `dvc remote list` names the remote you think it does before the first push of anything sensitive.

```bash
dvc status              # workspace vs cache
dvc status -c           # cache vs remote — exactly what a push would send
du -sh data/raw         # bytes, not pointers
```

---

## Data transfer — getting source data onto the box

7h assumes the bytes already sit on a local disk. Usually they don't: the cohort arrives on a cloud drive (Box, Drive, SharePoint) that only a laptop is signed into, and the workstation is headless or on a different network. Two routes work, and the choice is not cosmetic — one of them moves every byte twice.

**Check the size and the destination free space first.** A pull that fills the OS disk takes the desktop session down with it, and `df` on `$HOME` is not the number that matters if the data disk from Step 9 is the target:

```bash
df -h /data                     # the destination disk, not $HOME
rclone size box:path/to/folder  # or du -sh on a mounted copy
```

### Route A — rclone straight from the cloud to the box (preferred)

Box is a native rclone backend, so the workstation pulls directly and the laptop never touches the data. This is the right choice for anything large: parallel transfers instead of a serial stream, one hop instead of two, and nothing staged on a laptop disk.

If the box has a desktop session, let rclone open its own browser:

```bash
rclone config
# n) new remote  →  name: box  →  storage: box
# box_config_file: (blank), box_sub_type: user
# "Use web browser to automatically authenticate?" → y
```

If it's headless, run the browser half on any machine that has one — including the Mac — and paste the result back:

```bash
rclone authorize "box"          # on the laptop; prints a JSON token
rclone config                   # on the box; answer n to the browser question, paste the token
```

Either way the credential lands in `~/.config/rclone/rclone.conf`. Then:

```bash
rclone lsd box:                                    # sanity check before committing to a pull
rclone copy box:"path/to/folder" /data/dest \
  --transfers 8 --checkers 16 --progress --log-file=/tmp/box-pull.log
```

Use `copy`, never `sync` — `sync` makes the destination match the source, which means deleting local files that aren't on the remote. Re-running `copy` skips what already arrived, so an interrupted pull resumes by re-running the same command. Above roughly 8 transfers Box starts rate-limiting and throughput gets worse, not better.

Two things to know before relying on this. Ubuntu 24.04 packages rclone **v1.60.1** (Nov 2022) and the Box backend has had fixes since; if auth or listing misbehaves, install current rclone alongside it rather than debugging the old one:

```bash
curl -O https://downloads.rclone.org/rclone-current-linux-amd64.zip
unzip rclone-current-linux-amd64.zip
sudo install -m 755 rclone-v*/rclone /usr/local/bin/rclone
hash -r && rclone version       # /usr/local/bin precedes /usr/bin on PATH
```

And an enterprise Box tenant can block third-party OAuth clients, in which case `rclone lsd box:` fails at consent with an admin-approval error. That is not fixable from this side — it needs either an allowlist entry for rclone's client ID or the official Box CLI (`npm i -g @box/cli`) backed by a JWT app the Box admin authorizes. Fall back to Route B if neither is quick.

### Route B — rsync from a laptop's cloud mount

rsync speaks SSH, and Box exposes no SSH or rsync endpoint, so there is nothing to point it at directly. What makes rsync work is the desktop sync client: Box Drive presents the account as an ordinary directory, and rsync pushes from there over SSH like any other local path.

```bash
ls ~/Library/CloudStorage/                     # confirm the mount name first
BOX=~/Library/CloudStorage/Box-Box             # older Box Drive used ~/Box instead

tmux new -s boxpull                            # survives a closed lid
rsync -avh --partial --progress \
  "$BOX/Some Folder/" user@<box-ip>:/data/dest/
```

A trailing slash on the source copies the *contents*; no trailing slash copies the folder itself. `--partial` keeps a half-transferred file so an interrupted run resumes instead of restarting it.

The cost is hydration. Box Drive files are on-demand placeholders, so rsync pulls each one down to the laptop's disk as it reads it: the transfer runs at Box's download speed rather than LAN speed, and needs free space on the laptop. Marking the folder "Available Offline" in Finder first and letting it settle converts the rsync into a genuine LAN copy.

**Apple's rsync is a trap.** macOS 14 and earlier ship rsync **2.6.9** — from 2006, missing `--info=progress2` and much else; macOS 15 replaced it with **openrsync**, a BSD reimplementation with different gaps and different error text. Check before scripting anything against it, and prefer a real one:

```bash
rsync --version | head -1
brew install rsync              # rsync 3.4.x at /opt/homebrew/bin/rsync
```

### Verify, then hand off to DVC

A transfer that silently truncated is worse than one that failed. Confirm before the source goes away:

```bash
rclone check box:"path/to/folder" /data/dest --one-way   # hashes both sides
rsync -avhn --delete "$BOX/Some Folder/" user@<box>:/data/dest/   # -n: dry run, lists differences only
```

Then the data is a local directory like any other, and 7h applies: put the DVC cache on the data disk, `dvc add` at cohort granularity, and confirm `dvc remote list` before the first push if any of it is patient data.

---

## Step 8 — PyTorch

Check the driver before installing anything:

```bash
nvidia-smi
```

If that fails, install the driver (`sudo ubuntu-drivers install`) and reboot before continuing. The CUDA version `nvidia-smi` prints is the *maximum* the driver supports; the wheel bundles its own runtime, so the driver only needs to be at least as new as the wheel's CUDA.

With the venv from Step 5 active:

```bash
# CUDA 12.x box
uv pip install torch --index-url https://download.pytorch.org/whl/cu124

# CPU-only box
uv pip install torch --index-url https://download.pytorch.org/whl/cpu
```

Recent uv versions can also detect the right build with `uv pip install torch --torch-backend=auto`. The explicit `--index-url` form matches PyTorch's own documentation, so prefer it when you want the install unambiguous and reproducible.

```bash
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'cpu')"
```

- [ ] `torch.cuda.is_available()` is `True` (or CPU-only is deliberate)

---

## Step 9 — nnU-Net v2

```bash
uv pip install nnunetv2==2.8.1
```

For framework development — new trainers, modified loss, anything where you edit nnU-Net itself — clone so the source is on your path:

```bash
git clone https://github.com/MIC-DKFZ/nnUNet.git ~/src/nnUNet
uv pip install -e ~/src/nnUNet
```

### Choosing which disk holds what

On a machine with more than one disk, decide this *before* exporting the variables. Moving 200GB of preprocessed data later because you guessed wrong is a wasted afternoon.

**Step 0 — count the physical drives.**

Establish how many drives exist before comparing them. Bare `lsblk` buries the answer: on any box with snaps installed, thirty-plus `loop` devices for `/snap/...` mounts scroll past before real hardware appears.

```bash
lsblk -d -o NAME,SIZE,TYPE,TRAN,ROTA,MODEL,SERIAL -e7
lsblk -d -n -o NAME -e7 | wc -l          # just the count
```

`-d` shows disks only and suppresses their partitions, so one line is one physical drive. `-e7` excludes major number 7 — the loop devices — which is what makes the output readable at all.

```
NAME      SIZE TYPE TRAN   ROTA MODEL               SERIAL
sda     931.5G disk usb       0 Portable SSD        XXXXXXXXXXXXXXX
nvme0n1   1.8T disk nvme      0 NVMe SSD 2TB        XXXXXXXXXXXXXXX
```

Include `SERIAL`. It distinguishes two otherwise identical drives, and it is the only identifier that survives a rename — `sda` and `sdb` can swap across a reboot depending on enumeration order, which is why the fstab entry under *Mount the second disk somewhere stable* mounts by UUID and not by device path.

Three things this will not show you. Under LVM or mdadm, several physical drives present as a single logical volume — drop `-d` to see the tree, then confirm with `sudo pvs` or `cat /proc/mdstat`. A brand-new unformatted drive appears with an empty `FSTYPE` and no partition children, which is exactly what a good training-data candidate looks like. And an empty SD-card or media reader can appear as a zero-size device; ignore anything reporting `0B`.

If a drive is physically present but missing from that list, `ls /sys/block/` is the kernel's unformatted view, and `sudo lshw -class disk -short` reports each drive alongside the controller behind it, which catches one that enumerated but failed to come up cleanly.

**Is there room for another drive?** If the rest of this section concludes that no existing disk is suitable, the next question is whether one can be added rather than which to compromise on:

```bash
sudo dmidecode -t slot          # PCIe and M.2 slots, each marked in use or available
sudo lshw -class storage        # the controllers behind them
```

**Step 1 — inventory the disks.**

```bash
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,ROTA,TRAN,MODEL
df -hT -x tmpfs -x devtmpfs -x squashfs
```

`MOUNTPOINTS` (plural) requires util-linux 2.37 or newer. On Ubuntu 20.04 and older the column is the singular `MOUNTPOINT`, and passing the plural form makes the whole command fail rather than degrade.

Four columns answer "which is which":

| Column | Meaning |
|---|---|
| `ROTA` | `1` = spinning HDD, `0` = SSD or NVMe. The single most important number. |
| `TRAN` | `nvme` ≫ `sata` > `usb`. Transport usually caps throughput before the media does. |
| `FSTYPE` | `ext4`/`xfs` are usable; `exfat`/`ntfs`/`vfat` are a trap — see step 2. |
| `MODEL` | The actual device, so you can look up its rated speed. |

A representative two-disk desktop — internal NVMe holding the OS, plus a large external SSD:

```
NAME        SIZE TYPE FSTYPE MOUNTPOINTS         ROTA TRAN MODEL
sda       931.5G disk                               0 usb  Portable SSD
└─sda1    931.5G part exfat  /media/user/SSD_1TB    0
nvme0n1     1.8T disk                               0 nvme NVMe SSD 2TB
└─nvme0n1p5 1.6T part ext4   /                      0 nvme
```

Both report `ROTA=0`, so "is it an SSD" does not discriminate them. `TRAN` does: NVMe on a PCIe Gen4 link runs several GB/s, while a USB-attached SSD is capped by the bridge nearer 1GB/s, and its random-read IOPS — what training actually consumes — are far worse than the sequential number implies.

**Step 2 — check the filesystem before trusting the capacity.** This is the trap that costs the most time. Removable disks ship formatted exFAT or NTFS, which support neither hardlinks nor symlinks:

```bash
T=<mount>/.captest
touch "$T"
ln    "$T" "$T.hard" 2>/dev/null && echo "hardlinks: yes" || echo "hardlinks: NO"
ln -s "$T" "$T.sym"  2>/dev/null && echo "symlinks:  yes" || echo "symlinks:  NO"
rm -f "$T" "$T.hard" "$T.sym"
```

A `NO` on either has consequences beyond nnU-Net. DVC links cached files into the workspace using reflink/hardlink/symlink and falls back to full **copies** on such a filesystem, so every tracked dataset occupies twice the space. Treat an exFAT/NTFS volume as bulk archive storage, not a training target.

**Step 3 — measure rather than assume**, if the choice isn't obvious:

```bash
dd if=/dev/zero of=<mount>/speedtest bs=1M count=4096 oflag=direct status=progress
dd if=<mount>/speedtest of=/dev/null bs=1M iflag=direct status=progress
rm <mount>/speedtest
```

Random reads matter more than sequential for training. If `fio` is available, that is the honest test:

```bash
fio --name=randread --rw=randread --bs=64k --size=2G --numjobs=4 \
    --runtime=30 --time_based --group_reporting --filename=<mount>/fiotest
rm <mount>/fiotest
```

**Step 4 — place the three directories by access pattern.**

| Variable | Access pattern | Put it on |
|---|---|---|
| `nnUNet_raw` | Read heavily during `plan_and_preprocess`, then idle | The **large** disk. Slow is acceptable. |
| `nnUNet_preprocessed` | Random patch reads every iteration of every epoch | The **fastest** disk, always local, `ext4`/`xfs`. |
| `nnUNet_results` | Checkpoints written periodically, read at inference | Whichever disk you back up. Small, speed irrelevant. |

`nnUNet_preprocessed` is the only one where the choice changes wall-clock time, because it sits in the training inner loop. Everything else is a capacity decision.

**Budget the space realistically.** Preprocessed data is resampled float32 stored as `.npy`, and nnU-Net unpacks the compressed `.npz` before training. Against compressed `.nii.gz` inputs that is commonly a **3–10×** expansion, so a 100GB raw CT dataset can want several hundred GB preprocessed. Check headroom before you start, not when preprocessing dies at 90%.

**When the fast disk is too full**, the fix is not to move preprocessed to the slow disk — that penalizes every epoch. Better, in order: free space; keep only the active dataset preprocessed and delete others (they regenerate); or split at the dataset level with a symlink:

```bash
ln -s /mnt/bigdisk/Dataset501_Spine "$nnUNet_preprocessed/Dataset501_Spine"
```

**Mount the second disk somewhere stable.** Desktop auto-mounts under `/media/<user>/<label>` only appear after the session unlocks, so a training job started by systemd or cron finds nothing there. Give it a fixed mount — reformatting to ext4 first if step 2 showed exFAT or NTFS:

```bash
sudo mkfs.ext4 -L data /dev/sdX1          # DESTROYS the disk — back up first
sudo blkid /dev/sdX1                       # copy the UUID
sudo mkdir -p /data
echo 'UUID=<uuid> /data ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
sudo mount -a && sudo chown "$USER:$USER" /data
```

`noatime` suppresses a metadata write on every read, which matters when training reads millions of patches. Verify `mount -a` succeeds *before* rebooting — a bad fstab line can drop the machine to emergency mode.

### The three environment variables

nnU-Net refuses to run without these, and the resulting error names the variable but not the fix. Add to `~/.bashrc`, then `source ~/.bashrc`:

```bash
export nnUNet_raw="/data/nnUNet_raw"
export nnUNet_preprocessed="$HOME/nnUNet_preprocessed"     # fastest disk
export nnUNet_results="$HOME/nnUNet_results"
```

```bash
mkdir -p "$nnUNet_raw" "$nnUNet_preprocessed" "$nnUNet_results"
echo "$nnUNet_raw"    # must print a path, not an empty line
```

Point these at the disks chosen above rather than defaulting all three to `$HOME`, which is the decision the previous section exists to prevent.

- [ ] `nnUNetv2_plan_and_preprocess -h` prints help without a traceback

---

## Step 10 — TotalSegmentator

```bash
uv pip install TotalSegmentator==2.17.0
```

Install it into the *same* venv as nnU-Net and torch — TotalSegmentator runs on nnU-Net underneath, and splitting them across environments means two torch builds and a GPU-visibility bug hunt.

### Weights

Weights download on first use, which fails badly on an air-gapped or firewalled box mid-run. Pre-pull them:

```bash
totalseg_download_weights -t total
totalseg_download_weights -t total_mr     # if you'll do MR
```

Default cache is `~/.totalsegmentator/nnunet/results`. To relocate:

```bash
export TOTALSEG_HOME_DIR=/data/.totalsegmentator    # add to ~/.bashrc
```

### Licensing — check before this goes near product work

The default `total` (CT) and `total_mr` (MR) tasks are **Apache-2.0 and free for commercial use**. Spine refinement tasks are also free: `vertebrae_body` (body without the arch), `vertebrae_pp` and `vertebrae_pp_refined` (per-vertebra C1–L5).

Other subtasks — `heartchambers_highres`, `appendicular_bones`, `tissue_types`, `coronary_arteries`, `aortic_sinuses`, `brain_structures` — **require a license key**. Free keys exist for non-commercial use via the academic portal; commercial use means contacting the maintainer.

```bash
totalseg_set_license -l aca_XXXXXXXXX
```

Answer "is this task licensed?" *before* a task enters a pipeline, not after. An academic key on a commercial pipeline is a licensing problem, not a technical one. Verify current terms upstream — licensing changes faster than documentation.

### Smoke test

```bash
TotalSegmentator -i <ct>.nii.gz -o /tmp/seg_test
TotalSegmentator -i <ct>.nii.gz -o /tmp/seg_test_ml.nii.gz -ml -f       # multilabel, fast
```

Use public or phantom data, never a patient case.

---

## Step 11 — MLflow experiment tracking

### 11a. Install

```bash
uv pip install mlflow==3.14.0          # in the project venv — client + server
uv tool install mlflow==3.14.0         # optional: standalone `mlflow server` CLI
```

MLflow 3.14 requires Python ≥3.10, the same floor as nnU-Net. If a machine only *logs* runs and never serves the UI, `mlflow-skinny` is a much smaller install with the same logging API.

### 11b. Logging a run

The client half is three lines: point at a tracking server, name an experiment so runs do not all pile into `Default`, and wrap the training loop.

```python
import mlflow

mlflow.set_tracking_uri("http://127.0.0.1:5000")
mlflow.set_experiment("spine-seg-lumbar")

with mlflow.start_run(run_name="nnunet-3d-fullres-fold0"):
    mlflow.log_params({"fold": 0, "lr": 1e-2, "patch": "128x128x128"})
    for epoch in range(n_epochs):
        mlflow.log_metric("dice_val", dice, step=epoch)
    mlflow.log_artifact("plots/dice_curve.png")
```

The `step` argument is the difference between a curve and a single final number. Omitting it is the most common reason an MLflow chart shows one dot.

`mlflow.autolog()` captures params, metrics and the model automatically and is a fair default for scikit-learn or Lightning. It does much less for nnU-Net, which owns its training loop and writes its own logs — there you log explicitly, and the honest integration point is a callback or a post-hoc parse of nnU-Net's `training_log*.txt`.

Log what lets you reconstruct the run a year later: the git commit, the **data version**, and the environment. The data version is the one people omit, and it is the one that separates a reproducible experiment from a merely recorded one:

```python
mlflow.log_params({
    "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
})
mlflow.log_artifact("data/raw.dvc")     # the DVC pointer names the exact cohort
```

Logging the `.dvc` pointer rather than a path is deliberate: a path tells you where the data was, the pointer's hash tells you *which* data it was, and only the second survives someone overwriting the directory.

### 11c. Local server

Prefer one shared server across related projects over a database per repo — runs stay comparable and the registry has a single home.

```bash
mlflow server \
  --backend-store-uri sqlite:///mlflow.db \
  --artifacts-destination ./mlartifacts \
  --host 127.0.0.1 --port 5000
```

`mlflow ui` is the same server with fewer knobs; use `mlflow server` once you care about artifact destinations.

```bash
export MLFLOW_TRACKING_URI=http://127.0.0.1:5000     # add to ~/.bashrc
```

**That SQLite file is the system of record.** It is not reconstructable from the artifacts and it is not in git. Back it up, or accept losing every run's history. Put it on a disk you actually back up (the Step 9 survey applies).

To keep it running across logins, a user unit beats a stray terminal:

```ini
# ~/.config/systemd/user/mlflow.service
[Unit]
Description=MLflow tracking server
[Service]
WorkingDirectory=%h/projects/<repo>
ExecStart=%h/.local/bin/mlflow server --backend-store-uri sqlite:///mlflow.db \
          --artifacts-destination %h/projects/<repo>/mlartifacts \
          --host 127.0.0.1 --port 5000
Restart=on-failure
[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now mlflow
systemctl --user status mlflow
```

### 11d. Shared server on S3

For a server more than one person uses, move the backend to Postgres and artifacts to the S3 bucket from Step 7:

```bash
mlflow server \
  --backend-store-uri postgresql://<user>@<host>:5432/mlflow \
  --artifacts-destination s3://<bucket>/mlflow \
  --serve-artifacts \
  --host 0.0.0.0 --port 5000
```

`--serve-artifacts` (on by default) proxies artifact traffic through the server, so clients need no S3 credentials of their own — the server's IAM role does the writing. That is what you want when analysts should see plots without being handed bucket access. Without it, every client needs its own credentials and the same S3 permissions from 7c.

### 11e. Security and sensitive data

**`--host 0.0.0.0` publishes an unauthenticated server.** Default MLflow has no auth: anyone who can reach the port can read every run and delete experiments. In order of preference: keep it on `127.0.0.1` and reach remote instances over an SSH tunnel (`ssh -L 5000:127.0.0.1:5000 <host>`); enable built-in basic auth with `--app-name basic-auth`; or put it behind a reverse proxy that handles authentication.

### 11f. Reaching the UI from a laptop

The 11c server binds `127.0.0.1`, so nothing off the box can reach it — the correct default. To view the UI from a laptop, forward the port over SSH rather than rebinding the server to `0.0.0.0`:

```bash
# from the laptop
ssh -N -L 5000:127.0.0.1:5000 <user>@<box>
```

Then open `http://127.0.0.1:5000` in the laptop's browser. `-N` means "no remote command", so you get the tunnel and nothing else; add `-f` to push it into the background. Nothing is exposed to the network and no authentication has to be configured, because the traffic rides an SSH session you already trust.

If port 5000 is taken locally, remap the left side only: `-L 5001:127.0.0.1:5000`, then browse to 5001. The right-hand side is the address *on the box* and stays `127.0.0.1:5000` regardless — reversing those two is the usual reason a tunnel connects but the page never loads.

To stop rebuilding it by hand, put it in the laptop's `~/.ssh/config` so it comes up with every connection to that host:

```
Host mlbox
    HostName <ip-or-hostname>
    User <user>
    LocalForward 5000 127.0.0.1:5000
```

Then `ssh mlbox` both logs you in and forwards the UI. The same pattern reaches any other loopback-bound service on the box — add a `LocalForward` line per port.

---

## Step 12 — End-to-end verification

```bash
ssh -T git@bitbucket.org
ssh -T git@github.com                 # exit status 1 on success is normal — read the message
aws sts get-caller-identity
dvc --version && dvc remote list
dvc cache dir                         # should be the data disk, not $HOME
python -c "import torch, nnunetv2, totalsegmentator, mlflow; print('torch', torch.__version__, '| cuda', torch.cuda.is_available())"
echo "$nnUNet_raw $nnUNet_preprocessed $nnUNet_results"
curl -sf "$MLFLOW_TRACKING_URI/health" && echo " mlflow ok"
tmux -V && tmux ls 2>/dev/null || echo "tmux installed, no sessions yet"
lsblk -d -o NAME,SIZE,TRAN,MODEL -e7   # the drives you decided on in Step 9
nvidia-smi
```

---

## Onboarding a project — repo, data, and the first tracked run

Steps 1–12 provision the box; this is the first real project on it. Clone a training repo, take a cohort already sitting on the data disk from the transfer section, put it under DVC against an S3 bucket, and get a training run into MLflow. Every component is documented above — 7d for the remote, 7h for `dvc add`, 11b for run logging. What this section adds is the order, and the two failures that only appear when the pieces are combined.

### Clone the repo onto the data disk, not into `$HOME`

This is the decision that quietly determines whether every dataset costs one copy or two.

DVC's `cache.type reflink,hardlink,symlink` (7h) works by sharing storage between the cache and the working tree. **Reflink and hardlink cannot cross a filesystem boundary.** If the cache is on `/data` and the repo is in `$HOME` on the OS disk, DVC walks the list, finds neither can apply, and silently falls back to `copy` — so a 200GB cohort occupies 200GB of cache plus 200GB of working tree, on two different disks, with no warning. The symptom is a data disk that fills at twice the expected rate.

Keeping the repo and the cache on the same filesystem removes the problem entirely:

```bash
sudo mkdir -p /data/repos && sudo chown "$USER:$USER" /data/repos
cd /data/repos
git clone git@github.com:<org>/<repo>.git      # SSH keys from Step 3
cd <repo>
```

```bash
uv venv --python 3.11 && source .venv/bin/activate     # Step 4
uv pip install -e .                                     # or -r requirements.txt
uv pip install 'dvc[s3]' mlflow
```

### Initialize DVC with the cache on the same disk

```bash
dvc init
dvc cache dir /data/dvc-cache
dvc config cache.type reflink,hardlink,symlink
git add .dvc/config && git commit -m "dvc: cache on the data disk"
```

Confirm the constraint above actually holds, rather than assuming it:

```bash
stat -c '%d %n' /data/dvc-cache .      # same first number = same filesystem
```

Different numbers mean you are about to pay for every dataset twice. Fix it before the first `dvc add`, because changing the cache location afterwards re-hashes everything.

### Move the cohort into the repo

DVC tracks paths *inside* the working tree, so data parked at `/data/incoming` has to move in. On the same filesystem this is a metadata operation — instant, no bytes copied, regardless of cohort size:

```bash
stat -c '%d %n' /data/incoming .      # verify same filesystem FIRST
mkdir -p data
mv /data/incoming/<cohort> data/raw
```

If those device numbers differ, `mv` degrades into a full copy-then-delete and needs the cohort's size free on the destination. Check before running it on 200GB.

Resist `dvc add --external` for data that lives outside the repo. It exists, it is discouraged in DVC 3.x, and it gives up the guarantee that a clone plus `dvc pull` reproduces the tree.

### Point at S3 and push

Bucket creation and IAM are 7b and 7c; the wiring is 7d:

```bash
dvc remote add -d storage s3://<bucket>/<repo-name>
dvc remote modify storage profile <profile>
dvc remote modify storage region <region>
git add .dvc/config && git commit -m "dvc: point remote at s3"
```

```bash
dvc add data/raw
git add data/raw.dvc data/.gitignore && git commit -m "track raw cohort"
dvc push
```

**Verify the sharing worked**, which is the whole point of the disk layout above. Free space should barely move across `dvc add`, because the cache entry and the working file are the same extents:

```bash
df -h /data | tail -1     # before dvc add
dvc add data/raw
df -h /data | tail -1     # after — a second full copy means the fallback fired
```

A drop equal to the cohort size means you are on `copy`. Re-check `stat -c %d`, and on XFS confirm `xfs_info /data | grep reflink` reports `reflink=1`.

Before the first push of anything patient-derived, the 6c questions apply: `dvc remote list` names the bucket you think it does, and the de-identification status is settled. `dvc push` is an upload, and it is not undone by deleting the local copy.

### First tracked run

Point the client at the server from 11c and log against the cohort you just tracked:

```bash
export MLFLOW_TRACKING_URI=http://127.0.0.1:5000     # add to ~/.bashrc
python -c "import mlflow; print(mlflow.get_tracking_uri())"
```

Follow 11b for the run body. The one line to not skip is `mlflow.log_artifact("data/raw.dvc")` — that pointer's hash is what ties the run to *this* cohort rather than to a directory path that someone can overwrite next month. Together with `git_sha`, it is what makes the run reconstructable: the commit gives you the code, the pointer gives you the data, and `dvc pull` turns the pointer back into bytes.

### Order matters

- [ ] repo cloned on the data disk, same filesystem as the intended cache
- [ ] `dvc init` and `dvc cache dir` set **before** the first `dvc add`
- [ ] cohort moved inside the repo, verified same-filesystem so `mv` stays instant
- [ ] remote added and committed before `dvc push`
- [ ] `df` shows no second full copy after `dvc add`
- [ ] first run logs `git_sha` and the `.dvc` pointer, not a data path
