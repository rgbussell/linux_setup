#!/usr/bin/env python3
"""Register an already-NIfTI cohort into the ds-datakit canonical layout.

ds-datakit's ``ingest`` is DICOM-only: it de-identifies, converts, and writes the
manifest in one pass. A cohort that is *already* NIfTI and already de-identified
has no DICOM to ingest, so this script does the only part that is missing --
place the volumes in the canonical tree and write ``manifest.json`` -- using the
same dataclasses ds-datakit itself uses. Everything downstream (``track``,
``label add``, ``push``, ``pull``, ``query``, ``card``) then works unchanged.

    python scripts/register_nifti_cohort.py \
        --images /data/incoming/cohortA/images \
        --dataset spine_ct \
        --modality CT

Layout produced (the grandparent rule is load-bearing: ds-datakit derives a
study's directory from the NIfTI path's parent.parent, so the file MUST sit at
``<MODALITY>/<patient>/<study>/nifti/<name>``)::

    $DS_DATAKIT_DATA_ROOT/<dataset>/
      CT/P_<hex>/S_<hex>/nifti/<name>.nii.gz
      manifest.json

Labels are attached afterwards with ``ds-datakit label add`` (which enforces the
geometry and labelmap checks), not here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import nibabel as nib
except ImportError:  # pragma: no cover - environment guard
    sys.exit("nibabel is required: uv pip install nibabel")

try:
    from ds_datakit.manifest import Manifest, SeriesRecord
except ImportError:  # pragma: no cover - environment guard
    sys.exit("ds-datakit is required: uv pip install -e '/data/repos/ds-datakit[dvc]'")

NIFTI_SUFFIXES = (".nii.gz", ".nii")


def stem_of(path: Path) -> str:
    """Filename without a NIfTI suffix (``Path.stem`` leaves ``.nii`` on ``.nii.gz``)."""
    name = path.name
    for suffix in NIFTI_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def pseudo(prefix: str, key: str, salt: str, mode: str) -> str:
    """A stable ``P_``/``S_`` identifier for a source key.

    ``hash`` is the default because source case identifiers are routinely
    quasi-identifiers -- a name like ``250714.RB.02`` encodes a service date and
    initials, which is re-identifying even though no name appears in the file.
    """
    if mode == "passthrough":
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
        return f"{prefix}_{safe}"
    digest = hashlib.blake2b(
        key.encode("utf-8"), digest_size=6, salt=salt.encode("utf-8")[:16]
    ).hexdigest()
    return f"{prefix}_{digest}"


def place(src: Path, dest: Path, mode: str) -> None:
    """Materialise ``src`` at ``dest`` under the chosen link strategy."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    if mode == "move":
        shutil.move(str(src), str(dest))
        return
    if mode == "hardlink":
        try:
            os.link(src, dest)
            return
        except OSError:
            pass  # cross-filesystem: fall through to a copy
    if mode == "symlink":
        dest.symlink_to(src.resolve())
        return
    shutil.copy2(src, dest)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images", required=True, type=Path, help="directory of .nii/.nii.gz volumes")
    ap.add_argument("--dataset", required=True, help="dataset name under the data root")
    ap.add_argument("--modality", default="CT", help="CT, MR, XR ... (default: CT)")
    ap.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("DS_DATAKIT_DATA_ROOT", Path.home() / "ds-data")),
        help="defaults to $DS_DATAKIT_DATA_ROOT",
    )
    ap.add_argument(
        "--patient-regex",
        default=None,
        help="regex whose group 1 extracts a PATIENT key from the filename stem; "
        "without it each volume is treated as its own patient. Get this right -- "
        "it is what makes patient-disjoint splits possible later.",
    )
    ap.add_argument("--id-mode", choices=("hash", "passthrough"), default="hash")
    ap.add_argument("--id-salt", default="", help="salt for --id-mode hash (keep it constant per dataset)")
    ap.add_argument(
        "--link",
        choices=("copy", "hardlink", "symlink", "move"),
        default="copy",
        help="copy is safest; hardlink costs no extra bytes on the same filesystem "
        "but DVC will make the shared inode read-only, which also affects the source",
    )
    ap.add_argument(
        "--map-out",
        type=Path,
        default=None,
        help="write the source->pseudonym map here (keep it OUT of the data root)",
    )
    ap.add_argument(
        "--replace",
        action="store_true",
        help="re-place volumes for series already in the manifest. Without this a "
        "re-run skips them, so a CORRECTED source file is silently ignored -- the "
        "identifier is what is already registered, not the bytes.",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    volumes = sorted(
        p for p in args.images.rglob("*") if p.is_file() and p.name.endswith(NIFTI_SUFFIXES)
    )
    if not volumes:
        return _fail(f"no NIfTI volumes under {args.images}")

    base = args.data_root / args.dataset
    manifest_path = base / "manifest.json"
    manifest = (
        Manifest.load_json(manifest_path)
        if manifest_path.is_file()
        else Manifest(dataset=args.dataset)
    )
    known = {r.series_id for r in manifest.records}

    pattern = re.compile(args.patient_regex) if args.patient_regex else None
    id_map: dict[str, dict[str, str]] = {}
    added = skipped = 0

    for vol in volumes:
        stem = stem_of(vol)
        if pattern:
            match = pattern.search(stem)
            if not match:
                return _fail(f"--patient-regex did not match {stem!r}; fix it before registering")
            patient_key = match.group(1)
        else:
            patient_key = stem

        patient_pseudo = pseudo("P", patient_key, args.id_salt, args.id_mode)
        study_id = pseudo("S", stem, args.id_salt, args.id_mode)
        series_id = pseudo("R", stem, args.id_salt, args.id_mode)

        if series_id in known and not args.replace:
            skipped += 1
            continue

        img = nib.load(str(vol))
        shape = tuple(int(x) for x in img.shape)
        if len(shape) < 3:
            return _fail(f"{vol} is {len(shape)}D; expected a 3D volume")
        zooms = [float(z) for z in img.header.get_zooms()[:3]]

        rel_nifti = Path(args.modality) / patient_pseudo / study_id / "nifti" / vol.name
        dest = base / rel_nifti
        if not args.dry_run:
            place(vol, dest, args.link)

        manifest.upsert(
            SeriesRecord(
                modality=args.modality,
                patient_pseudo=patient_pseudo,
                study_id=study_id,
                series_id=series_id,
                n_instances=shape[2],
                rows=shape[1],
                columns=shape[0],
                pixel_spacing=[zooms[0], zooms[1]],
                slice_thickness=zooms[2],
                series_description=None,
                acquisition={"orientation": "".join(nib.aff2axcodes(img.affine))},
                dicom_reldir="",
                nifti_relpath=str(rel_nifti),
                labels=[],
            )
        )
        id_map[stem] = {
            "patient_key": patient_key,
            "patient_pseudo": patient_pseudo,
            "study_id": study_id,
            "series_id": series_id,
            "source": str(vol),
        }
        added += 1

    manifest.generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest.tool_version = "register_nifti_cohort/1"

    patients = {r.patient_pseudo for r in manifest.records}
    print(
        f"{args.dataset}: +{added} series ({skipped} already registered) -> "
        f"{len(manifest.records)} series / {len(patients)} patients"
    )
    if added and len(patients) == len(manifest.records) and args.patient_regex is None:
        print(
            "NOTE: every volume became its own patient. If any subject has more than "
            "one scan, pass --patient-regex or the train/val split will leak.",
            file=sys.stderr,
        )

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    manifest.write_json(manifest_path)
    print(f"wrote {manifest_path}")

    map_out = args.map_out
    if map_out is None:
        map_out = args.data_root.parent / f"{args.dataset}.idmap.json"
    if args.data_root in map_out.parents or map_out.parent == args.data_root:
        return _fail(f"--map-out must live outside the data root, got {map_out}")
    existing = json.loads(map_out.read_text()) if map_out.is_file() else {}
    existing.update(id_map)
    map_out.parent.mkdir(parents=True, exist_ok=True)
    map_out.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
    print(f"wrote {map_out}  (source -> pseudonym; keep out of git and DVC)")
    return 0


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
