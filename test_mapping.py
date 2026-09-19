# -*- coding: utf-8 -*-
"""Configuration checks over ``loader/datas.json``.

These are deliberately cheap — they walk the *shape* of the configuration and
parse at most one file per dataset. The expensive full-corpus parse already
lives in ``test_poetry.py``.

``test_excludes_name_real_files`` is the one that matters most: a typo in an
``excludes`` list silently changes which files a dataset reads. ``songci``
listed ``authors.song.json`` while the file on disk is ``author.song.json``
(no plural ``s``), so the author file was read as if it were a corpus file and
``body_extractor('songci')`` raised ``KeyError: 'paragraphs'``.
"""
import json
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(REPO_ROOT, "loader", "datas.json")

with open(CONFIG_PATH, encoding="utf-8") as _f:
    CONFIG = json.load(_f)

DATASET_KEYS = sorted(CONFIG["datasets"])


def _dir_of(spec):
    return os.path.normpath(os.path.join(CONFIG["cp_path"], spec["path"]))


def source_files(spec):
    """The files a dataset contributes, mirroring ``PlainDataLoader``.

    Deliberately reimplemented here rather than imported, so that a silent
    change to the loader's walk cannot silently change what gets validated.
    ``test_loader_compat`` asserts the two agree.
    """
    path = _dir_of(spec)
    if os.path.isfile(path):
        return [path]
    excludes = set(spec.get("excludes", []))
    return [
        os.path.join(path, name)
        for name in sorted(os.listdir(path))
        if name not in excludes and name.endswith(".json")
    ]


def test_dataset_ids_are_contiguous():
    ids = sorted(spec["id"] for spec in CONFIG["datasets"].values())
    assert ids == list(range(len(ids))), f"dataset ids are not 0..N-1: {ids}"


def test_every_dataset_path_exists():
    missing = [
        f"{key} -> {_dir_of(spec)}"
        for key, spec in CONFIG["datasets"].items()
        if not os.path.exists(_dir_of(spec))
    ]
    assert not missing, "datasets point at paths that do not exist:\n" + "\n".join(missing)


def test_excludes_name_real_files():
    """Every ``excludes`` entry must name something actually in the directory.

    An entry that matches nothing is always a typo, and it fails silently:
    the file it was meant to skip gets read instead.
    """
    problems = []
    for key, spec in CONFIG["datasets"].items():
        directory = _dir_of(spec)
        if not os.path.isdir(directory):
            continue
        present = set(os.listdir(directory))
        for entry in spec.get("excludes", []):
            if entry not in present:
                problems.append(
                    f"{key}: excludes {entry!r}, not in {os.path.basename(directory)}/"
                )
    assert not problems, "excludes entries match no real file:\n" + "\n".join(problems)


def test_single_file_datasets_declare_no_excludes():
    offenders = [
        key
        for key, spec in CONFIG["datasets"].items()
        if os.path.isfile(_dir_of(spec)) and spec.get("excludes")
    ]
    assert not offenders, f"single-file datasets carry a meaningless excludes list: {offenders}"


@pytest.mark.parametrize("key", DATASET_KEYS)
def test_dataset_contributes_at_least_one_file(key):
    spec = CONFIG["datasets"][key]
    assert source_files(spec), f"{key}: excludes removed every file in {spec['path']}"


@pytest.mark.parametrize("key", DATASET_KEYS)
def test_declared_tag_is_reachable(key):
    """Every record in the dataset's smallest file must carry the declared ``tag``.

    This turns a config typo into a test failure instead of a ``KeyError`` at
    some later call site.
    """
    spec = CONFIG["datasets"][key]
    files = source_files(spec)
    if not files:
        pytest.skip(f"{key} contributes no files")

    smallest = min(files, key=os.path.getsize)
    with open(smallest, encoding="utf-8") as f:
        data = json.load(f)

    records = data if isinstance(data, list) else [data]
    tag = spec["tag"]
    for i, record in enumerate(records):
        assert isinstance(record, dict), (
            f"{key}: record {i} in {os.path.basename(smallest)} is {type(record).__name__}"
        )
        assert tag in record, (
            f"{key}: record {i} of {os.path.basename(smallest)} has no {tag!r} key "
            f"(keys: {sorted(record)})"
        )
