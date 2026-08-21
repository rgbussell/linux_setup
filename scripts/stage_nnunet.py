#!/usr/bin/env python3
"""Bridge one or more ds-datakit datasets into an nnU-Net v2 raw dataset.

The two layouts do not match and nothing converts between them automatically:

  ds-datakit  <MODALITY>/<patient>/<study>/nifti/<name>.nii.gz
              <MODALITY>/<patient>/<study>/labels/<task>/<set>/<mask>.nii.gz
  nnU-Net     nnUNet_raw/Dataset<ID>_<Name>/imagesTr/<case>_0000.nii.gz
              nnUNet_raw/Dataset<ID>_<Name>/labelsTr/<case>.nii.gz

Staging is by symlink, so a combined cohort costs no additional bytes and the
DVC-tracked tree stays the single source of truth.

    python scripts/stage_nnunet.py --dataset cohortA --dataset cohortB \
        --id 501 --name SpineCombined --label-set gt-v1 --write-splits

``--write-splits`` is the option to not skip. nnU-Net's default 5-fold split is
random over *cases*; when one patient contributed two studies, that patient
lands in train and val at once and every reported metric is optimistic. This
writes a patient-disjoint ``splits_final.json`` instead.

The file goes in the **raw** dataset folder, next to ``dataset.json``. That is a
supported nnU-Net hook: ``ExperimentPlanner.__init__`` copies
``$nnUNet_raw/Dataset<ID>_<Name>/splits_final.json`` into the preprocessed
folder during ``plan_and_preprocess``. Writing it here means staging happens in
one step, before preprocessing, with no ordering to remember.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    from ds_datakit.manifest import Manifest
except ImportError:  # pragma: no cover - environment guard
    sys.exit("ds-datakit is required: uv pip install -e '/data/repos/ds-datakit[dvc]'")


def link(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    dest.symlink_to(src.resolve())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", action="append", required=True, help="ds-datakit dataset (repeatable)")
    ap.add_argument("--id", type=int, required=True, help="nnU-Net dataset id, e.g. 501")
    ap.add_argument("--name", required=True, help="nnU-Net dataset name, e.g. SpineCombined")
    ap.add_argument("--label-task", default="segmentation")
    ap.add_argument("--label-set", required=True, help="e.g. gt-v1 -- must exist in every dataset")
    ap.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("DS_DATAKIT_DATA_ROOT", Path.home() / "ds-data")),
    )
    ap.add_argument(
        "--nnunet-raw",
        type=Path,
        default=Path(os.environ["nnUNet_raw"]) if os.environ.get("nnUNet_raw") else None,
        help="defaults to $nnUNet_raw",
    )
    ap.add_argument(
        "--channel-name",
        default="CT",
        help="nnU-Net normalisation is chosen by this string: 'CT' triggers global "
        "foreground-percentile normalisation, anything else falls back to per-image "
        "z-score. Setting it wrong changes the model, silently. (default: CT)",
    )
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--write-splits", action="store_true", help="patient-disjoint splits_final.json")
    args = ap.parse_args()

    if args.nnunet_raw is None:
        return _fail("set $nnUNet_raw or pass --nnunet-raw")

    out = args.nnunet_raw / f"Dataset{args.id:03d}_{args.name}"
    images_dir, labels_dir = out / "imagesTr", out / "labelsTr"

    cases: list[str] = []
    case_patient: dict[str, str] = {}
    labelmap: dict[str, str] | None = None

    for dataset in args.dataset:
        base = args.data_root / dataset
        manifest_path = base / "manifest.json"
        if not manifest_path.is_file():
            return _fail(f"{dataset}: no manifest.json -- register the cohort first")
        manifest = Manifest.load_json(manifest_path)

        for rec in manifest.records:
            label_dir = base / rec.study_reldir / "labels" / args.label_task / args.label_set
            if not label_dir.is_dir():
                print(f"skip {dataset}/{rec.study_id}: no label set {args.label_set!r}", file=sys.stderr)
                continue
            masks = [
                p for p in label_dir.iterdir()
                if p.is_file() and p.name.endswith((".nii", ".nii.gz"))
            ]
            if len(masks) != 1:
                return _fail(f"{dataset}/{rec.study_id}: expected one mask, found {len(masks)}")

            lm_path = label_dir / "labelmap.json"
            if lm_path.is_file():
                this = json.loads(lm_path.read_text())
                if labelmap is None:
                    labelmap = this
                elif this != labelmap:
                    return _fail(
                        f"{dataset}/{rec.study_id}: labelmap differs from the first dataset -- "
                        "merging would remap classes; reconcile them before staging"
                    )

            case = f"{dataset}_{rec.study_id}"
            link(base / rec.nifti_relpath, images_dir / f"{case}_0000.nii.gz")
            link(masks[0], labels_dir / f"{case}.nii.gz")
            cases.append(case)
            case_patient[case] = f"{dataset}:{rec.patient_pseudo}"

    if not cases:
        return _fail("no labelled studies staged")
    if labelmap is None:
        return _fail("no labelmap.json found -- ds-datakit requires one for segmentation")

    # ds-datakit stores value -> class; nnU-Net wants class -> value.
    labels = {"background": 0}
    for value, name in sorted(labelmap.items(), key=lambda kv: int(kv[0])):
        if int(value) != 0:
            labels[str(name)] = int(value)

    (out / "dataset.json").write_text(
        json.dumps(
            {
                "channel_names": {"0": args.channel_name},
                "labels": labels,
                "numTraining": len(cases),
                "file_ending": ".nii.gz",
            },
            indent=2,
        )
        + "\n"
    )
    print(f"staged {len(cases)} cases -> {out}")
    print(f"classes: {len(labels)} (including background)")

    by_patient: dict[str, list[str]] = defaultdict(list)
    for case in sorted(cases):
        by_patient[case_patient[case]].append(case)

    if args.write_splits:
        patients = sorted(by_patient)
        if len(patients) < args.folds:
            return _fail(f"{len(patients)} patients cannot fill {args.folds} folds")

        splits = []
        for fold in range(args.folds):
            val_patients = {p for i, p in enumerate(patients) if i % args.folds == fold}
            val = sorted(c for p in val_patients for c in by_patient[p])
            train = sorted(c for p in patients if p not in val_patients for c in by_patient[p])
            splits.append({"train": train, "val": val})

        dest = out / "splits_final.json"
        dest.write_text(json.dumps(splits, indent=2) + "\n")
        print(f"wrote {dest}  ({len(patients)} patients, patient-disjoint {args.folds}-fold)")
        print("plan_and_preprocess copies this into $nnUNet_preprocessed. If a splits_final.json")
        print("is already there from an earlier run, planning asserts rather than overwriting --")
        print("delete the preprocessed copy to adopt these splits.")
    else:
        multi = sum(1 for studies in by_patient.values() if len(studies) > 1)
        if multi:
            print(
                f"WARNING: {multi} patient(s) contributed more than one study. Without "
                "--write-splits, nnU-Net's random split will put the same patient in "
                "train and val at once.",
                file=sys.stderr,
            )
    return 0


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
