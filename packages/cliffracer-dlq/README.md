# cliffracer-dlq

A read-only inspector for the dead letters cliffracer services publish. It lists them, shows one
in full, and counts them by service and cause. It writes nothing: no replay, no delete, no
purge, and no consumer on the stream.

```bash
cliffracer-dlq ls --service orders --cause invalid --since 1h
cliffracer-dlq show 17
cliffracer-dlq count
```

[Dead letters](../../docs/dead-letters.md) says what a service publishes and what a record
holds.

## Reading

The inspector reads the dead-letter stream with the stream's message-get API. It creates no
consumer, acknowledges nothing and moves no cursor, so reading leaves nothing behind on the broker.
The identity it connects as needs `$JS.API.STREAM.INFO.<stream>` and
`$JS.API.STREAM.MSG.GET.<stream>`, plus `$JS.API.STREAM.NAMES` when it is left to find the stream,
and no permission to publish. It talks to the default JetStream API prefix; a domain or a custom
API prefix is not supported.

The stream is `--stream NAME`. Without it, the inspector asks the broker which streams hold
`--subject` (default `dlq.*`) and reads the one that does. The broker refuses two streams that
share a literal subject, so more than one match means the subject is a wildcard: `--subject 'dlq.*'`
over per-service streams such as `DLQ_ORDERS` and `DLQ_BILLING` matches both. The inspector then
exits 4 and names the streams, because reading one of them would count only part of the dead
letters; `--stream` chooses one. A service with a subject prefix publishes under it, so give the
subject with the prefix in front: `--subject "staging.dlq.*"`.

Connection options are the `nats` command line's: `--server` (`$NATS_URL`), `--creds`
(`$NATS_CREDS`), `--user` and `--password` (`$NATS_USER`, `$NATS_PASSWORD`), and `--token`
(`$NATS_TOKEN`). A password or a token belongs in the environment, where a process list does not
show it.

## Commands

`ls` lists records oldest first, one line each: sequence, time, service, cause, the subject the
message arrived on, deliveries and the first error. `--limit N` (default 50) bounds it, and
`--json` prints one object per line with the fields a script reads: `sequence`, `time`, `subject`,
`cause`, `service`, `original_subject`, `deliveries`, `error`, `stream`, `stream_sequence`,
`consumer` and `problem`.

`show SEQUENCE` prints one message in full: its headers and the decoded record, and, when the
record carries the original message's `stream` and `stream_sequence`, the command that reads that
message while the stream still holds it. `--json` prints the same as one object.

`count` counts by service and cause over the same filters, with a total.

## Filters

`ls` and `count` take `--service`, `--cause {decode,delivery-limit,invalid}`, `--since DURATION`
(`90s`, `15m`, `2h` or `1d3h5m2s`, against the time the stream stored the message) and
`--original-subject SUBJECT` (wildcards allowed). A message must satisfy every filter that is set.

The cause is read from the record's `cause` field: `decode`, `delivery-limit` or `invalid`. A
record published before that field existed is read from its shape: a list of `errors` is a message
that failed its schema; an `error` starting `Decode error:` is a body that could not be decoded;
any other record with `deliveries` is a handler that ran out of deliveries. Only such an older
record is read by the text of `error`.

A message that is not a dead-letter record, or cannot be decoded, is listed as `unreadable` with
the reason and never stops the listing. It satisfies none of the service, cause or
original-subject filters. A record published before the delivery fields existed shows `-` for
them.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | the command ran; a listing or a count of nothing is still 0 |
| 3 | no broker at that address, or a broker that refused the connection, a refused login among the reasons |
| 4 | no stream could be chosen: none holds the subject, several do (a wildcard) and none was named, or the one named is not there |
| 5 | `show` named a sequence the stream does not hold |
| 6 | the broker did not answer a stream request, or refused it: JetStream is off, or this identity may not read the stream |
| 7 | the command line is wrong, including a `--server` nats-py cannot dial: a list of servers, a port it cannot read |

A test reads the package's source and refuses anything that publishes, subscribes, creates,
changes or removes, including an alias or a literal `getattr` of one; its one request to the broker
that is not a stream read, the stream-names lookup, is allowed by name. The check cannot see a name
assembled at run time in other ways or what the libraries it imports do; counting a stream's
consumers and messages before and after a command is the check for those.

JSON records need nothing extra. A msgpack record needs the `msgpack` extra of `cliffracer`.
