"""Behavioral azsync regression tests; see azsync_support for fixtures."""

from __future__ import annotations
import json
import os
import time
from pathlib import Path
import pytest
import azsync
from usm_publish import PublishError, snapshot_unit
from usm_azure import (
    SasManager,
    SasToken,
)
from azsync import (
    NETWORK,
    OK,
    PARTIAL,
    AzcopyEngine,
    PublishCoordinator,
    Supervisor,
)

from azsync_support import (
    FakePublishEngine,
    FilesystemPublishEngine,
    _Clock,
    _StubProvider,
    fail_step,
    invoke,
    make_checkpoint,
    make_job,
    ok_step,
    publish_job,
    sas_for,
)


class TestPublishArgv:
    def test_payload_uses_copy_not_sync(self, tmp_path):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        snap, _ = snapshot_unit(Path(job.source), root, job.publish_policy())
        argv = AzcopyEngine(job, binary="azcopy").build_publish_argv(
            snap, "sig=x", marker=".complete"
        )
        assert argv[1] == "copy"

    def test_payload_is_limited_to_the_checkpoint(self, tmp_path):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        snap, _ = snapshot_unit(Path(job.source), root, job.publish_policy())
        argv = AzcopyEngine(job, binary="azcopy").build_publish_argv(
            snap, None, marker=".complete"
        )
        assert argv[2] == str(root)
        assert argv[3] == job.dest + "/checkpoints/checkpoint-100"
        assert "--as-subdir=false" in argv
        assert "--include-path" not in argv

    def test_directory_payload_excludes_marker(self, tmp_path):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        snap, _ = snapshot_unit(Path(job.source), root, job.publish_policy())
        argv = AzcopyEngine(job, binary="azcopy").build_publish_argv(
            snap, None, marker=".complete"
        )
        assert argv[argv.index("--exclude-pattern") + 1] == ".complete"

    def test_file_payload_excludes_its_sidecar_marker(self, tmp_path):
        job = publish_job(
            tmp_path,
            publish_unit="file",
            publish_patterns=["*.ckpt"],
        )
        path = Path(job.source) / "checkpoints" / "model.ckpt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        path.with_name("model.ckpt.complete").touch()
        snap, _ = snapshot_unit(Path(job.source), path, job.publish_policy())
        argv = AzcopyEngine(job, binary="azcopy").build_publish_argv(
            snap, None, marker=".complete"
        )
        assert argv[2] == str(path)
        assert "--recursive" not in argv and "--include-path" not in argv

    def test_md5_mode_stores_md5(self, tmp_path):
        job = publish_job(tmp_path, publish_verify="md5")
        root = make_checkpoint(job)
        snap, _ = snapshot_unit(Path(job.source), root, job.publish_policy())
        assert "--put-md5" in AzcopyEngine(job, binary="azcopy").build_publish_argv(
            snap, None, marker=".complete"
        )

    def test_exact_copy_url_encodes_every_segment(self, tmp_path):
        job = publish_job(tmp_path)
        argv = AzcopyEngine(job, binary="azcopy").build_exact_copy_argv(
            tmp_path / "marker", "checkpoints/a b/完成", "sig=x"
        )
        assert "a%20b/%E5%AE%8C%E6%88%90" in argv[3]
        assert "sig=x" in argv[3]

    def test_exact_copy_never_uses_recursive(self, tmp_path):
        job = publish_job(tmp_path)
        argv = AzcopyEngine(job, binary="azcopy").build_exact_copy_argv(
            tmp_path / "marker", "x/.complete", None
        )
        assert "--recursive" not in argv

    def test_payload_dry_run(self, tmp_path):
        job = publish_job(tmp_path)
        root = make_checkpoint(job)
        snap, _ = snapshot_unit(Path(job.source), root, job.publish_policy())
        assert "--dry-run" in AzcopyEngine(job, binary="azcopy").build_publish_argv(
            snap, None, marker=".complete", dry_run=True
        )


class TestRemoteMarkerProbe:
    def _completed(self, argv, code=0, stdout="", stderr=""):
        return azsync.subprocess.CompletedProcess(argv, code, stdout, stderr)

    def test_existing_marker(self, tmp_path, monkeypatch):
        job = publish_job(tmp_path)
        monkeypatch.setattr(
            azsync.subprocess,
            "run",
            lambda argv, **kw: self._completed(
                argv,
                stdout=json.dumps(
                    {
                        "MessageType": "ListObject",
                        "MessageContent": json.dumps(
                            {"Path": "", "ContentLength": "0"}
                        ),
                    }
                ),
            ),
        )
        assert AzcopyEngine(job, binary="azcopy").remote_exists(
            "checkpoints/one/.complete", None
        )

    def test_empty_success_means_absent(self, tmp_path, monkeypatch):
        job = publish_job(tmp_path)
        monkeypatch.setattr(
            azsync.subprocess,
            "run",
            lambda argv, **kw: self._completed(argv),
        )
        assert not AzcopyEngine(job, binary="azcopy").remote_exists(
            "checkpoints/one/.complete", None
        )

    @pytest.mark.parametrize("kind", ["Info", "Init", "ListSummary"])
    def test_banner_is_not_evidence_of_a_marker(self, tmp_path, monkeypatch, kind):
        job = publish_job(tmp_path)
        monkeypatch.setattr(
            azsync.subprocess,
            "run",
            lambda argv, **kw: self._completed(
                argv,
                stdout=json.dumps({"MessageType": kind, "MessageContent": "ready"}),
            ),
        )
        assert not AzcopyEngine(job, binary="azcopy").remote_exists("x/.complete", None)

    @pytest.mark.parametrize("text", ["not JSON", "[]", '{"MessageType":"ListObject"}'])
    def test_malformed_probe_output_fails_closed(self, tmp_path, monkeypatch, text):
        job = publish_job(tmp_path)
        monkeypatch.setattr(
            azsync.subprocess,
            "run",
            lambda argv, **kw: self._completed(argv, stdout=text),
        )
        with pytest.raises(PublishError):
            AzcopyEngine(job, binary="azcopy").remote_exists("x/.complete", None)

    @pytest.mark.parametrize(
        "message",
        [
            "BlobNotFound",
            "blob not found",
            "the specified blob does not exist",
            "statuscode=404",
            "status code: 404",
        ],
    )
    def test_missing_marker_shapes(self, tmp_path, monkeypatch, message):
        job = publish_job(tmp_path)
        monkeypatch.setattr(
            azsync.subprocess,
            "run",
            lambda argv, **kw: self._completed(argv, 1, stderr=message),
        )
        assert not AzcopyEngine(job, binary="azcopy").remote_exists(
            "checkpoints/one/.complete", "sig=x"
        )

    def test_auth_or_network_failure_is_not_treated_as_absent(
        self, tmp_path, monkeypatch
    ):
        job = publish_job(tmp_path)
        monkeypatch.setattr(
            azsync.subprocess,
            "run",
            lambda argv, **kw: self._completed(argv, 1, stderr="authentication failed"),
        )
        with pytest.raises(PublishError, match="authentication"):
            AzcopyEngine(job, binary="azcopy").remote_exists(
                "checkpoints/one/.complete", None
            )

    def test_sas_is_redacted_from_probe_failure(self, tmp_path, monkeypatch):
        job = publish_job(tmp_path)
        secret = "super-secret-signature"
        monkeypatch.setattr(
            azsync.subprocess,
            "run",
            lambda argv, **kw: self._completed(
                argv, 1, stderr=f"auth failed sig={secret}"
            ),
        )
        with pytest.raises(PublishError) as raised:
            AzcopyEngine(job, binary="azcopy").remote_exists(
                "checkpoints/one/.complete", f"sig={secret}"
            )
        assert secret not in str(raised.value)

    def test_probe_uses_machine_readable_json(self, tmp_path, monkeypatch):
        job = publish_job(tmp_path)
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return self._completed(argv)

        monkeypatch.setattr(azsync.subprocess, "run", run)
        AzcopyEngine(job, binary="azcopy").remote_exists(
            "checkpoints/one/.complete", None
        )
        assert "--machine-readable" in calls[0]
        assert "--output-type=json" in calls[0]


class TestPublishCliValidation:
    def test_publish_and_delete_destination_are_incompatible(
        self, tmp_path, state_dir, runner
    ):
        src = tmp_path / "src"
        src.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(src),
                "https://acct.blob.core.windows.net/bucket",
                "--publish-path",
                "checkpoints",
                "--delete",
                "--no-start",
            ],
        )
        assert result.exit_code != 0
        assert "cannot be combined" in result.output

    def test_publish_path_cannot_escape_source(self, tmp_path, state_dir, runner):
        src = tmp_path / "src"
        src.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(src),
                "https://acct.blob.core.windows.net/bucket",
                "--publish-path",
                "../outside",
                "--no-start",
            ],
        )
        assert result.exit_code != 0 and "publish path" in result.output

    def test_options_are_persisted(self, tmp_path, state_dir, runner):
        src = tmp_path / "src"
        src.mkdir()
        result = invoke(
            runner,
            [
                "add",
                str(src),
                "https://acct.blob.core.windows.net/bucket",
                "--publish-path",
                "checkpoints",
                "--publish-pattern",
                "checkpoint-*",
                "--ready-marker",
                "DONE",
                "--publish-stable",
                "60",
                "--after-publish",
                "delete",
                "--publish-keep-last",
                "3",
                "--no-start",
            ],
        )
        assert result.exit_code == 0, result.output
        job = azsync.list_jobs()[0]
        assert job.publish_paths == ["checkpoints"]
        assert job.publish_patterns == ["checkpoint-*"]
        assert job.ready_marker == "DONE"
        assert job.after_publish == "delete"
        assert job.publish_keep_last == 3

    def test_old_job_without_publish_fields_loads(self, state_dir):
        (state_dir / "old.json").write_text(
            json.dumps(
                {
                    "id": "old",
                    "source": "/tmp/source",
                    "dest": "https://acct.blob.core.windows.net/bucket",
                }
            )
        )
        job = azsync.load_job("old")
        assert not job.publish_policy().enabled


class TestPublishScheduling:
    def test_scan_registers_the_end_of_the_stability_window(self, tmp_path, state_dir):
        job = publish_job(tmp_path, publish_stable=60)
        root = make_checkpoint(job)
        old = time.time() - 120
        for path in root.rglob("*"):
            if path.is_file():
                os.utime(path, (old, old))
        (root / ".complete").touch()
        engine = FakePublishEngine(job)
        coordinator = PublishCoordinator(
            job,
            engine,
            ledger_path=state_dir / "ledger.json",
            clock=time.time,
        )
        coordinator.scan()
        assert coordinator.next_wake is not None
        assert 55 <= coordinator.next_wake - time.time() <= 61

    def test_tick_wakes_at_publish_deadline(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        publisher = type(
            "Publisher",
            (),
            {"next_wake": 105.0, "run": lambda self, token: azsync.PublishRun()},
        )()
        clock = _Clock(100)
        sup = Supervisor(
            job,
            publisher=publisher,
            clock=clock,
            log=lambda _m: None,
        )
        sup.state.last_sync_end = 100
        decision = sup.tick()
        assert not decision.should_sync and decision.wake_at == 130

    def test_tick_runs_only_publish_when_checkpoint_becomes_ready(
        self, tmp_path, state_dir, monkeypatch
    ):
        job = publish_job(tmp_path)
        publisher = type(
            "Publisher",
            (),
            {"next_wake": 99.0, "run": lambda self, token: azsync.PublishRun()},
        )()
        clock = _Clock(100)
        sup = Supervisor(
            job,
            publisher=publisher,
            clock=clock,
            log=lambda _m: None,
        )
        sup.state.last_sync_end = 60
        reasons = []
        monkeypatch.setattr(
            sup,
            "run_sync",
            lambda reason, event=None: reasons.append(reason) or azsync.PublishRun(),
        )
        decision = sup.tick()
        assert decision.should_sync
        assert reasons == ["checkpoint ready"]

    def test_publisher_obeys_its_own_backoff(self, tmp_path, state_dir):
        job = publish_job(tmp_path)
        publisher = type("Publisher", (), {"next_wake": 99.0})()
        clock = _Clock(100)
        sup = Supervisor(
            job,
            publisher=publisher,
            clock=clock,
            log=lambda _m: None,
        )
        sup.state.last_result = NETWORK
        sup.state.backoff_until = 200
        decision = sup.tick()
        assert not decision.should_sync
        assert decision.reason == "backoff"
        assert decision.wake_at == 200


class TestPublishWithRealAzcopyEngine:
    def test_expired_marker_probe_refreshes_sas_without_normal_sync(
        self, tmp_path, state_dir, fake_azcopy
    ):
        job = publish_job(tmp_path)
        make_checkpoint(job)
        fake_azcopy.program(
            fail_step("Signature not valid in the specified time frame"),
            fail_step("BlobNotFound"),
            ok_step(completed=2, size=9),
            ok_step(completed=1, size=100),
            ok_step(completed=1, size=0),
        )
        provider = _StubProvider([sas_for(7200), sas_for(7200)])
        manager = SasManager(provider, tmp_path / "cache.sas")
        sup = Supervisor(
            job,
            engine=AzcopyEngine(job, state_dir=tmp_path / "work"),
            sas=manager,
            log=lambda _: None,
        )
        result = sup.run_sync("auth refresh")
        assert result.status == OK and result.published == 1
        assert provider.calls == 2
        assert [call[0] for call in fake_azcopy.calls] == [
            "list",
            "list",
            "copy",
            "copy",
            "copy",
        ]

    def test_publisher_runs_probe_payload_manifest_marker_without_sync(
        self, tmp_path, state_dir, fake_azcopy
    ):
        job = publish_job(tmp_path)
        make_checkpoint(job)
        azsync.save_job(job)
        fake_azcopy.program(
            fail_step("BlobNotFound"),  # marker probe
            ok_step(completed=2, size=9),  # payload
            ok_step(completed=1, size=200),  # manifest
            ok_step(completed=1, size=0),  # marker
        )
        now = time.time()
        manager = SasManager(
            _StubProvider([sas_for(7200, now=now)]),
            tmp_path / "cache.sas",
        )
        engine = AzcopyEngine(job, state_dir=tmp_path / "wd")
        publisher = PublishCoordinator(
            job,
            engine,
            ledger_path=state_dir / "ledger.json",
            clock=time.time,
            log=lambda _m: None,
        )
        sup = Supervisor(
            job,
            engine=engine,
            sas=manager,
            publisher=publisher,
            clock=time.time,
            log=lambda _m: None,
        )
        result = sup.run_sync("manual")
        assert result.status == OK
        assert [call[0] for call in fake_azcopy.calls] == [
            "list",
            "copy",
            "copy",
            "copy",
        ]
        assert fake_azcopy.calls[-1][2].split("?", 1)[0].endswith("/.complete")
        assert sup.state.publish_last_path == "checkpoints/checkpoint-100"

    def test_publish_failure_is_its_own_result_and_never_deletes(
        self, tmp_path, state_dir, fake_azcopy
    ):
        job = publish_job(tmp_path, after_publish="delete")
        root = make_checkpoint(job)
        fake_azcopy.program(
            fail_step("BlobNotFound"),
            fail_step("connection reset"),
        )
        now = time.time()
        manager = SasManager(
            _StubProvider([sas_for(7200, now=now)]),
            tmp_path / "cache.sas",
        )
        engine = AzcopyEngine(job, state_dir=tmp_path / "wd")
        publisher = PublishCoordinator(
            job,
            engine,
            ledger_path=state_dir / "ledger.json",
            clock=time.time,
        )
        sup = Supervisor(
            job,
            engine=engine,
            sas=manager,
            publisher=publisher,
            clock=time.time,
            log=lambda _m: None,
        )
        result = sup.run_sync("manual")
        assert result.status == NETWORK
        assert isinstance(result, azsync.PublishRun)
        assert result.status == NETWORK
        assert root.exists()
        assert [call[0] for call in fake_azcopy.calls] == [
            "list",
            "copy",
        ]

    def test_active_log_job_and_checkpoint_publisher_are_structurally_independent(
        self, tmp_path, state_dir, fake_azcopy
    ):
        """Regression: a partial log sync starved a 454GB checkpoint."""
        source = tmp_path / "output"
        normal = make_job(
            tmp_path,
            id="logs",
            source=source,
            excludes=["checkpoints/"],
        )
        publisher_job = publish_job(
            tmp_path,
            id="checkpoints",
            source=source,
        )
        make_checkpoint(publisher_job)
        fake_azcopy.program(
            fail_step(
                "train.log changed while reading",
                TransfersCompleted=3,
                TransfersFailed=1,
                TotalBytesTransferred=100,
            ),
            fail_step("BlobNotFound"),
            ok_step(completed=2, size=9),
            ok_step(completed=1, size=300),
            ok_step(completed=1, size=0),
        )
        now = time.time()
        manager = SasManager(
            _StubProvider([sas_for(7200, now=now)]),
            tmp_path / "cache.sas",
        )
        normal_engine = AzcopyEngine(normal, state_dir=tmp_path / "normal-wd")
        normal_sup = Supervisor(
            normal,
            engine=normal_engine,
            sas=manager,
            clock=time.time,
            log=lambda _m: None,
        )
        retain = normal_sup.run_sync("active logs")
        assert retain.status == PARTIAL

        publisher_engine = AzcopyEngine(
            publisher_job, state_dir=tmp_path / "publisher-wd"
        )
        publisher = PublishCoordinator(
            publisher_job,
            publisher_engine,
            ledger_path=state_dir / "ledger.json",
            clock=time.time,
            log=lambda _m: None,
        )
        sup = Supervisor(
            publisher_job,
            engine=publisher_engine,
            sas=manager,
            publisher=publisher,
            clock=time.time,
            log=lambda _m: None,
        )
        result = sup.run_sync("publisher")
        assert isinstance(result, azsync.PublishRun)
        assert result.status == OK
        assert result.published == 1
        assert [call[0] for call in fake_azcopy.calls] == [
            "sync",
            "list",
            "copy",
            "copy",
            "copy",
        ]
        assert fake_azcopy.calls[-1][2].split("?", 1)[0].endswith("/.complete")


class TestPublishFilesystemRoundTrip:
    TOKEN = SasToken("", None)

    def test_marker_is_last_and_delete_is_after_marker(self, tmp_path, state_dir):
        job = publish_job(tmp_path, after_publish="delete")
        local = make_checkpoint(job)
        remote = tmp_path / "remote"
        engine = FilesystemPublishEngine(job, remote)
        coordinator = PublishCoordinator(
            job,
            engine,
            ledger_path=state_dir / "ledger.json",
            clock=time.time,
        )
        result = coordinator.run(self.TOKEN)
        published = remote / "checkpoints" / "checkpoint-100"
        assert engine.phases == ["probe", "payload", "manifest", "marker"]
        assert (published / "model.bin").read_bytes() == b"weights"
        assert json.loads((published / ".azsync-manifest.json").read_text())[
            "fingerprint"
        ]
        assert (published / ".complete").exists()
        assert not local.exists()
        assert result.published == 1 and result.deleted == 1

    def test_keep_leaves_identical_local_and_remote_payload(self, tmp_path, state_dir):
        job = publish_job(tmp_path, after_publish="keep")
        local = make_checkpoint(job)
        remote = tmp_path / "remote"
        result = PublishCoordinator(
            job,
            FilesystemPublishEngine(job, remote),
            ledger_path=state_dir / "ledger.json",
            clock=time.time,
        ).run(self.TOKEN)
        published = remote / "checkpoints" / "checkpoint-100"
        assert (published / "model.bin").read_bytes() == (
            local / "model.bin"
        ).read_bytes()
        assert result.published == 1 and result.deleted == 0
