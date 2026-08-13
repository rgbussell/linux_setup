#!/usr/bin/env python3
"""Gate a ds-datakit dataset before it is allowed into a training run.

The failures this catches are the quiet ones -- the cohort trains, the loss
curve looks plausible, and the model is wrong:

  * an all-zero label volume (an export that "succeeded" and wrote nothing);
  * label integers outside the labelmap, or a labelmap class that never appears;
  * a mask whose affine disagrees with its image (right shape, wrong world);
  * a volume in a different orientation from the rest of the cohort;
  * the SAME image present in two datasets under two pseudonyms -- which puts one
    patient on both sides of a split and inflates every metric you report.

    python scripts/qc_cohort.py --dataset cohortA --dataset cohortB \
        --label-task segmentation --label-set gt-v1

Exit status is non-zero if any hard check fails, so it can gate a pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import nibabel as nib
    import numpy as np
except ImportError:  # pragma: no cover - environment guard
    sys.exit("nibabel and numpy are required: uv pip install nibabel numpy")

try:
    from ds_datakit.manifest import Manifest
except ImportError:  # pragma: no cover - environment guard
    sys.exit("ds-datakit is required: uv pip install -e '/data/repos/ds-datakit[dvc]'")


def content_key(path: Path) -> str:
    """Hash the voxel array, not the file: the same scan re-exported twice differs
    byte-for-byte (timestamps, compression level) but is identical as an image."""
    arr = np.asanyarray(nib.load(str(path)).dataobj)
    return hashlib.blake2b(np.ascontiguousarray(arr).tobytes(), digest_size=16).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", action="append", required=True, help="repeatable")
    ap.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("DS_DATAKIT_DATA_ROOT", Path.home() / "ds-data")),
    )
    ap.add_argument("--label-task", default="segmentation")
    ap.add_argument("--label-set", default=None, help="default: the only set present, else required")
    ap.add_argument(
        "--require-labels",
        action="store_true",
        help="fail on any study without a label set (default: report and continue)",
    )
    ap.add_argument(
        "--duplicate-scan",
        action="store_true",
        help="hash every volume to find the same image under two pseudonyms "
        "(reads all bytes -- slow on a large cohort, but the check that matters "
        "most when merging two cohorts)",
    )
    args = ap.parse_args()

    errors: list[str] = []
    warnings: list[str] = []
    orientations: Counter[str] = Counter()
    class_voxels: Counter[int] = Counter()
    labelmaps: dict[str, dict[str, str]] = {}
    hashes: dict[str, str] = {}
    patients: dict[str, set[str]] = defaultdict(set)
    n_studies = n_labelled = 0

    for dataset in args.dataset:
        base = args.data_root / dataset
        manifest_path = base / "manifest.json"
        if not manifest_path.is_file():
            errors.append(f"{dataset}: no manifest.json at {manifest_path}")
            continue
        manifest = Manifest.load_json(manifest_path)

        for rec in manifest.records:
            n_studies += 1
            patients[dataset].add(rec.patient_pseudo)
            image = base / rec.nifti_relpath
            tag = f"{dataset}/{rec.study_id}"
            if not image.is_file():
                errors.append(f"{tag}: image missing at {rec.nifti_relpath}")
                continue

            img = nib.load(str(image))
            orientations["".join(nib.aff2axcodes(img.affine))] += 1
            zooms = [float(z) for z in img.header.get_zooms()[:3]]
            if any(z <= 0 for z in zooms):
                errors.append(f"{tag}: non-positive voxel spacing {zooms}")
            elif max(zooms) / min(zooms) > 10:
                warnings.append(f"{tag}: extreme anisotropy {zooms}")

            if args.duplicate_scan:
                key = content_key(image)
                if key in hashes:
                    errors.append(f"{tag}: identical image content to {hashes[key]} -- same scan twice")
                else:
                    hashes[key] = tag

            label_root = base / rec.study_reldir / "labels" / args.label_task
            if not label_root.is_dir():
                (errors if args.require_labels else warnings).append(f"{tag}: no {args.label_task} labels")
                continue
            sets = sorted(p for p in label_root.iterdir() if p.is_dir())
            if args.label_set:
                chosen = [p for p in sets if p.name == args.label_set]
                if not chosen:
                    (errors if args.require_labels else warnings).append(
                        f"{tag}: label set {args.label_set!r} absent (have {[p.name for p in sets]})"
                    )
                    continue
                label_dir = chosen[0]
            elif len(sets) == 1:
                label_dir = sets[0]
            else:
                errors.append(f"{tag}: {len(sets)} label sets present -- pass --label-set")
                continue

            lm_path = label_dir / "labelmap.json"
            if lm_path.is_file():
                labelmaps[f"{dataset}:{label_dir.name}"] = json.loads(lm_path.read_text())

            masks = [
                p for p in label_dir.iterdir()
                if p.is_file() and p.name.endswith((".nii", ".nii.gz"))
            ]
            if len(masks) != 1:
                errors.append(f"{tag}: expected one mask in {label_dir}, found {len(masks)}")
                continue
            mask_path = masks[0]
            n_labelled += 1

            mask = nib.load(str(mask_path))
            if tuple(mask.shape) != tuple(img.shape):
                errors.append(f"{tag}: mask shape {mask.shape} != image shape {img.shape}")
                continue
            if not np.allclose(mask.affine, img.affine, atol=1e-3):
                errors.append(f"{tag}: mask affine disagrees with image -- same grid, different world")

            data = np.asanyarray(mask.dataobj)
            if not np.issubdtype(data.dtype, np.integer):
                rounded = np.round(data)
                if not np.allclose(data, rounded):
                    errors.append(f"{tag}: mask dtype {data.dtype} holds non-integer values")
                data = rounded.astype(np.int32)
            values = np.unique(data)
            foreground = values[values != 0]
            if foreground.size == 0:
                errors.append(f"{tag}: mask is entirely background -- an empty export")
                continue
            for value in foreground:
                class_voxels[int(value)] += int((data == value).sum())

            if lm_path.is_file():
                allowed = {int(k) for k in labelmaps[f"{dataset}:{label_dir.name}"]}
                stray = {int(v) for v in foreground} - allowed
                if stray:
                    errors.append(f"{tag}: mask values {sorted(stray)} absent from labelmap.json")

    # ---- cohort-level checks -------------------------------------------------
    if len(orientations) > 1:
        warnings.append(
            "mixed orientations across the cohort: "
            + ", ".join(f"{k}x{v}" for k, v in orientations.most_common())
            + " -- reorient to one convention before training"
        )

    distinct = {json.dumps(m, sort_keys=True) for m in labelmaps.values()}
    if len(distinct) > 1:
        errors.append(
            "labelmaps disagree across datasets -- merging them silently remaps classes:\n    "
            + "\n    ".join(f"{k}: {json.dumps(v, sort_keys=True)}" for k, v in labelmaps.items())
        )

    if labelmaps:
        declared = {int(k) for m in labelmaps.values() for k in m}
        never = sorted(declared - set(class_voxels) - {0})
        if never:
            warnings.append(f"labelmap classes never present in any mask: {never}")

    overlap_reported = set()
    for a in args.dataset:
        for b in args.dataset:
            if a >= b or (a, b) in overlap_reported:
                continue
            shared = patients[a] & patients[b]
            if shared:
                errors.append(f"{a} and {b} share {len(shared)} patient pseudonym(s): {sorted(shared)[:5]}")
            overlap_reported.add((a, b))

    # ---- report --------------------------------------------------------------
    print(f"studies: {n_studies}   labelled: {n_labelled}   patients: {sum(len(v) for v in patients.values())}")
    if class_voxels:
        print("class voxel counts: " + ", ".join(f"{k}={v:,}" for k, v in sorted(class_voxels.items())))
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"FAIL  {e}")
    print("PASS" if not errors else f"{len(errors)} failure(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
