"""StreamSpec declaration shape, and which subjects a set of declarations covers."""

import pytest
from nats.js.api import StreamConfig
from pydantic import ValidationError

from cliffracer import ServiceConfig
from cliffracer.core.jetstream import StreamSpec, subject_covered_by

pytestmark = pytest.mark.unit


class TestStreamSpec:
    def test_minimal_spec_has_file_storage_and_limits_retention(self):
        spec = StreamSpec(name="EXTRACTION", subjects=["utils.events.extraction.*"])
        assert spec.storage == "file"
        assert spec.retention == "limits"
        assert spec.max_age_seconds is None

    def test_empty_subject_list_is_rejected(self):
        # A stream claiming nothing is always a mistake, never a default.
        with pytest.raises(ValidationError):
            StreamSpec(name="EMPTY", subjects=[])

    @pytest.mark.parametrize(
        "subject",
        ["*.events.x.*", "*.x", "*.*", "*.>", ">"],
    )
    def test_a_subject_that_begins_with_a_wildcard_is_refused(self, subject):
        # nats-server refuses these with err_code 10052, because the wildcard can
        # match $JS. Declaring one is a startup failure with the wrong words.
        with pytest.raises(ValidationError) as exc:
            StreamSpec(name="X", subjects=["ok.events.*", subject])

        assert repr(subject) in str(exc.value)
        assert "10052" in str(exc.value)

    @pytest.mark.parametrize(
        "subject",
        ["*", "a.*.>", "a.>", "a.*", "jorbo.events.extraction.*"],
    )
    def test_a_wildcard_after_the_first_token_is_accepted(self, subject):
        # A lone `*` has no second token to overlap `$JS.<something>`, and the
        # server accepts it.
        assert StreamSpec(name="X", subjects=[subject]).subjects == [subject]

    def test_unknown_storage_is_rejected(self):
        with pytest.raises(ValidationError):
            StreamSpec(name="X", subjects=["a.b"], storage="tape")

    def test_to_stream_config_carries_the_declaration(self):
        spec = StreamSpec(name="X", subjects=["a.b"], storage="memory", max_age_seconds=60.0)
        cfg = spec.to_stream_config()
        assert cfg.name == "X"
        assert cfg.subjects == ["a.b"]
        assert cfg.storage.value == "memory"
        assert cfg.max_age == 60.0

    @pytest.mark.parametrize("retention", ["limits", "interest", "workqueue"])
    def test_to_stream_config_carries_each_retention_and_the_duplicate_window(self, retention):
        """A stream provisioned with the wrong retention is the config that decides: a workqueue
        built as limits never removes a message on ack, and nothing but disk growth says so."""
        spec = StreamSpec(
            name="X", subjects=["a.b"], retention=retention, duplicate_window_seconds=30.0
        )

        cfg = spec.to_stream_config()

        assert cfg.retention.value == retention
        assert cfg.duplicate_window == 30.0

    @pytest.mark.parametrize("retention", ["limits", "interest", "workqueue"])
    def test_apply_to_carries_the_declared_retention_over_the_brokers(self, retention):
        broker = StreamSpec(name="X", subjects=["a.b"], retention="limits").to_stream_config()
        spec = StreamSpec(name="X", subjects=["a.b"], retention=retention)

        assert spec.apply_to(broker).retention.value == retention

    def test_unknown_retention_is_rejected(self):
        with pytest.raises(ValidationError):
            StreamSpec(name="X", subjects=["a.b"], retention="forever")

    def test_matches_is_true_for_an_equivalent_server_config(self):
        spec = StreamSpec(name="X", subjects=["a.b", "a.c"])
        assert spec.matches_declared_fields(
            StreamSpec(name="X", subjects=["a.c", "a.b"]).to_stream_config()
        )

    def test_matches_is_false_when_subjects_differ(self):
        spec = StreamSpec(name="X", subjects=["a.b"])
        assert not spec.matches_declared_fields(
            StreamSpec(name="X", subjects=["a.b", "a.c"]).to_stream_config()
        )

    def test_matches_is_true_against_the_wire_shape(self):
        """A real ``js.streams_info()`` round trip hands back plain strings for
        storage/retention, not the StorageType/RetentionPolicy enums that a
        locally built config carries. matches() must accept both shapes —
        this is the exact comparison that runs whenever a stream a service
        declares already exists, which is the normal case for two services
        sharing one stream."""
        spec = StreamSpec(name="X", subjects=["a.b", "a.c"])
        wire_config = StreamConfig(
            name="X",
            subjects=["a.c", "a.b"],
            storage="file",
            retention="limits",
            duplicate_window=120.0,
        )
        assert spec.matches_declared_fields(wire_config)

    def test_matches_is_false_against_a_differing_wire_shape(self):
        spec = StreamSpec(name="X", subjects=["a.b"], storage="file")
        wire_config = StreamConfig(
            name="X",
            subjects=["a.b"],
            storage="memory",
            retention="limits",
        )
        assert not spec.matches_declared_fields(wire_config)

    def _wire(self, duplicate_window):
        return StreamConfig(
            name="X",
            subjects=["a.b"],
            storage="file",
            retention="limits",
            duplicate_window=duplicate_window,
        )

    def test_a_zero_window_declaration_matches_the_servers_default_window(self):
        """NATS applies its 120-second default when a declaration sends zero and reports 120 on
        the stored stream, so a spec that declares 0 is not drift against it. 0 here means "the
        server's default", not "no deduplication": the server has no way to switch it off."""
        spec = StreamSpec(name="X", subjects=["a.b"], duplicate_window_seconds=0.0)

        assert spec.matches_declared_fields(self._wire(120.0))

    @pytest.mark.parametrize("reported", [0, None])
    def test_a_server_that_reports_its_default_as_zero_or_omits_it_matches_the_default(
        self, reported
    ):
        """Older or partial wire shapes report the default window as 0 or leave it out."""
        spec = StreamSpec(name="X", subjects=["a.b"])

        assert spec.matches_declared_fields(self._wire(reported))

    @pytest.mark.parametrize("declared,server", [(300.0, 120.0), (120.0, 300.0), (0.0, 300.0)])
    def test_a_window_that_is_not_the_servers_default_is_drift(self, declared, server):
        """The normalisation covers the default and only the default: a genuinely different window
        on either side is reported, including a zero declaration against a stream tuned to 300."""
        spec = StreamSpec(name="X", subjects=["a.b"], duplicate_window_seconds=declared)

        assert not spec.matches_declared_fields(self._wire(server))
        assert spec.declared_differences(self._wire(server))[0][0] == "duplicate_window_seconds"


class TestSubjectCoveredBy:
    SPECS = [
        StreamSpec(name="EXTRACTION", subjects=["utils.events.extraction.*"]),
        StreamSpec(name="UTILS_DLQ", subjects=["utils.dlq.*"]),
    ]

    def test_covered_by_a_wildcard_claim(self):
        assert subject_covered_by(self.SPECS, "utils.events.extraction.requested")

    def test_covered_by_a_narrow_claim(self):
        assert subject_covered_by(self.SPECS, "utils.dlq.pdf-extractor")

    def test_uncovered_subject(self):
        assert not subject_covered_by(self.SPECS, "utils.events.upload.received")

    def test_a_namespaced_dlq_is_not_covered_by_an_unnamespaced_claim(self):
        # `subject_covered_by` is a pure matcher: a claim of `dlq.*` does not cover a subject that
        # is prefixed with a namespace. Whether a service's DLQ subject is prefixed is decided by
        # its `dlq_subject` template (the default has no `{namespace}`), and `publish_dlq`
        # publishes the formatted subject verbatim.
        specs = [StreamSpec(name="DLQ", subjects=["dlq.*"])]
        assert not subject_covered_by(specs, "utils.dlq.pdf-extractor")
        assert subject_covered_by(specs, "dlq.pdf-extractor")

    def test_no_specs_covers_nothing(self):
        assert not subject_covered_by([], "anything.at.all")


class TestServiceConfigJetStreamFields:
    def test_defaults_are_inert(self):
        cfg = ServiceConfig(name="svc")
        assert cfg.jetstream_enabled is False
        assert cfg.jetstream_resource_mode == "provision"
        assert cfg.jetstream_streams == []
        assert cfg.jetstream_update_streams is False

    def test_bind_mode_cannot_claim_it_will_update_operator_owned_streams(self):
        with pytest.raises(ValidationError, match="jetstream_update_streams"):
            ServiceConfig(
                name="shipments",
                jetstream_resource_mode="bind",
                jetstream_update_streams=True,
            )

    def test_tuning_defaults(self):
        cfg = ServiceConfig(name="svc")
        assert cfg.jetstream_max_deliver == 5
        assert cfg.jetstream_ack_wait == 30.0
        assert cfg.jetstream_max_ack_pending == 64
        assert cfg.jetstream_nak_backoff == 1.0
        assert cfg.jetstream_max_backoff == 60.0

    def test_streams_accept_stream_specs(self):
        cfg = ServiceConfig(
            name="svc",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="X", subjects=["a.b"])],
        )
        assert cfg.jetstream_streams[0].name == "X"


class TestDlqCoverageAssertion:
    """A dead-letter queue that can silently drop messages is not one.

    The subject checked is the one the service really publishes to: the
    `dlq_subject` template formatted with the service name and namespace, which
    `publish_dlq` publishes verbatim. The default template, `dlq.{service}`, has no
    `{namespace}`, so the default subject is the same for every namespace and a
    root claim covers it; a template that names `{namespace}` is covered only by a
    claim that does. An assertion against the raw template would pass while the
    runtime publish still failed.
    """

    def _svc(self, **overrides):
        from cliffracer import CliffracerService

        return CliffracerService(ServiceConfig(name="pdf-extractor", **overrides))

    def test_namespaced_dlq_is_covered_by_root_claim(self):
        """A namespaced service's default DLQ subject carries no namespace, so root 'dlq.*' covers it."""
        svc = self._svc(
            namespace="utils",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
        )
        # What makes this about the namespace: it is set, and the subject has none in it.
        assert svc.config.namespace == "utils"
        assert svc.container._format_dlq_subject() == "dlq.pdf-extractor"
        svc.container._assert_dlq_covered()  # must not raise

    def test_namespaced_claim_is_accepted(self):
        svc = self._svc(
            namespace="utils",
            dlq_subject="{namespace}.dlq.{service}",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="UTILS_DLQ", subjects=["utils.dlq.*"])],
        )
        svc.container._assert_dlq_covered()  # must not raise

    def test_the_coverage_check_reads_config_and_not_a_live_connection(self):
        """`_assert_dlq_covered` is gated on `jetstream_enabled`, not on a connected JetStream
        context: with no `js` at all it still refuses an uncovered dead-letter subject. The
        publish side differs, and is gated on the live context; the second half pins it."""
        from cliffracer.core.jetstream import StreamDeclarationError

        svc = self._svc(jetstream_enabled=True)
        assert svc.container.js is None

        with pytest.raises(StreamDeclarationError):
            svc.container._assert_dlq_covered()

    async def test_publish_dlq_without_a_live_jetstream_goes_to_core_nats_unchecked(self):
        from unittest.mock import AsyncMock

        svc = self._svc(jetstream_enabled=True)
        svc.container.nc = AsyncMock()
        assert svc.container.js is None

        await svc.container._publish_dlq("dlq.pdf-extractor", payload={"k": 1}, error="boom")

        svc.container.nc.publish.assert_awaited_once()
        assert svc.container.nc.publish.await_args.args[0] == "dlq.pdf-extractor"

    def test_no_streams_at_all_fails(self):
        from cliffracer.core.jetstream import StreamDeclarationError

        svc = self._svc(jetstream_enabled=True)
        with pytest.raises(StreamDeclarationError):
            svc.container._assert_dlq_covered()

    def test_unnamespaced_service_is_covered_by_a_plain_claim(self):
        svc = self._svc(
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
        )
        svc.container._assert_dlq_covered()  # must not raise

    def test_dlq_bare_subject_not_covered_by_gt_claim(self):
        """A dlq_subject 'wtdlq' is not covered by 'wtdlq.>' because '>' requires >= 1 token."""
        from cliffracer.core.jetstream import StreamDeclarationError

        svc = self._svc(
            jetstream_enabled=True,
            dlq_subject="wtdlq",
            jetstream_streams=[StreamSpec(name="DLQ", subjects=["wtdlq.>"])],
        )
        with pytest.raises(StreamDeclarationError):
            svc.container._assert_dlq_covered()
