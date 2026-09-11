"""Subject pattern matching, and the pattern-vs-pattern overlap rule stream ownership turns on."""

import pytest

from cliffracer.core.subjects import subject_matches, subjects_overlap


@pytest.mark.unit
class TestSubjectMatches:
    """Behaviour moved verbatim out of CliffracerService._subject_matches."""

    def test_literal_match(self):
        assert subject_matches("orders.created", "orders.created")

    def test_literal_mismatch(self):
        assert not subject_matches("orders.created", "orders.updated")

    def test_star_matches_one_token(self):
        assert subject_matches("orders.*", "orders.created")

    def test_star_does_not_match_two_tokens(self):
        assert not subject_matches("orders.*", "orders.created.v2")

    def test_gt_matches_remainder(self):
        assert subject_matches("orders.>", "orders.created.v2")

    def test_token_count_mismatch_without_gt(self):
        assert not subject_matches("a.b", "a.b.c")

    def test_gt_requires_at_least_one_more_token(self):
        # Wildcard '>' must consume >= 1 token; it does not match bare prefix
        assert not subject_matches("a.>", "a")
        assert not subject_matches("*.>", "a")
        assert not subject_matches("a.b.>", "a.b")
        assert not subject_matches("a.b.>", "a")
        assert not subject_matches("a.*.>", "a.b")

    def test_gt_matches_one_or_more_tokens(self):
        assert subject_matches("a.>", "a.b")
        assert subject_matches("a.>", "a.b.c")
        assert subject_matches(">", "a")
        assert subject_matches(">", "a.b")
        assert subject_matches("a.*.>", "a.b.c")
        assert subject_matches("a.*.>", "a.b.c.d")


@pytest.mark.unit
class TestSubjectsOverlap:
    """Two streams may not claim the same subject, so declarations are compared pattern to pattern."""

    def test_identical_patterns_overlap(self):
        assert subjects_overlap("*.events.extraction.*", "*.events.extraction.*")

    def test_gt_swallows_a_wildcard_pattern(self):
        # This is the jorbo case: a natural-looking jorbo.events.> would collide
        # with the extractor's EXTRACTION stream.
        assert subjects_overlap("*.events.extraction.*", "jorbo.events.>")

    def test_differing_literal_token_does_not_overlap(self):
        assert not subjects_overlap("*.events.extraction.*", "jorbo.events.user.*")

    def test_per_service_dlq_claims_do_not_overlap(self):
        assert not subjects_overlap("utils.dlq.*", "jorbo.dlq.*")

    def test_a_shared_dlq_claim_overlaps_every_per_service_one(self):
        assert subjects_overlap("*.dlq.*", "jorbo.dlq.*")
        assert subjects_overlap("*.dlq.*", "utils.dlq.*")

    def test_star_matches_any_single_token(self):
        assert subjects_overlap("a.*.c", "a.b.c")

    def test_shorter_pattern_does_not_overlap_longer(self):
        assert not subjects_overlap("a.b", "a.b.c")

    def test_gt_needs_at_least_one_more_token(self):
        # "a.>" requires a token after "a", so it cannot overlap the bare subject "a".
        assert not subjects_overlap("a.>", "a")

    def test_gt_overlaps_when_a_token_follows(self):
        assert subjects_overlap("a.>", "a.b.c")
