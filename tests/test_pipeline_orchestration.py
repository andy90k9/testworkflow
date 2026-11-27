# -*- coding: utf-8 -*-
"""编排层单元测试：manifest、锁、脱敏与 run_id 路径。"""
import sys
from pathlib import Path

WORKFLOW_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKFLOW_ROOT / "scripts"))

from dsh_common import (  # noqa: E402
    acquire_pipeline_lock, release_pipeline_lock, canonical_hash,
    mask_url, scope_manifest_path,
)


def test_canonical_hash_is_order_insensitive():
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_mask_url_hides_query_and_userinfo():
    masked = mask_url("https://user:pass@example.test/api?token=abc&page=1")
    assert "user:pass" not in masked
    assert "token=abc" not in masked
    assert "page=1" not in masked
    assert "***" in masked


def test_pipeline_lock_blocks_duplicate_and_releases(tmp_path):
    lock = tmp_path / ".pipeline.lock"
    ok, info = acquire_pipeline_lock(lock, "run-test")
    assert ok is True
    assert info["run_id"] == "run-test"

    blocked, current = acquire_pipeline_lock(lock, "run-other")
    assert blocked is False
    assert current["run_id"] == "run-test"

    release_pipeline_lock(lock, "run-other")
    assert lock.exists()

    release_pipeline_lock(lock, "run-test")
    assert not lock.exists()


def test_scope_manifest_path_uses_records_root():
    cfg = {"records_dir": "../dsh-workflow-tmp/records", "cases": {"selection": {}}}
    path = scope_manifest_path(cfg, "run-test")
    assert path.name == "scope-manifest.json"
    assert path.parent.name == "run-test"
    assert "dsh-workflow-tmp" in str(path)
