# Dead letters

A dead letter is a record a service publishes about a message it could not process. This page
says what reaches one, what the record holds, and how to read it with the `nats` command line.
The `cliffracer` command has no subcommand for dead letters.

## What reaches the dead-letter subject

Each service publishes to `dlq_subject` (default `dlq.{service}`, with the service's subject
prefix in front when it has one). Four things are dead-lettered:

| Cause | When | Record |
|---|---|---|
| The body cannot be decoded | any transport | `original_subject`, `payload: {raw}`, `error`, `cause: "decode"`, `service`, `deliveries`, `correlation_id` |
| A handler fails on its last delivery | JetStream only: the delivery limit is `jetstream_max_deliver`, or the server's own `max_deliver` when that is lower | `original_subject`, `payload`, `error`, `cause: "delivery-limit"`, `service`, `deliveries`, `delivery_limit`, `correlation_id` |
| The payload fails its schema and the listener's `on_invalid` (else `default_on_invalid`) is `deadletter` | any transport | `original_subject`, `payload`, `errors`, `cause: "invalid"`, `service`, `schema`, `correlation_id` |
| A `fails_closed` extension hook crashes (raises anything but `RejectMessage`) | JetStream only, on the last delivery, counted as for a handler that fails | `original_subject`, `payload`, `error` (`extension <name> failed: ...`), `cause: "delivery-limit"`, `service`, `deliveries`, `delivery_limit`, `correlation_id` |

Nothing else is dead-lettered. A refusal an extension authors (`RejectMessage`) is acknowledged,
and a `RetryMessage` is redelivered with a delay until the delivery limit. A `fails_closed` hook
that crashes is not a refusal: its message is redelivered, with a delay, and dead-lettered when
the deliveries run out, as a failing handler's is. The `error` of a hook crash carries the exception's text only
when `expose_internal_errors` is set, and otherwise says `internal error`. The `error` of a handler that fails on
its last delivery follows the same flag: with it off the record carries the exception's type (`RuntimeError`),
and `cliffracer-dlq show` prints that type; with it set, the exception's text. The dead-letter stream is
readable by whoever can read the stream, so it is outside the process, and the flag is "whether an exception's own
text may leave the process". A decode failure's `error` (`Decode error: ...`), the delivery limit and the sentences the
framework writes itself (a handler that overran `max_processing_time`, a missing `msgpack` package) are not
the text of a handler's exception and stay readable. A failure before the
handler is entered, while the payload is decoded and validated, is a failure of the message and
is dead-lettered on the first delivery whatever exception raised it. The exception is a body in an
encoding the service lacks a package to read, such as msgpack without the `msgpack` extra: that is
the service's fault and not the message's, so a JetStream delivery is redelivered like a handler
failure, and dead-lettered only at the delivery limit, while any other transport logs it as an error.

`payload` is the decoded value, written in the service's `serialization_format`. A body that
cannot be decoded is kept as text in `payload.raw`, with undecodable bytes replaced.

## What a record says about its delivery

A record for a JetStream delivery also carries:

| Field | Holds |
|---|---|
| `stream` | the stream the message was delivered from |
| `stream_sequence` | its sequence number in that stream |
| `consumer` | the consumer it was delivered to |
| `original_headers` | the headers it arrived with, except those that carry a credential |
| `withheld_headers` | the names of the headers left out of `original_headers` |

A record for a core NATS message has no stream, sequence or consumer, and omits those fields.
A header is withheld when its name is `Cookie` or `Set-Cookie`, contains `authorization`,
`token`, `secret`, `password`, `passwd`, `passphrase`, `credential`, `api-key`, `api_key`,
`apikey`, `access_key`, `private_key`, `signing_key`, `encryption_key`, `jwt`, `bearer` or
`session` (in any case, with `-`, `_` and `.` read as the same), or is the header an installed
extension reads a credential from. The log stream applies the same rule to the keys of a log
record. The values of withheld headers are never written. The rule is by name: a credential
carried under a name that matches none of these, by something that is not an installed
extension, is copied as it arrived.

When the stream, sequence and consumer are all known, the dead letter is published with the
`Nats-Msg-Id` `dlq:<service>:<stream>:<sequence>:<consumer>`. The stream stores a dead letter
published twice for one delivery once, within its duplicate window, and keeps the dead letters
that two consumers write for one stream message.

## Where the records are kept

With `jetstream_enabled`, a declared stream must cover the dead-letter subject, and the service
refuses to start without one. A catch-all such as `StreamSpec(name="DLQ", subjects=["dlq.*"])`
covers every service. The stream is the record: it is what survives a restart and what an
operator reads afterwards.

Without JetStream a dead letter is a core publish. Only a subscriber connected at that moment
receives it, and nothing can be read back later.

A dead letter that cannot be published is logged with its cause and the payload, counted in
`dead_letters_lost` on `/health` and `/ready`, and the delivery is terminated all the same.

## Reading them with `cliffracer-dlq`

The `cliffracer-dlq` distribution is a read-only command that lists dead letters, shows one in
full and counts them by service and cause, with filters by service, cause, time and original
subject:

```bash
cliffracer-dlq ls --service order_service --cause invalid --since 1h
cliffracer-dlq show 17
cliffracer-dlq count
```

It names a record's cause from its shape, so a message whose `fails_closed` hook crashed is
listed with the cause `delivery-limit`, the same as a handler that ran out of deliveries; its
`error` starts `extension <name> failed:`. It reads the stream's messages without creating a
consumer, and it writes nothing. Its
[README](../packages/cliffracer-dlq/README.md) has the options and the exit codes.

## Reading them with the `nats` command line

`broker_permissions` grants a service publish on its dead-letter subject and no subscribe on it,
so reading needs an operator identity with access to the stream. Substitute the stream's name
and the subject.

Find the stream that holds a service's dead letters:

```bash
nats stream ls --subject dlq.order_service
```

List its records, a page at a time (a page is 10 by default; the size, at most 25, follows the
stream name):

```bash
nats stream view DLQ --subject dlq.order_service
nats stream view DLQ --subject dlq.order_service --since 1h
nats stream view DLQ 25 --subject dlq.order_service
```

Show one record, by its sequence in the dead-letter stream, or the newest for a subject:

```bash
nats stream get DLQ 17
nats stream get DLQ --last-for dlq.order_service
```

`--translate "jq ."` passes a record's data through a command before it is printed. Records
are JSON unless the service sets `serialization_format="msgpack"`.

Find the message a record is about, while its stream still holds it, from the record's own
`stream` and `stream_sequence`:

```bash
nats stream get EVENTS 42
```

Watch dead letters as they are published, which is also the only way to see them without
JetStream:

```bash
nats sub "dlq.>"
```

Put the service's subject prefix in front of the subject when it has one, such as
`nats sub "staging.dlq.>"`.
