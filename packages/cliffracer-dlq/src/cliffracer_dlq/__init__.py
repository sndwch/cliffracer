"""A read-only inspector for the dead letters cliffracer services publish.

    cliffracer-dlq ls --service orders --cause invalid --since 1h
    cliffracer-dlq show 17
    cliffracer-dlq count

It reads the dead-letter stream with the stream's message-get API and writes nothing.
"""

from cliffracer_dlq.filters import Filters, parse_duration
from cliffracer_dlq.records import CAUSES, DeadLetter, classify

__all__ = ["CAUSES", "DeadLetter", "Filters", "classify", "parse_duration"]
