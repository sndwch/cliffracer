"""StreamSpec declaration shape, and which subjects a set of declarations covers."""

import pytest
from nats.js.api import StreamConfig
from pydantic import ValidationError

from cliffracer import ServiceConfig
from cliffracer.core.jetstream import StreamSpec, subject_covered_by


@pytest.mark.unit
class TestStreamSpec:
    def test_minimal_spec_has_file_storage_and_limits_retention(self):
        spec = StreamSpec(name="EXTRACTION", subjects=["*.events.extraction.*"])
        assert spec.storage == "file"
        assert spec.retention == "limits"
        assert spec.max_age_seconds is None

    def test_empty_subject_list_is_rejected(self):
        # A stream claiming nothing is always a mistake, never a default.
        with pytest.raises(ValidationError):
            StreamSpec(name="EMPTY", subjects=[])

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

    def test_matches_is_true_for_an_equivalent_server_config(self):
        spec = StreamSpec(name="X", subjects=["a.b", "a.c"])
        assert spec.matches(StreamSpec(name="X", subjects=["a.c", "a.b"]).to_stream_config())

    def test_matches_is_false_when_subjects_differ(self):
        spec = StreamSpec(name="X", subjects=["a.b"])
        assert not spec.matches(StreamSpec(name="X", subjects=["a.b", "a.c"]).to_stream_config())

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
        )
        assert spec.matches(wire_config)

    def test_matches_is_false_against_a_differing_wire_shape(self):
        spec = StreamSpec(name="X", subjects=["a.b"], storage="file")
        wire_config = StreamConfig(
            name="X",
            subjects=["a.b"],
            storage="memory",
            retention="limits",
        )
        assert not spec.matches(wire_config)


@pytest.mark.unit
class TestSubjectCoveredBy:
    SPECS = [
        StreamSpec(name="EXTRACTION", subjects=["*.events.extraction.*"]),
        StreamSpec(name="UTILS_DLQ", subjects=["utils.dlq.*"]),
    ]

    def test_covered_by_a_wildcard_claim(self):
        assert subject_covered_by(self.SPECS, "utils.events.extraction.requested")

    def test_covered_by_a_narrow_claim(self):
        assert subject_covered_by(self.SPECS, "utils.dlq.pdf-extractor")

    def test_uncovered_subject(self):
        assert not subject_covered_by(self.SPECS, "utils.events.upload.received")

    def test_a_namespaced_dlq_is_not_covered_by_an_unnamespaced_claim(self):
        # The trap this whole check exists for: dlq_subject goes through
        # publish_event, which namespaces it.
        specs = [StreamSpec(name="DLQ", subjects=["dlq.*"])]
        assert not subject_covered_by(specs, "utils.dlq.pdf-extractor")
        assert subject_covered_by(specs, "dlq.pdf-extractor")

    def test_no_specs_covers_nothing(self):
        assert not subject_covered_by([], "anything.at.all")


@pytest.mark.unit
class TestServiceConfigJetStreamFields:
    def test_defaults_are_inert(self):
        cfg = ServiceConfig(name="svc")
        assert cfg.jetstream_enabled is False
        assert cfg.jetstream_streams == []
        assert cfg.jetstream_update_streams is False

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


@pytest.mark.unit
class TestDlqCoverageAssertion:
    """A dead-letter queue that can silently drop messages is not one.

    dlq_subject is published through publish_event, which namespaces it, so
    the assertion must check the namespaced form. An assertion against the raw
    template would pass while the runtime publish still failed — a check that
    certifies the exact failure it exists to prevent.
    """

    def _svc(self, **overrides):
        from cliffracer import CliffracerService

        svc = CliffracerService(ServiceConfig(name="pdf-extractor", **overrides))
        svc.js = object()  # non-None: pretend JetStream is live
        return svc

    def test_namespaced_dlq_is_covered_by_root_claim(self):
        """A namespaced service DLQ matches root 'dlq.*' streams."""
        svc = self._svc(
            namespace="utils",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="DLQ", subjects=["dlq.*"])],
        )
        svc.container._assert_dlq_covered()  # must not raise

    def test_namespaced_claim_is_accepted(self):
        svc = self._svc(
            namespace="utils",
            dlq_subject="{namespace}.dlq.{service}",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="UTILS_DLQ", subjects=["utils.dlq.*"])],
        )
        svc.container._assert_dlq_covered()  # must not raise

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
