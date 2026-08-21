#!/usr/bin/env bash
# model_dvc.sh — a DVC model-checkpoint registry on S3.
#
# One-time:
#   model_dvc.sh init --bucket <name> --repo <dir> [--region <aws-region>] [--cache-dir <dir>]
#     Creates + hardens the bucket (public-access block, default encryption,
#     versioning), makes <dir> a git + DVC repo, and sets s3://<bucket>/dvcstore
#     as the default remote. Safe to re-run.
#
# Per checkpoint:
#   model_dvc.sh checkin --repo <dir> --file <checkpoint> --name <model> --version <v> \
#       [--framework <s>] [--arch <s>] [--train-repo <repo@commit>] \
#       [--mlflow-run <id>] [--metrics '<json>'] [--notes <s>]
#     Places the checkpoint at models/<model>/<v>/, writes manifest.json next to
#     it (sha256, size, provenance), dvc-adds the checkpoint, commits the
#     pointer + manifest, and pushes to the remote.
set -euo pipefail

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "required tool not found: $1"; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

cmd="${1:-}"
if [ $# -gt 0 ]; then shift; fi

bucket="" repo="" region="" cache_dir=""
file="" name="" version="" framework="" arch="" train_repo="" mlflow_run="" metrics="" notes=""

while [ $# -gt 0 ]; do
  case "$1" in
    --bucket)     bucket="${2:?missing value for --bucket}"; shift 2 ;;
    --repo)       repo="${2:?missing value for --repo}"; shift 2 ;;
    --region)     region="${2:?missing value for --region}"; shift 2 ;;
    --cache-dir)  cache_dir="${2:?missing value for --cache-dir}"; shift 2 ;;
    --file)       file="${2:?missing value for --file}"; shift 2 ;;
    --name)       name="${2:?missing value for --name}"; shift 2 ;;
    --version)    version="${2:?missing value for --version}"; shift 2 ;;
    --framework)  framework="${2:?missing value for --framework}"; shift 2 ;;
    --arch)       arch="${2:?missing value for --arch}"; shift 2 ;;
    --train-repo) train_repo="${2:?missing value for --train-repo}"; shift 2 ;;
    --mlflow-run) mlflow_run="${2:?missing value for --mlflow-run}"; shift 2 ;;
    --metrics)    metrics="${2:?missing value for --metrics}"; shift 2 ;;
    --notes)      notes="${2:?missing value for --notes}"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *)            die "unknown argument: $1" ;;
  esac
done

case "$cmd" in
  init)
    need aws; need git; need dvc
    [ -n "$bucket" ] || die "--bucket is required"
    [ -n "$repo" ]   || die "--repo is required"
    region="${region:-$(aws configure get region 2>/dev/null || true)}"
    [ -n "$region" ] || die "--region is required (no default region configured)"

    if aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
      echo "bucket $bucket already exists — skipping creation/hardening"
    else
      if [ "$region" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$bucket" --region "$region"
      else
        aws s3api create-bucket --bucket "$bucket" --region "$region" \
          --create-bucket-configuration "LocationConstraint=$region"
      fi
      aws s3api put-public-access-block --bucket "$bucket" \
        --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
      aws s3api put-bucket-encryption --bucket "$bucket" \
        --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
      aws s3api put-bucket-versioning --bucket "$bucket" \
        --versioning-configuration Status=Enabled
      echo "bucket $bucket created + hardened in $region"
    fi

    mkdir -p "$repo"
    cd "$repo"
    [ -d .git ] || git init -b main
    [ -d .dvc ] || dvc init
    if [ -n "$cache_dir" ]; then
      dvc cache dir "$cache_dir"
      dvc config cache.type reflink,hardlink,symlink
    fi
    dvc remote add -d -f storage "s3://$bucket/dvcstore"
    mkdir -p models
    git add -A
    git commit -m "model registry: init (remote s3://$bucket/dvcstore)" >/dev/null 2>&1 || true
    echo "repo ready: $repo  ->  s3://$bucket/dvcstore"
    ;;

  checkin)
    need git; need dvc; need python3; need sha256sum
    [ -n "$repo" ]    || die "--repo is required"
    [ -n "$file" ]    || die "--file is required"
    [ -n "$name" ]    || die "--name is required"
    [ -n "$version" ] || die "--version is required"
    [ -f "$file" ]    || die "no such file: $file"
    if [ -n "$metrics" ]; then
      printf '%s' "$metrics" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null \
        || die "--metrics is not valid JSON"
    fi
    src="$(readlink -f "$file")"
    cd "$repo" 2>/dev/null || die "no such repo: $repo"
    [ -d .dvc ] || die "$repo is not a DVC repo — run init first"

    dest="models/$name/$version"
    base="$(basename "$src")"
    [ -e "$dest/$base" ] && die "$dest/$base already exists — bump --version instead of overwriting"
    mkdir -p "$dest"
    cp "$src" "$dest/$base"

    sha="$(sha256sum "$dest/$base" | cut -d' ' -f1)"
    size="$(stat -c%s "$dest/$base")"

    M_NAME="$name" M_VERSION="$version" M_FILE="$base" M_SHA="$sha" M_SIZE="$size" \
    M_FRAMEWORK="$framework" M_ARCH="$arch" M_TRAIN_REPO="$train_repo" \
    M_MLFLOW="$mlflow_run" M_METRICS="$metrics" M_NOTES="$notes" \
    python3 - > "$dest/manifest.json" <<'EOF'
import datetime
import json
import os

def opt(key):
    value = os.environ.get(key, "")
    return value if value else None

metrics_raw = os.environ.get("M_METRICS", "")
manifest = {
    "model_name": os.environ["M_NAME"],
    "version": os.environ["M_VERSION"],
    "file": os.environ["M_FILE"],
    "sha256": os.environ["M_SHA"],
    "size_bytes": int(os.environ["M_SIZE"]),
    "created_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "framework": opt("M_FRAMEWORK"),
    "architecture": opt("M_ARCH"),
    "training_code": opt("M_TRAIN_REPO"),
    "mlflow_run": opt("M_MLFLOW"),
    "metrics": json.loads(metrics_raw) if metrics_raw else None,
    "notes": opt("M_NOTES"),
}
print(json.dumps(manifest, indent=2))
EOF

    dvc add "$dest/$base"
    git add "$dest/$base.dvc" "$dest/.gitignore" "$dest/manifest.json"
    git commit -m "model: $name $version ($base)"
    dvc push
    echo
    dvc status -c
    echo "checked in: $dest/$base  (sha256 $sha)"
    ;;

  ""|help)
    usage
    ;;

  *)
    die "unknown command: $cmd (use init or checkin)"
    ;;
esac
