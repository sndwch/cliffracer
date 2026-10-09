# Changelog

What an upgrader needs, newest first. Entries for the next release are fragments
in `changelog.d/`, written into a section here when that release is prepared.

Every release also gets a note on the
[releases page](https://github.com/sndwch/cliffracer/releases).
The release job writes it there from the commit messages and `BREAKING CHANGE:`
footers in that release's range, so the full commit-level history lives there
rather than here.

<!-- version list -->

## 1.2.0
- **Fix**: `publish_event` and `broadcast_message` write each model in the payload in the form its class reads back as itself, as `call_rpc` does. They wrote every model by alias, so a listener whose model reads by field name only (`validate_by_alias=False`, `validate_by_name=True`, an aliased field) refused the event, which was dead-lettered or dropped, on core NATS and on JetStream push and pull consumers alike. The alias form is still written first, so an event a listener read is sent byte for byte as it was; a model its own class does not read back by alias is written by field name, and a tree whose levels need different forms is written a level at a time. A model inside a list, tuple, set or dict is handled the same way. When no model class of a payload model's hierarchy reads the form that would be sent and its own class would read a field the caller set as anything other than what its validators make of the caller's value, both raise `RpcValidationError` before publishing, naming the fields, where the event was delivered wrong. The idempotency key and what a send hook is handed are unchanged. Choosing the form validates each model in the payload on the publishing side, so a model's validators run there too: once per publish for a model whose own class reads the alias form, three times for one that needs the field-name form, where they ran not at all. Measured in process with the connection mocked (no broker round trip, a 20-core host at load 6 to 9, median of 5 to 7 runs, two runs agreeing), per publish, before and after: one three-field model 24 µs and 32 µs when its own class reads the alias form, 24 µs and 62 µs when it needs the field-name form; one model holding a list of 200 such models 140 µs and 393 µs when they are read by alias, 143 µs and 2.1 ms when they need the field-name form; a payload that is itself a list of 200 such models, each chosen on its own, 184 µs and 677 µs by alias, 203 µs and 6.6 ms by field name; a payload with no model 23 µs and 28 µs. Of the field-name cases, 30% to 65% of the time is the check that a decoded value holds only what JSON carries (`_is_json_in_form`), by cProfile and by the reviewer's measurement. In the check before sending, a NaN read back is the NaN sent, and a value a model's serializer writes is taken as the field's value only when the serializer writes it alike by alias and by field name.
- **Breaking**: `put()`, `create()` and `put_object()` store a Pydantic model in a form its own class reads back as the same model, its field names or its aliases, chosen as the RPC client chooses a form, and refuse a model that reads back as other values from both with `ModelDoesNotReadBackError`, a `KvError` and a `TypeError` naming the model and what each form read back as; nothing is written. A model was stored under its field names whenever its class accepted them, and `get(as_type=...)` returned other values with no error: a field read only through an `AliasChoices` or `AliasPath` naming other keys came back as its default, two fields whose aliases are each other's field names came back swapped, and a `field_serializer` that changes the value came back changed. The crossed-alias model is now stored under its aliases and reads back equal; the others are refused, as are a tree whose parent reads only field names and whose child only aliases, and a strict model with a `before` validator whose dump strict JSON mode refuses, which `get` refused. "Reads back as the model" is judged by declared type over what a dump writes: a value at a typed position must read back as the value it held, NaN as NaN; a value at a position declared `Any` or `object`, at any depth (a field, a `list[Any]` or `tuple[Any, ...]` item, a `dict[str, Any]` value, an `Optional` or union arm; also through a type alias such as `type Payload = dict[str, Any]`, or as a `TypeVar` with no bound), and every extra, must read back with the same JSON form, so a `datetime` held there is stored as before; a `TypeVar` with a bound is judged as its bound and one with constraints as their union, and a value in a union is judged under every arm it is an instance of, so by its JSON form only where each such arm promises no more; private attributes and `exclude=True` fields are not stored and not compared. A base class that declares the model's fields is a reader too: the form stored is one every such class reads back as the model where one exists, else the form earlier releases stored when the model's own class reads it back, so every reader reads what it read before, else one no base reads worse than that; else the write is refused. A model that reads back as itself is stored with the bytes it had, unless a base class it shares its fields with reads another form right and the stored one wrong. To store a model lossily on purpose, store `model_dump_json()` or a dict. The check is paid on each write of a model and never on a read: measured on one 20-core host under light load, a model with five union fields took 105 µs per `put` where it took 12 µs, and one whose five union fields hold 200 entries each took 6.8 ms where it took 1.4 ms. See the upgrade guide.
- **Breaking**: `ServiceClient` and a generated client offer the same forms of a model argument as `call_rpc`, the dump by field name included, so a `serialize_by_alias` model with a `serialization_alias` that the handler reads by field name arrives with its value; it arrived as the field's default, and a required field was refused. And when no form of a model reads back as the argument, and the one that would be sent makes the service read a field the caller set as anything other than what the model's own validators make of the caller's value (its default, another field's value, or a value an inner alias reshapes), every client path now raises `RpcValidationError` before sending, naming the field in a `value_would_be_lost` or `value_would_be_misread` entry of `details`, where the value was dropped, crossed or changed without a trace. An `AliasChoices` whose first member is another field's name, an `AliasPath` whose head is another field's name, a chain of aliases each naming the next field (`a` arrived holding `b`'s value), and a nested model whose alias is another field's name are the shapes that do this; give such a field an alias that is no other field's name (see the upgrade guide). A model whose validator normalises a value is sent as before. A generated client also chooses among a model argument's forms by the parameter's declared class, not the argument's own class, so a subclass passed where a base is declared goes out in a form the base reads where one exists.
- **Fix**: A model whose field is read through a `validation_alias` (`AliasChoices` or `AliasPath`) arrives at an RPC handler with the value passed. `ServiceClient`, a generated client, `call_rpc`, `call_async`, `call_rpc_no_wait` and `RpcProxy` wrote the field under its name or serialization alias, which such a model does not read, so the handler got the field's default (`x=5` arrived as `x=0`, with no error) and a required field was refused. Each model is now also offered in a form with each such field moved under its first `AliasChoices` member or into the structure its `AliasPath` names, after the forms that were sent before. A generated client sends that form only where the declared annotation reads it back as the argument, and otherwise sends what it sent before. `call_rpc` and the others, which do not know the handler's class, withhold it where a model class the value is an instance of would read it as other values, or reads an earlier form instead, so a handler declaring any of those classes either reads it as the argument or refuses it. A handler declaring a base class that reads the field by name keeps getting the form it reads.
- **Behaviour change**: A KV or object-store write refuses pydantic's generic `Secret[...]` as it refuses a `SecretStr` or `SecretBytes`, and a secret used as a dict key, in a model or a plain dict, with the `TypeError` that names its place (`Pin.pin`, `Keyed.by_secret key`) and points to `get_secret_value()`. A generic secret was stored as its mask `**********`, and a secret key in a model was stored as the mask; one in a plain dict was refused as a value with no JSON form, naming no secret.
- **Fix**: `redact_nats_url` keeps a server that carries no credentials when it is listed in front of one that does. `nats://h1:4222,nats://u:p@h2:4222` was printed as `nats://***@h2:4222`, which dropped `h1` from the service's start and connection lines, the generator's messages and the `repr` of a `BrokerUrl`; it is now `nats://h1:4222,nats://***@h2:4222`, in any order of servers with and without credentials. A piece in front of a credentialed server is read as a server of its own only when it is exactly a scheme, a host and a numeric port; anything else (`nats://u:secret`, a server with a path, query or fragment, one without a port or a scheme) is still read as possibly part of a password and withheld with the server after it, so only the last host is printed for it. A comma followed by whitespace and a scheme separates servers as a comma does: in a list written `a, b`, a password or a `?token=` after the comma-space was printed, and each server is redacted on its own now. A comma after a server's host, where its text runs on into the next server's user and password, ends what is printed: `nats://a@h1:4222,usr:pw,nats://b@h2:4222` is `nats://***@h1:4222,***,nats://***@h2:4222`. A password that is itself written as a host and a port with a comma and a scheme in it (`u:1234,nats://…`) is the one shape read as a server and printed.
- **Breaking**: a handler parameter that declares a Pydantic alias (`Annotated[int, Field(alias="itemId")]`, `validation_alias`, `serialization_alias`) is refused when the service discovers its handlers at start, before it connects, for `@rpc` handlers and event listeners, and by `describe`: the service fails to start with `UntypedHandler: Svc.take: parameter 'item' declares alias='itemId', ...` naming the handler and the parameter. It was described under its Python name (`item`) and accepted only under the alias (`itemId`), so a generated client or `RpcProxy` call built from the description was refused with `missing`. A service that declares such a parameter has to remove the alias before it starts; a handler parameter is passed by its Python name.
- **Fix**: `normalize` accepts settings that pydantic reads back through a dataclass it shares between uses. When one stdlib dataclass is used three or more times in a model, pydantic builds one definition for it under the config of its first use and reads the other uses through it, so a use under a model that configures aliases differently is read under another config than the model's schema shows; a document pydantic writes and reads back was refused with "must round-trip through an accepted serialized key". Where the check is about to refuse a field, it now validates the document again with a different value under a key of the object that holds the field, and accepts the field under that key only if the change moves that field and no other, the changed document dumps back with the change at that same key and at no other, and the field set alone to its new value on the original model dumps the change there too, which shows the field is carried by the key. A key pydantic does not read, a field a validator fills from another field's key or from an extra key, a value no different one can replace, or a document the model refuses once changed is still refused, and so is any field once the check has spent its budget of copies and validations, which are weighted by the bytes of the document so the cost is bounded whatever its size; a large document is refused as before (a shared dataclass of 300 fields, or a 30-field one beside a 1 MB string).
- **Fix**: A `RejectMessage` or `RetryMessage` subclass whose `__init__` does not call `super().__init__` is read the same way on every arm: the synchronous request, describe, fire-and-forget, event, JetStream and timer arms. `hook_crash` is a class attribute of `RejectMessage` that defaults to `False`, and `retry_after` one of `RetryMessage` that defaults to `None`, so such a refusal is an authored refusal and such a retry asks for the configured backoff. Before, the request and describe arms answered it as `refused`, while the fire-and-forget, event and timer arms raised `AttributeError: 'Sub' object has no attribute 'hook_crash'` out of dispatch, and a JetStream listener NAKed it as a handler failure, so it was redelivered. The event and JetStream log lines name a refusal by its text, so they do not read `reason`, which such a subclass does not set either.
- **Behaviour change**: A KV or object-store write refuses a `SecretStr` or `SecretBytes` held by a dataclass, standard or pydantic, at any depth, held as an extra of a model that allows them, or returned by a computed field, with the `TypeError` that names its place (`Login.pw`, `list[0].pw`) and points to `get_secret_value()`. Each was stored with the mask `**********` in place of the secret, with no error. A pydantic dataclass field declared `exclude=True` is not part of the dump and is not affected. `KvExtension` refuses a `buckets` or `object_stores` that is neither a declaration nor a collection of them, with a `BucketConfigError` naming the option when it is built: a number, where it raised a bare `TypeError`, and a `bytes`, `bytearray` or `memoryview`, which was read as the declarations of its byte values. A single name is still one declaration.
- **Fix**: `cliffracer-dlq` refuses a `--server` that nats-py cannot dial before it dials, as `ServiceConfig` refuses such a `nats_url`: a comma-separated list of servers, a port that is not a number from 1 to 65535, or a password holding a `/`. It is a usage error, exit 7, `--server h1:4222,h2:4222 is not a URL nats-py can dial: it contains a second '://' (one server per URL, no list)`, naming the address without its credentials. A list was dialled and nats-py refused it unread, which the CLI reported as a broker that refused the connection, exit 3, and `nats://h1:4222, nats://h2:4222` ended in a `ValueError` traceback, exit 1. The address in every message is read from each server's redacted form and never raises; a single URL is dialled and named as before.
- **Fix**: on Python 3.12 an extension argument that is a C callable with no signature of its own, such as `operator.itemgetter("id")`, `attrgetter`, `methodcaller` or a `sqlite3.Connection`, is copied for each service instance, as on 3.13. Python 3.12 reports such a callable's signature as exactly `(*args, **kwargs)`, which binds no arguments, so it was called at service build time as a zero-argument factory and the build failed with `ExtensionIsolationError`. That signature, on a callable with no Python code behind it, now counts as unreadable and the callable is not a factory; Python functions, lambdas, partials and Python callables are read as before.
- **Fix**: A model tree whose levels need different alias settings is accepted by the service. An outer model read by field name (`validate_by_alias=False`) that holds an inner model read by alias has no whole-value form the service accepts, and `ServiceClient`, a generated client, `call_rpc` and `RpcProxy` sent it by one form and had it refused. It is now written one model at a time, each level in the form its own class reads back, after the two whole-value forms, so a tree they serve is sent exactly as before. A tree is still sent whole, and refused, when a model on the way declares a `field_serializer`, a `model_serializer` or a `computed_field` or carries extra fields; `populate_by_name=True` on the model read by alias lets the tree be written in one form. The limit is stated in the API reference.
- **Behaviour change**: a typed `@listener` with several parameters receives each one as the type it declares. A model parameter such as `item: Item` arrived as a plain `dict` (keyed by field name even where the model aliases the field), because the validated arguments were dumped back to data before the handler was called. A listener now gets each validated parameter as it is, a model as the model, models inside a list, a dict or an optional as models, and a `Message` parameter with the event's `correlation_id` filled, as an RPC handler does. A handler that read a model parameter as a `dict`, `item["name"]`, gets `TypeError` and reads `item.name` instead.
- **Behaviour change**: a `@validated_listener` reads the form `publish_event(topic, message=Schema(...))` sends, by the rule a typed listener with one model parameter reads it. The payload `{"message": {...}}` is read as the `Schema` when no field of the schema is named or aliased like the handler's parameter (by its alias or a validation alias) and the payload holds that one key, apart from the event's `correlation_id`, with an object under it; any other payload, a `RootModel`'s included, is read as the schema itself, as before. Before, a schema with a required field or `extra="forbid"` refused the nested payload and it was dead-lettered or dropped, and a schema whose fields all have defaults read it as an empty model, so the handler received the defaults and the published values were lost. That handler now receives the published values. A schema whose fields all have defaults and that keeps extra keys (`extra="allow"`), sent a lone `{"message": {...}}`, is read as the nested form too, where it received itself with `message` as an extra key.
- **Behaviour change**: a listener whose only parameter is a model, `on(self, item: Item)`, reads the form `publish_event(topic, item=Item(...))` sends. The payload `{"item": {...}}` is read as the `Item` when no field of the model is named or aliased `item` (by its alias or a validation alias) and the payload holds that one key, apart from the event's `correlation_id`, with an object under it; any other payload, a `RootModel`'s included, is read as the model itself, as before. Before, a model with a required field or `extra="forbid"` refused the nested payload and it was dead-lettered, and a model whose fields all have defaults read it as an empty model, so the listener received the defaults and the published values were dropped without a trace. That listener now receives the published values. A model whose fields all have defaults and that keeps extra keys (`extra="allow"`), sent a lone `{"item": {...}}`, is read as the nested form too, where it received itself with `item` as an extra key.
- **Fix**: `call_rpc`, `call_async`, `call_rpc_no_wait` and `RpcProxy` send each model argument in the form its class reads back as that argument. They wrote a model by alias, so a model the service reads by field name (`validate_by_alias=False`, or a `serialization_alias` that differs from its `validation_alias`) was refused as invalid, a model that `ServiceClient` already sent correctly. The alias form is still tried first, so a call the service accepted is sent byte for byte as it was; when the model does not read the alias form back as the argument, the field-name form is sent, and when it reads back as neither, the first form it accepts. A model inside a list, tuple, set or dict is handled the same way. Because a call has no annotation to say which class the handler declares, a base class of the instance's class that reads the alias form as the argument and the field-name form not keeps the alias form (a base that reads both forms, has no fields or has only defaulted fields does not decide), so a subclass passed to a handler that declares its alias-only base is sent as before. A populatable base (one that reads either form) with a subclass read by field name now sends field names instead of aliases, a form both read. Each form is read as the service reads a message, python mode then JSON mode, so a strict model read by field name is sent in a form it accepts. A handler that declares a subclass read by field name whose base reads only by alias is still refused, and so is the mirror case: a subclass whose own class reads the alias form sent to a handler that declares a base read only by field name. A value that holds a NaN is read back as itself in no form, so no base decides and the instance's own class reads: a by-name subclass holding a NaN, sent to a handler that declares its alias-only base, is now refused where it was delivered. Events, broadcasts and replies are not changed.
- **Breaking**: cliffracer and cliffracer-kv require pydantic 2.11.0 or later; they declared 2.0.0. On pydantic 2.5, `normalize()` of any service template raised `AttributeError: 'FieldInfo' object has no attribute 'init'`. On 2.6 to 2.10 the library imports and runs, but parts of it behave differently from 2.11: template settings, output bindings and the handling of `validate_by_alias` and `validate_by_name`, which arrived in 2.11, fail tests of the broker-free suite. 2.11.0 is the lowest version that suite passes on, and a repository guard holds the declared floor to a version recorded as verified. An installation that pinned pydantic below 2.11 now fails to resolve instead of failing at run time.
- **Behaviour change**: a service whose handler takes a model declared strict, or a strict field, refused the JSON form of its own value: a payload was validated in pydantic's python mode, which refuses an ISO string for a `datetime`, text for a `UUID` or a `Decimal` and an array for a tuple or a set, the forms every sender writes over JSON and over msgpack. A payload python mode refuses, and that is JSON in form (no `bytes`, no map with a key that is not text), is now validated once more in JSON mode, which accepts those forms and still refuses what strict is for (`"1"` for an int). A payload python mode accepts is accepted exactly as before, with the same value, so a lax model and a msgpack producer that sends python values such as `bytes` accept what they did. When both modes refuse, the error raised is JSON mode's, for a model that is not strict too, at the same location: a `list`, `tuple`, `set` or `frozenset` field's refusal says "valid array" where it said "valid list" (tuple, set, frozenset), a `dict` or nested model's says "an object" where it said "a valid dictionary", and a `timedelta`'s says "valid duration" where it said "valid timedelta". The error type changes for a whole number of magnitude `10**18` or more given for a `date`, `datetime` or `timedelta`: `date_type`, `datetime_type` or `time_delta_type` where it was `date_from_datetime_parsing`, `datetime_parsing` or `time_delta_parsing`. Both reach a client in the details of its `RpcValidationError`. A strict model with a `before` or `wrap` validator still refuses its own JSON dump, as pydantic's JSON mode does. `ServiceClient` reads what it sends the same way, through one helper shared with the service, so a strict model that only its alias reads is sent by its alias and a JSON-form dict for a strict model is not refused before sending.
- **Behaviour change**: a generated client writes the keys of a dict default in sorted order, at every depth. A client generated from the class (`--class`) kept the order the dict was declared in, and one generated from a running service (`--service`) got it sorted, because `describe` is written with sorted keys. So `cliffracer-generate-client --check`, which compares bytes, could call a client stale depending only on which way it was generated, and the two forms did not produce the same bytes the docs say they do. A client already generated for a service with a dict default whose keys are not in sorted order is reported by `--check` as stale once; regenerating it clears that.
- **Fix**: A `ServiceClient` (and a generated client) sends a model argument in the form the service reads as that argument. A model that can only be read by its alias (an `alias` without `populate_by_name`, an `alias_generator` without it, such a model nested in another or in a list) was sent by field name and refused by the service as invalid; it is now sent by alias. A model whose fields' aliases are each other's field names was sent by field name, accepted, and read with its values swapped; it is now sent by alias and read as passed. The argument is dumped as before first, and the alias form is used only when the service would not read the first dump back as the argument, so a call the service read correctly is sent byte for byte as it was (models with `populate_by_name`, `validate_by_alias=False`, a `serialization_alias` that differs from the `validation_alias`, or a validator or serializer that is not idempotent included). When neither form reads back as the argument (a validator or serializer that is not idempotent changes it in every form), the first form the service accepts is sent, so those calls go out as they did. Only calls that were refused or read wrongly change.
- **Breaking**: a client generated for a service writes the default of a model-typed parameter as `Model.model_validate({...}, strict=False)`, and the default of a list or dict of models with each member built the same way, where it wrote the dict the description carries. The old form ran and was a type error for the caller (`mypy --strict`: `Incompatible default for argument`), and `inspect.signature` showed a dict where the annotation promised the model. The service decides which defaults can be built: its description now carries `rebuildable` on each parameter with a default that holds a model, true only when the service rebuilt the default's dump with its own models and got its own value back, dumping to the same JSON. That adds a key to those parameters, so the signature hash of a method with such a parameter, and the description hash, change. A default that is not rebuildable (aliases that swap, a union the dump cannot tell apart, a serializer that changes a type, a masked secret) stays the dict, as does any default in a description from an older service. `strict=False` because the value is the service's JSON dump, which a model declared strict does not accept as it stands. The instance is built once, when the client module is imported, and every call that leaves the argument out passes that one object; the wire value is unchanged. A client already generated is not rewritten: it raises `ClientOutOfDate`, naming those methods, on its first call against a service that carries the key, and `cliffracer-generate-client --check` reports it as stale, until it is regenerated with `cliffracer-generate-client`. The hash moves in both directions: a client generated against a service that carries the key raises `ClientOutOfDate` against an older service that does not, so upgrade the service and regenerate its clients together.
- **Fix**: `normalize` accepts settings that hold a pydantic dataclass whose config has an `alias_generator` and a multi-word field. A field such as `box_width` written as `boxWidth` was refused with "must round-trip through an accepted serialized key", because the check read the key from the field's info, and a pydantic dataclass applies its `alias_generator` in its validation schema and leaves the info without an alias. The check now reads a pydantic dataclass's aliases from its schema, as it does for a stdlib dataclass.
- **Fix**: `normalize` accepts settings that pydantic reads and writes back, and refuses a lossy list serializer with a `TemplateError`. A stdlib dataclass used by two settings models that configure aliases differently was checked against the aliases of whichever model's copy the check found first in the root model's schema, so a document the other model reads was refused with "must round-trip through an accepted serialized key"; the check now reads a dataclass's aliases from the nearest model or pydantic dataclass that holds it, wherever the models sit and however deep. A field serializer that returns more or fewer items than the list holds raised a bare `ValueError` from `zip`, so a caller that catches `TemplateError` around `normalize` missed it.
- **Behaviour change**: `LoggingConfig.configure("beta", replace_existing=False)` keeps the process-wide `service` that an earlier `configure` or `setup_correlation_logging` named, and logs one WARNING naming both services, as `setup_correlation_logging(replace_existing=False)` does. It overwrote the name silently, so after `configure("alpha")` the plain logger's lines carried `alpha` in a text or JSON sink, and after the second `configure` they carried `beta`, while `setup_correlation_logging("beta", replace_existing=False)` left them `alpha`: which service a plain-logger line named depended on which of the two functions a service called. A `configure` with the default `replace_existing=True` sets the name as before, and a `configure(replace_existing=False)` with no earlier service names it, as before. A service that added its sinks next to another's with `configure(replace_existing=False)` and relied on its own name appearing on the plain logger's lines sees the first service's name instead; a line bound with `logger.bind(service=...)` or written through `get_service_logger` is unaffected.
- **Fix**: an event listener declared on a service class has its signature read once, not on every message. The dispatcher kept the parsed signature as an attribute of the handler, which a bound method, the form every such listener is registered in, does not accept, so the assignment failed unnoticed and the signature was parsed again for each event. It is kept in a table on the dispatcher now.
- **Behaviour change**: a line nothing bound a service to is no longer published by the NATS log sink of the last-configured service. `LoggingConfig.configure` and `setup_correlation_logging` store the service name in loguru's process-wide `extra`, which is merged into every record, and the sink published the records whose `service` was its own, so in 1.1.0, after `configure("alpha")` and `configure("beta", replace_existing=False)`, a host's `logger.info("...")` was published to `logs.beta.info` as beta's line, though beta never logged it. The stored name is now an instance of a private `str` subclass that formats and serialises as the name (the JSON and text sinks carry `service` on unbound lines as before), and the sink publishes a record only when its `service` is its name and a call bound it: a line from the host with no `service` binding is published under no service, whatever was configured last. A consumer of `logs.<service>.<level>` stops receiving the host application's own unbound lines under the last-configured service. Lines bound with `logger.bind(service=...)`, `get_service_logger` and the extensions' own lines are streamed as before. A host that stores its own `service` with `logger.configure(extra={"service": ...})` stores a plain string, which is still read as a binding. The framework lines that had a service in hand are bound to it, so they are still streamed: a timer's `Error executing timer method` (a method that raised and a gate that crashed) and its `Stopping timer ... from its own handler`; the local supervisor's activation lines (bound to its host service); and a single-service runner's lifecycle lines (`Starting service`, `Service crashed`, `Restarting service`, `NATS connection closed unexpectedly`, the stop-after-cancel and refused-overlay lines), bound to the service it runs. The framework lines with no service to name are no longer streamed to anyone, where the stamp had sent them to the last-configured service's stream: the correlation helpers' warnings (`Rejected invalid correlation ID ...`), `ServiceClient`'s debug and warning lines, `KvRateLimiter`'s backend lines, `BatchProcessor`'s lines, `SimpleAuthService`'s lines, the warning that a timed-out dependency probe finished late, the loop host's `did not stop after cancellation` error, and the multi-service orchestrator's and the CLI's lines, which belong to no one service. They are still written to the console and the log files. An application that wants one in the stream binds it with `logger.bind(service=...)`.
- **Breaking**: a distributed cron job refuses an option it cannot run with, with
  `ConfigurationError`, where the job is declared (`@cron(..., distributed=True)` or
  `DistributedCronTimer(...)`), as `Timer` and `CronTimer` do for their own. A `lease_ttl` that is
  not a finite number of seconds above zero (0, negative, `nan`, `inf`, a bool, a string) is refused:
  a `lease_ttl` of 0 or less made `no_overlap` skip nothing, and `nan` or a string failed at every
  firing. A `no_overlap` that is not a bool and a `bucket` the Key-Value layer would refuse (an empty
  name, `a.b`, a name with a space) are refused: a bad bucket name failed at the first firing.
- **Fix**: the cron documentation says what each setting does. `lease_ttl` is how long a running lease is honoured before another run starts over it; the lease and the interval records are keys in the bucket and live the bucket's TTL (`max(lease_ttl, 300)` seconds for a bucket the timer creates), where `docs/api-reference.md` and the `@cron` docstring said `lease_ttl` was how long a lease and its record live. The bucket is opened when the timer starts, which is what `docs/decisions.md` now says (it said when the first firing needs it). The warning a job logs when the wall clock stepped forward past occurrences says "this replica does not run those occurrences", and `docs/api-reference.md` says the same, since another replica of a distributed job whose clock did not step runs them.
- **Fix**: `cliffracer-generate-client` dials through `cliffracer.core.dial.connect`, as the service connection, `ServiceClient` and the metrics pool do. A dial that its `--timeout` cut off left the client it had opened, reconnecting, to the garbage collector; it is closed at the cut now. A guard fails any code in the core or a package that calls `nats.connect`.
- **Behaviour change**: the `RpcTimeoutError` of a `ServiceClient` request says what the broker reported while that request waited, when it reported a permissions violation: a role confined to an inbox prefix that is opened without `inbox_prefix=` cannot subscribe to its reply inbox, and the request timed out with no reason, as if the service were down. The message now ends with the broker's text, which names the subject it refused, and for a subscription violation a hint at `inbox_prefix=`. A publish violation is attached only to the request whose subject it names, a subscription violation to every request waiting, and one reported before the request began is not attached. The exception class is unchanged, so a handler of `RpcTimeoutError` still matches; a timeout with nothing reported reads as before. The broker reports a refusal once, so a later request on the same connection times out without the reason.
- **Fix**: `cliffracer-generate-client` refuses a service name, a namespace or, when it asks a running service for its description, a `$CLIFFRACER_SUBJECT_PREFIX` that cannot be part of a subject, each under its own flag or variable and with exit 7. A `--service` with white space was refused only when `--namespace` was also given, and then blamed `--namespace`; alone it sent a request on a malformed subject and exited 2 with "no service answered" after the timeout. A bad environment prefix built a subject nothing serves and gave the same wrong answer after the timeout. A class described in process with `--class` sends nothing and does not read the prefix.
- **Breaking**: a `ServiceConfig` does not print the password embedded in its `nats_url`, and `model_dump_json()` no longer carries it. Its `repr` and `str`, a printed `model_dump()`, `__dict__` and `dict(config)` show the URL without the user and password (`nats://***@broker:4222`), and `model_dump_json()` carries the URL with them withheld, as it carries the other credentials. `config.nats_url` is the whole URL and still an instance of `str`, but it is a `str` subclass whose `repr` is redacted, so `type(config.nats_url) is str` is false; a dial, a comparison, `str()` and an f-string use the real URL, and a Python dump still round-trips and overlays it. A config copied with `model_copy(update={"nats_url": ...})` holds the string it was given, as it does for the other credentials. A config persisted with `model_dump_json()` no longer carries the URL's user and password, so one reloaded with `model_validate_json()` dials with the mask in their place: nats-py sends `***` as the token, a broker that authenticates refuses the connection, and one that does not authenticate accepts it. Supply the credentials again when a config is reloaded, or persist a Python dump (`model_dump()`), which holds them whole. A JSON dump masks `nats_password` and `nats_token` too, so a reload holds the mask for them as well.
- **Behaviour change**: a child a `LocalSupervisor` activates stops inside its cleanup budget. Its stop runs up to four phases one after another (a timer run's grace, the task drain, the cancellation grace and the connection drain) and gave each half the budget, so a stop that used the task drain, the cancellation grace and the connection drain, as a task that refused to stop on a silent broker does, took one and a half times the budget and the supervisor recorded the activation `UNFINISHED` while the child was still closing. Each phase now gets a fifth of the smaller of the host and template `cleanup_timeout` (6 s each at the default 30 s, 15 s before), so a child's handler that needs more than 6 s to stop is cut off where it was not: raise `cleanup_timeout` when a handler needs longer than that to stop.
- **Behaviour change**: the `error` of a dead letter for a handler that failed on its last delivery is the exception's type (`RuntimeError`) unless `expose_internal_errors` is set, and the exception's text only then. The record carried the text whichever the flag said, so anyone who can read the dead-letter stream, `cliffracer-dlq` included, could read a DSN or a password that an exception held, though the flag is "whether an exception's own text may leave the process" and already governs the wire, the health endpoint and the cron record. A reader of the stream, and `cliffracer-dlq show`, stops seeing the text and sees the type; set `expose_internal_errors=True` to have the text in the stream as before. A decode failure (`Decode error: ...`), the reason of a crashed gate (`extension <name> failed: internal error`), the sentences the framework writes itself (a handler that overran `max_processing_time`, a missing `msgpack` package) and the delivery limit are unchanged, as are the service's own logs.
- **Behaviour change**: a gate that crashes is a fault on a timer firing and on a fire-and-forget request, as it already was for RPC, events, describe and metrics. A timer firing stopped by a `fails_closed` extension that raised counts in `error_count` (so the error rate moves) with the crash in `last_error`, and is logged at ERROR with its traceback; it was a refusal, counted in `refusal_count`, logged at WARNING, and left out of the executions and the error rate, so an alarm on timer error rates stayed quiet while an auth backend was down. A distributed cron firing follows its timer: the interval record has `status: "failed"` and the error, where it said `refused`. A fire-and-forget request whose gate crashed is logged at ERROR, not at WARNING as "refused". A gate that refuses on purpose is unchanged.
- **Fix**: the framework's own log lines that have a service in hand are bound to it, so they reach that service's `logs.<service>.<level>` stream, which publishes only the records bound to its service. The cron timers (a skipped occurrence, a lease that could not be recorded, a bucket whose TTL could not be read, a failed loop), the connection pool and its extension, the cyanide, otel, resilience (a rate limit exceeded, a message with no rate-limit key) and auth extensions, and the discovery warning for an undecorated override wrote through the bare logger, which carries no binding to their own service. Two of those lines are written before `LoggingExtension` attaches the service's sink after the connection is made, so they reach a stream only through a sink the host attached earlier: the cyanide seed line (written in `setup`) and the discovery warning for an undecorated override. Lines with no single service stay unbound: the multi-service orchestrator, the CLI, the loop host, `exceptions`, `correlation`, `get_correlation_logger`, `SimpleAuthService` and the shared rate limiter's own recovery lines.
- **Fix**: `cliffracer run` exits with code 2 when the `--config` YAML or the flags give a service a value its config refuses, such as a `restart_delay` below 0 or a `nats_user` without a `nats_password`, and names the service, the field and the reason. The overlay was validated at construction and the runner retried the refusal on its restart backoff, so the command never exited. A `ServiceRunner` given such `overrides` reports the refusal once, without a traceback, and stops with `RUNNER_SERVICE_DOWN`; a constructor that raises is retried as before. The refusal text is each error's field and reason, not the input pydantic echoes, and it holds no credential; the reason for a refused `name`, `namespace` or `dlq_subject` shows that value so it can be read. The log sink that `--log-level` installs prints a traceback's frames and not the values in them (loguru `diagnose=False`), where it printed the overlay, a YAML `nats_password` included, on every restart.
- **Fix**: `cliffracer run` prints the frames of a traceback and not the values in them whether or not it is given `--log-level`. Without the flag it kept loguru's default sink, which prints the value of every expression on each line of a traceback, so a service that crashed at startup while holding a credential (or the `--config` overlay) printed it, about 230 lines of values for one crash. The command now replaces that sink with one at the level it had (`LOGURU_LEVEL`, else DEBUG) and `diagnose` off. An application that embeds the services through `ServiceOrchestrator` is unchanged and keeps its own logging.
- **Fix**: `cliffracer-dlq` exits 3 with the broker's reason when a broker that answered refuses the connection, a refused login among the reasons, and names `--user`, `--password`, `--token` and `--creds` when the reason is an authorization one. It reached the user as a 33-line nats-py traceback and exit 1, a code its exit table does not list. The command dials through `cliffracer.core.dial.connect`, which closes the client a cut-off dial leaves behind where a bare `nats.connect` left it to the garbage collector.
- **Fix**: `cliffracer-dlq` prints a `--server` URL that has no scheme (`user:password@host:4222`) without its user and password. Its own address helper found no hostname in such a URL and printed the whole string, credentials included, in "no broker answered" and "refused the connection". The address now goes through the shared redactor when it has no hostname.
- **Fix**: `register_broadcast_handler` called after the service has started raises `ServiceLifecycleError` naming the pattern and where to register instead. It was accepted and listed in the registry, but subscriptions are created once at start, so the handler was never called. Registering in `__init__` or `on_startup` is unchanged.
- **Fix**: a handler that is the first to call `stop()` (a "shutdown" RPC) finishes after `stop()` returns. The stop waited `shutdown_timeout` for the supervised task that was running it, then cancelled that task, so the handler never replied. The drain and the cancellation that follows leave the task running the stop alone; every other supervised task is drained and cancelled as before. The handler still cannot reply to its caller after `await self.stop()`, because the stop has disconnected the service: a shutdown handler should start the stop with `asyncio.create_task(self.stop())`, keep a reference to that task, and return, which sends the reply first (`docs/api-reference.md`, "Tasks that refuse shutdown").
- **Fix**: `stop()` is called on an extension only when its `setup()` was begun, as `docs/extensions.md` says it pairs with `setup()`. After a `setup()` that raised, the teardown also called `stop()` on every extension declared after it, which had built nothing, so a `stop()` that dereferenced state `setup()` creates raised and was logged as a failed stop. The extension whose `setup()` raised is still stopped, because it may hold part of what it was building. A service that never began its setup stops no extension.
- **Fix**: an event that fails its schema counts as `rejected` in the metrics extension's `/health` section and ends its span in error in the OpenTelemetry extension, as an invalid RPC does. The event path dead-letters or drops the payload and raises nothing, so both extensions saw a plain success: `rejected` stayed at zero and the span ended OK while the dead-letter stream filled. The dispatch is marked `ctx.data["outcome"] = "invalid"` for any extension that wants to count it.
- **Fix**: an id a service sends is held to the rule an inbound id is: printable text of at most 256 characters. The id given as `correlation_id=`, the ambient `CorrelationContext` id (which `set()` stores unchecked) and the id in a `ServiceClient`'s or a metrics pool's headers are used only if they pass, and one that fails is treated as absent with a warning, then a new id is made. A newline in such an id was written raw into the sender's log and an id over 256 characters was sent, and the receiver dropped it and began a new trace. The pool finds an id the caller passed under any of the receiver's spellings, as the client does. A dead letter's id is not held to the rule: it is taken from the record, the payload or the ambient context as a string.
- **Breaking**: `call_rpc`, `call_async`, `call_rpc_no_wait`, `publish_event` and `broadcast_message` raise an `RpcError` for what a connection raises on a send, as `ServiceClient` does: an `RpcClientError` for an argument larger than the broker's `max_payload`, an `RpcConnectionError` for a full outbound buffer during a reconnect, a draining connection, a closed or stale one and, on `call_rpc`, any other nats-py error. They reached a handler's `except RpcError` as raw nats-py classes. One function maps the errors for the client and the service. A JetStream error about the stream itself is not mapped. A caller that caught `nats.errors.MaxPayloadError`, `OutboundBufferLimitError` or `ConnectionDrainingError` around these calls catches `RpcClientError` or `RpcConnectionError` instead; the nats-py error is the `__cause__`.
- **Fix**: a dead-letter record carries its cause in a `cause` field (`decode`, `delivery-limit` or `invalid`), and `cliffracer-dlq` lists and counts by it. The inspector told a decode failure from a handler that ran out of deliveries by the text of `error`, so a handler that raised an exception whose message began `Decode error:` was counted and listed as a decode failure. A record published before the field existed has none and is still classified by its shape, `error` text included. A consumer of the dead-letter stream that rejects a record with a field it does not know must accept `cause`.
- **Fix**: The dead letter for a JetStream handler that overran `max_processing_time` carries the correlation ID the handler ran under. An exception a handler raises was stamped with the dispatch's ID and the dead letter read it back, but a handler cancelled at its budget raises nothing the dispatch can stamp: the overrun is re-raised as a new `TimeoutError`, which carried no ID, so the dead letter used the wire's ID or minted a new one. For a message that arrived with no ID, the dead letter's ID appeared in none of the handler's log lines. The overrun error now carries the ID the message was dispatched under, whether the handler was cancelled or swallowed the cancellation and returned.
- **Fix**: A `RetryMessage` whose `retry_after` is `nan`, `inf` or a negative number no longer puts it in the RPC reply. The reply carried the number as it came, so a client got `NaN`, `Infinity` or a negative delay, and `NaN` and `Infinity` are not JSON, so a client in another language could not read the refusal at all. The reply now leaves such a `retry_after` out, as it does for a refusal that carries none, and `RpcRefusedError.retry_after` is `None`. A finite number of zero or more is written as before. The NAK path and the reply ask the same question of the number, whether it is finite.
- **Fix**: A stop can no longer leave a distributed cron job's lease and a `running` record behind. `Timer.stop(grace)` waits for a run only while the handler is executing, and `DistributedCronTimer` still had two writes to make after the handler returned: the outcome in the interval record and the release of the `.active` lease. A stop that landed there, with any grace, cancelled the task at the first, the release never ran, and until `lease_ttl` (300 seconds by default) every replica skipped every firing as "prior run still active" while the record said `running`. The writes that finish a firing now run to their end whatever cancels the task meanwhile, and the cancellation is raised after them, so whoever waits for the task (the stop, the service's drain) waits for them. A write that has not returned in ten seconds (`DistributedCronTimer.finish_timeout`) is given up on and logged, so a broker that goes away mid-finish cannot hold a stop open; the bucket's TTL removes what it would have left. The same holds between creating the interval record and starting the handler: a cancellation there leaves a record that says `cancelled`, no lease, and the handler does not start.
- **Documentation**: With `distributed=True`, `eager` runs once per cluster for as long as the bucket keeps the `.eager` key, not once on every service start: the bucket's TTL, which is `max(lease_ttl, 300)` seconds for a bucket the timer creates and whatever an existing bucket has, and a bucket with no TTL never runs it again. The first start takes the `.eager` key and leaves it, so a restart or a rolling deploy inside that window does not run the job again, on that replica or any other, which is what a job that runs once across replicas needs and is not what the `@cron` section's "once on service start" said. The `@cron` options and the docstrings now say so, and that work which must run after every start belongs in the service's `on_startup`. A test pins two starts in one bucket and one eager run.
- **Fix**: a JetStream event whose body is in an encoding the service lacks the package to read, such as msgpack on a service installed without the `msgpack` extra, is naked and redelivered, and dead-lettered only at the delivery limit. It was terminated on its first delivery as a "Decode error", with its body stored in the dead-letter stream as replacement characters, although a replica with the package would have processed it; the RPC path already called the same condition the service's own fault. An event on a transport with no redelivery is logged as an error and not dead-lettered.
- **Security**: A distributed cron firing's record in its KV bucket held the exception's own text under `error` whatever `expose_internal_errors` said, so a handler that failed with a connection string or a credential in its message wrote it where anyone who can read the bucket could read it. The record now holds the exception's type (`RuntimeError`), and the text as well (`RuntimeError: ...`) only when `expose_internal_errors=True`; `status: failed` and the duration are recorded either way. `Timer.last_error_type` carries the type beside `last_error`, which is unchanged.
- **Documentation**: the documentation of the order of decoding, the declared gates and validation is stated and pinned. A payload that cannot be decoded (a body that is not JSON or msgpack) reaches no declared extension: an RPC with one is answered `validation_failed` with the decoder's text under the default `rpc_validation_errors` policy, a fire-and-forget request is logged and dropped, and an event is dead-lettered (one in an encoding whose package is not installed is redelivered instead), so a gate is not consulted and a rate limit spends no permit on it. A payload that decodes and fails its schema is judged by the declared gates first, and spends a permit of a limit. The documentation said that a limit is checked after validation, so that a payload validation refuses spends none; the order is the reverse, and the resilience README and `docs/extensions.md` now agree with the code and with each other. The code is unchanged; a test pins the order for RPC, async RPC and events.
- **Behaviour change**: `CyanideConfig` raises `ValidationError` for a `slow_delay`, `raise_delay` or `sleep_timeout_duration` that is negative, NaN or infinite, naming the field, where it built and the value was met only when a fault ran: a negative delay slept for no time, an infinite one held the request for good, and NaN failed inside the dispatch hook with a bare `ValueError` (with `fails_closed` the caller was answered `internal` on every injected fault). The environment variables are read through the same model. A delay passed straight to `slow()`, `raise_after_delay()` or `sleep_past_timeout()` that is not a finite number of seconds, 0 or more, raises a `ValueError` that names what it was (NaN was refused by `asyncio.sleep` with `Invalid delay: NaN`). A request header `x-cyanide-delay` or `x-cyanide-duration` that is not such a number is logged at WARNING and ignored, and the fault runs with the configured value, where it raised an unlabelled `ValueError` from the hook (`float("abc")`).
- **Behaviour change**: `setup_correlation_logging(service_name)` with the default `replace_existing=True` makes `service_name` the process-wide `service`, merged into loguru's global `extra` as `LoggingConfig.configure` does, so every line after it carries the name it was given. After `LoggingConfig.configure("first")`, a later `setup_correlation_logging("second")` labelled every line `service=first`: `configure` stores the service in the process-wide `extra`, which loguru merges into each record before the correlation filter runs, and the filter's `setdefault` kept it. A `service` bound with `logger.bind(service=...)` still wins. With `replace_existing=False` the process-wide `service` is left as it is, since the earlier service's sinks stay installed; when it names another service, the call logs one WARNING naming both the kept and the ignored service.
- **Fix**: A distributed cron job now says so when the wall clock steps forward past occurrences, as a local one does. After a step forward the job runs the occurrence it waited for and the ones the clock jumped over are not run; `CronTimer` logged a warning naming how many and the first and last, and `DistributedCronTimer` did not log anything, so a multi-replica job lost occurrences without a trace. The warning is written by the wait both loops share, so neither can skip it.
- **Fix**: The generated client is lint-clean and already formatted in four shapes where it was not, and `emit` refuses what it could not express. A mutable default split over several lines carried its `# noqa: B006` after the closing bracket, where ruff does not read it; it is now on the line that opens the default. A module with a plain and an aliased import (`from shop.models import Item as ShopModelsItem, Order`) is written as ruff's isort wants it, one statement for the plain names and one for each aliased name, in ruff's order (I001 otherwise). A docstring of 75 to 77 characters was written with its closing quotes alone on a line, which `ruff format` rejoins, and joined it is over the width; it is wrapped over two lines. And a description with duplicate parameter names, names that are one name once Python normalises them (NFKC), duplicate method names, or a NUL in the version, the hash or a doc is refused by name, where it made a file that did not compile or one that silently shadowed a method. A model-typed parameter with a default is built as the model where the service marks it rebuildable, and stays the dict where it does not, which `mypy --strict` reports for the consumer.
- **Behaviour change**: `broker_permissions` refuses an inbox prefix whose grant would cover the role's own subjects. The inbox grant is `<prefix>.>`, a subscribe permission over everything beneath the prefix, and a prefix that was the service's environment prefix (`east`), its namespace, or a family such as `orders.rpc` made the role's "inbox" a blanket subscribe over its own traffic, in a generator whose purpose is least privilege. The refusal is a `ValueError` naming the subjects the grant would cover, for the service and the client role and for subjects added through `extra_publish` and `extra_subscribe`. A dedicated prefix anywhere else, `_INBOX.orders` or `orders.replies` for instance, is accepted as before, and `ServiceConfig` and `validate_inbox_prefix` are unchanged.
- **Fix**: A `StreamSpec` with a `max_age_seconds` under 120 and no `duplicate_window_seconds` is no longer refused when its own `model_dump()` is validated again. The dump wrote the default window of 120 seconds, the copy read as one that had set it, and the refusal of a window longer than the age fired. The runners' `ServiceTemplate` construction and the local supervisor's activation dump a service config and validate the dump, so a service holding such a stream could not be activated from a template. A dump of a declaration that left the window out now leaves it out, and validating it gives the same declaration; a window that was set is dumped and kept as before.
- **Breaking**: a `dlq_subject` template that raises while it renders, such as `dlq.{service.nope}` or `dlq.{service[0].x}`, is a `ValidationError` naming the template. It escaped `ServiceConfig(...)` as a bare `AttributeError` (or whatever the format raised), where every other bad template was a `ValidationError`, so a caller that handles `ValidationError` around construction, as the CLI's config loading does, missed it. A caller that caught `AttributeError` around the construction catches `pydantic.ValidationError` now.
- **Fix**: an extension argument that is a `NamedTuple` or another tuple subclass reaches the extension as that class. It was rebuilt as a plain `tuple`, both when the declaration was frozen and when an instance was bound, so `limits.calls` raised `AttributeError: 'tuple' object has no attribute 'calls'` when the service was built. A tuple subclass is now rebuilt item by item as its own type (`_make` for a `NamedTuple`, else its constructor given the items), so a `SharedDependency` inside it is still the shared object and a factory inside it is still called, as in a plain tuple. A subclass whose constructor cannot be built from its items is copied whole, and a `RuntimeWarning` says so when a `SharedDependency`, a factory or an extension is inside it.
- **Fix**: a handler named `config`, `logger` or `health_listener` is refused with the usual "give the handler another name" `ConfigurationError`, by `describe` and by discovery. `CliffracerService.__init__` sets those three on every instance, so the handler was published by `describe` and a generated client got a `config` method, while discovery found the instance attribute where the method should have been and registered nothing: the service started, and a call to the method got no responder. The names are read off a constructed service, so an attribute added to the constructor is reserved with it.
- **Behaviour change**: `$CLIFFRACER_SUBJECT_PREFIX` is validated like an explicit `subject_prefix`. Pydantic does not validate a default, so a prefix such as `a.b` or `my-env` read from the environment built a `ServiceConfig`. A service that declares streams or durables then failed at start, after the connection, with nats-py's `invalid stream name: 'a.b_ORDERS'`, and any other service ran under the prefix. The config now refuses it when it is built, with a message that names the environment variable, so a service that ran under such a prefix no longer builds: set a prefix of one token of letters, digits or underscores, or unset the variable. `tools/gen_service_config_table.py` documents the field's default as `None` whatever the shell exports, where `--check` reported the table stale when `$CLIFFRACER_SUBJECT_PREFIX` was set.
- **Fix**: a handler that takes or returns a Pydantic model with no JSON Schema (a field that is a callable or an arbitrary class) is refused with the module's usual named error, `Owner.handler: parameter 'x': Model has no JSON Schema, so a contract cannot carry it`. Pydantic's own `PydanticInvalidForJsonSchema` escaped `build_handler_spec` unnamed at service start, from `describe`, and from `cliffracer-generate-client`, which printed a traceback and exited 1. `describe` raises the named `UntypedHandler`, and the generator reports it as exit 4, "the service cannot be described". Listeners and broadcast handlers are named the same way.
- **Breaking**: a return model's schema hash, and with it the method's `signature_hash` and the description hash, is the hash of the model's serialization-mode JSON Schema, what the handler writes. It was the validation-mode schema, what a caller may send, so a `computed_field` added to a reply model, or a `serialization_alias` renamed on one, changed the reply on the wire and left the contract identity the same: `verify`, a template's contract check and a generated client's `--check` saw no change. The hashes that move are the return model's `schema_hash`, the method's `signature_hash` and the description hash, for a return model whose serialization schema differs from its validation schema. That is a model with a `computed_field` or a `serialization_alias`, and also one with a `Decimal` field (written as a string, read as a number or a string), a `Json[...]` field or a `field_serializer` that changes a field's type. `Decimal` is the common case. Such a return model now has a different `signature_hash` than before, so a generated client of a service that returns such a model, a `Decimal` field included, raises `ClientOutOfDate` until it is regenerated, and a template whose handlers return one needs a new revision where a revision already holds the old contract (registering it again under that revision raises `ActivationConflict`); a model whose two schemas are the same, and every parameter model, hashes as before.
- **Breaking**: an RPC reply, and the reply to a `describe` request, carries only its `Content-Type` and the `X-Correlation-ID` the service used, not the headers of the request it answers. `Msg.respond` publishes the inbound message's headers, so a caller received its own request headers back on every reply, including an `Authorization: Bearer ...` token the caller had sent and a correlation id the service had refused (an escape byte, an 8 KB value). It went only to the caller that sent it. A client that read some other request header off a reply must read it from its own request; the error and refusal fields are in the envelope, as before.
- **Behaviour change**: `Timer.stop()` called without the service's hand-over gives a run it cancelled `cancel_grace` seconds to finish, where it waited for ever for a callback that catches `CancelledError`. `cancel_grace` defaults to the `shutdown_timeout` of the service the timer belongs to (`STANDALONE_CANCEL_GRACE`, 30 s, for a timer that belongs to none), and `None` waits as before. A run still going when it ends is reported at error level and left running, and `stop()` returns. The service's own shutdown is unchanged: it hands such a run to the drain.
- **Behaviour change**: a timer schedules on the monotonic clock, so a step of the wall clock (an NTP correction, a VM resume, `date -s`) no longer moves its firings: after a backward step of an hour a timer sat idle for the hour. And a timer no longer reads a `_metrics` attribute of its service and calls `increment_counter` and `record_custom_metric` on it. Nothing in the framework set that attribute, and a service with its own `_metrics` (a dict of counters is a likely one) had every firing counted as an error and the timer put into its error backoff. A service that set `_metrics` to a `PerformanceMetrics` to get `timer_<method>_executions`, `_refusals`, `_errors` and `_duration_ms` counters from its timers no longer gets them; record them in the timer method.
- **Fix**: a timer's `token_factory` may be a coroutine function, as `AuthExtension(outbound_token_factory=)` may. An `async def` factory raised `AttributeError: 'coroutine' object has no attribute 'lower'` on every firing, counted an error, and never ran the method. And a token that already carries the scheme (`Bearer abc`, in any case) is sent as it is by both factories: `outbound_token_factory` sent `Bearer Bearer abc`, which the receiving service rejected, where a timer accepted the same token.
- **Fix**: A bad `CLIFFRACER_SUBJECT_PREFIX` no longer stops a service that pins its own `subject_prefix`. The client the framework builds to read the names a `ServiceClient` reserves was given no prefix, so it checked the one in the environment, and with a value no subject can carry every service that declares an RPC method failed to start, `describe()` failed the same way, and `cliffracer-generate-client --class` exited 5. That client addresses nothing and now pins an empty prefix, so it reads no environment. A client you build with no prefix still checks the environment's, and refuses a bad value naming `CLIFFRACER_SUBJECT_PREFIX`.
- **Fix**: The resilience README no longer lists a builtin `ConnectionError` among the errors that open a circuit, and a bare `record_failure()` no longer erases `last_failure`. `DEFAULT_MONITORED_EXCEPTIONS` has the four `Rpc*` classes only, so a builtin `ConnectionError` or `TimeoutError` that propagates through the breaker never counted, whatever the README said; a user who chose `monitored_exceptions` from it was protected against a case that is not one. `record_failure()` with no exception replaced the last recorded failure with `None`; it now counts the failure and leaves `last_failure` as it was.
- **Fix**: A cron job runs each occurrence once, and not before its time, when the wall clock steps back. `CronTimer` guarded a step made while it waited but kept no record of the occurrence it had just run, so after a firing a clock stepped back past that occurrence found it again and ran it again. `DistributedCronTimer` had no wall-clock guard at all: after a backward step it ran the job, and claimed the interval key, before its scheduled time, and the later "already acquired by peer replica" line named a peer when the claimant was this replica. Both timers now wait in one place, which records the last occurrence it started and searches only after it, and does not return until the wall clock has reached the target.
- **Fix**: A cron expression that names no date is refused with `ValueError` when the timer is built. `croniter.is_valid` checks the syntax only, so `"0 0 30 2 *"` (30 February), `"0 0 31 4 *"` and `"0 0 31 6,9,11 *"` were accepted; `_next_fire` then raised on every pass, the service started normally with a job that never ran, and `Error in cron loop` was logged every `error_backoff` seconds for as long as it lived. This was documented as impossible for a bad expression. `@cron`, `CronTimer` and the distributed timer all take the check, and an expression whose syntax is wrong or whose timezone is unknown is refused as before.
- **Fix**: Working out the NAK for a failed JetStream delivery can no longer leave the message without one. `nak_delay` raised `OverflowError` once a message had been delivered more than 1024 times (an integer power past 2**1023 does not convert to a float, and `jetstream_max_deliver` has no upper bound), so the exception escaped the failure branch and no NAK was sent. A `RetryMessage` with a `retry_after` of `nan` or `inf` passed the `<= 0` test and then failed to encode in nats-py, where `safe_nak` swallowed it and nothing fell back to a plain NAK. The backoff exponent is capped, a `retry_after` that is not a finite number above zero is no hint and the configured backoff is used, and a delayed NAK that cannot be sent is followed by a plain NAK.
- **Fix**: A pull consumer whose fetch keeps failing waits longer each time and no longer spins. Any error from `fetch` other than a timeout (a consumer deleted on the server, a subscription that is no longer valid, a permission error) was logged at ERROR and retried after `jetstream_nak_backoff` seconds, a setting that paces redelivery of a NAKed message and may be 0; at 0 the loop logged an error on every pass, about 31,000 in half a second. The wait is now `jetstream_nak_backoff` or one second, whichever is more, doubling for each failure in a row up to `jetstream_max_backoff` (never under one second), starts over after a fetch that works, and the error line says when the fetch is tried again.
- **Fix**: A `ServiceClient` dials again when the connection it opened has been closed for good. nats-py closes a connection for good on an authentication change or a terminal error from the server and leaves the object assigned, and the client dialled only when none had ever been assigned, so after a credential rotation that ended its session every later call raised `RpcConnectionError` with no dial attempt, for the life of the client. The next call now dials a new connection (callers that arrive together share one dial) and the drift check runs again on it. A connection the client was handed is left to its owner, and a client that was closed with `close()` stays closed.
- **Fix**: `redact_nats_url`, which every message that names a broker goes through (the service's connection errors, `RpcConnectionError`, a refused `nats_url`, the generator's messages), withholds more than the user and password. The value of a query or fragment parameter whose name says it is a credential is printed as `***` (`?token=`, `?pass=`, `?pwd=`, `?password=`, `?user=`, `?auth_token=`, `?api-key=`, spelled in any case or percent-encoded; the shared credential names plus `pass`, `pwd`, `user`, `username`, `user_name`, `auth` and `key`), and the other parameters are kept. The scheme `nats+tls` is kept in the redacted form. A URL with nothing to withhold is still returned unchanged.
- **Fix**: The in-progress heartbeat is paced by the `ack_wait` the server enforces for the durable, not only the one in the config. A durable keeps the `ack_wait` it was created with, so when `jetstream_ack_wait` asks for more than the durable holds, the heartbeat pulsed at half the config's value, too slowly for the server, which redelivered the message under a running handler: against a 1 s durable and a 30 s config, a 2.5 s handler ran three times for one message. The service reads the durable's `ack_wait` when it subscribes, as it already does `max_deliver`, and paces the heartbeat, in the handler and while a message waits for a concurrency permit, from half the shorter of the two.
- **Behaviour change**: cyanide's random mode injects the same faults in a run that has the same `seed`, for callers that send an id they choose and for callers that send none. The draw was keyed on `ctx.correlation_id`, which `CorrelationExtension` always fills and fills with a fresh id when the message carries none, so for callers that send no id two runs over identical messages injected different faults and the seed logged at start was not a way to replay one. A message the caller gave an id (a header, or `correlation_id` in the payload) is drawn for by that id, in any arrival order. A message with no id is drawn for by its subject, its payload and how many identical messages came before it, so identical messages are neither all faulted nor none: a payload sent 300 times gets the configured mix, and the same mix each run. The count is kept for the last `injection_record_limit` distinct subject and payload pairs, and a forgotten pair starts again at its first draw. The faults any given seed injects are different from before. Traffic sent through cliffracer's own senders (`ServiceClient`, `call_rpc`, `call_async`, `publish_event`) is not replayed exactly by default: they always send a correlation id, a new one per request when none is set, and that id is what identifies the message; set the id from the request's number to replay such a run.
- **Breaking**: a rate limit's counter is keyed on the service (with its namespace), the handler and the key's value: `<namespace>.<service>:<handler>:<value>`, `<service>:<handler>:<value>` with no namespace, and `<service>:<handler>` for a handler that declares no key. The key used to be the value alone (the handler's name when there was no key), so two handlers keyed on one header shared one counter per caller (`export` was refused for a call only `search` had made, and a handler allowed 100 calls spent the budget of one allowed 1), and two services that name a handler alike and share a `KvRateLimiter` bucket, as every service does by default, counted in one entry (`customers.process` was refused for calls only `orders.process` had made). Replicas of one service still share a counter. Counters in a `KvRateLimiter` bucket restart once on upgrade, because the entries are under new keys; the old entries are left in the bucket and are removed by `prune_expired` or the bucket's TTL. While replicas of both versions run, each version counts under its own keys, so a caller can spend both budgets. A `reset(key)` made with a caller's value alone no longer finds the counter: it takes the counted key. The fingerprint in a refusal's `details` is still of the value alone. A budget shared across handlers or services cannot be declared. A function decorated with `@rate_limit` and called directly, outside a service's handler dispatch, still counts under the key's value alone, or `global` with no key.
- **Behaviour change**: the interval record of a distributed cron run that the service cancels has `status: "cancelled"`, with `duration_ms` and `completed_at` and no `error`. `Timer.stop()` cancels an in-flight run once its grace has passed, and the record said `completed` for it, so a reader that filtered on `completed` counted a killed run as a success. The status values are now `running`, `failed`, `refused`, `completed` and `cancelled`; code that reads the record and only knows the first four sees a fifth. The `CancelledError` is still raised after the record is written, the active lease is still released, and the record is kept, so a peer replica does not run an interval that was cancelled.
- **Breaking**: a distributed cron job with `no_overlap` (the default) whose `lease_ttl` is longer
  than the TTL of the bucket it is handed is refused when the service starts, with a
  `ConfigurationError` that names the job, its `lease_ttl`, the bucket and the bucket's TTL. A job
  with `no_overlap=False` writes no lease and is not checked. A bucket expires every key in it,
  active leases included, after a TTL that is fixed by whichever job opened it first, so a job that
  asked for a one-hour lease on a bucket another job had opened with five minutes had its overlap
  lease vanish after five, silently. A bucket that keeps keys longer than the job asked, or has no
  expiry, is used as it is. A bucket whose TTL cannot be read at start does not stop the job: it is
  logged, and the loop reports what it cannot do. To clear the refusal, give the job a bucket of its
  own (`bucket=`), or start the job with the longest `lease_ttl` first. This can stop an existing
  service from starting after an upgrade: the default `lease_ttl` is 300 seconds, so a job on a
  bucket someone created with a shorter TTL (60 seconds, say) is now refused where it used to start
  and lose its lease early.
- **Behaviour change**: the `replica` field of a distributed cron record, and the replica a skip warning names (`prior run still active on replica '...'`), is the service's `instance_id` attribute if it sets one, and otherwise `<hostname>-<pid>`. `instance_id` is defined by nothing in the framework, so for a real service the field was a fresh random `replica-<hex>` on every firing: it matched no pod and no log line, and one replica appeared under a different name in each interval's record. In a container the hostname is the pod or container name.
- **Breaking**: Five things a `ServiceClient` did in silence it no longer does. A correlation ID the caller put in `headers` under another case (`X-Correlation-Id`, the usual HTTP spelling) or under another of the names the service reads was ignored and replaced by a new one, which the service then preferred, so the trace broke; it is now read the way the service reads it and the caller's other-case duplicate is not sent beside it. A `service`, `namespace` or `subject_prefix` that `ServiceConfig` would refuse is refused with `ValueError` when the client is built (the prefix from `CLIFFRACER_SUBJECT_PREFIX` counts when the client is given none, and a client that pins its own is not refused for the environment's), where it used to build a subject nothing served (the call waited out `timeout`) or, with white space, a malformed frame on a possibly shared connection; a client that names no service refuses the call. A `verify()` that raises `ClientOutOfDate` clears the flag an earlier success set, so the next call verifies again instead of skipping the check. A describe that fails while a call is re-verified after a validation reply no longer replaces that reply with its own error: the call raises the `RpcValidationError` the service sent, where it raised the describe's `RpcTimeoutError` or `RpcConnectionError` (so does any other `RpcError` the describe raises, `ClientOutOfDate` apart). And a dial that timed out after the broker reported an error, an authorization failure for instance, names the last error, which used to appear only in nats-py's own log.
- **Breaking**: `ServiceClient.close()` and leaving `async with` release the connection while the broker is reconnecting and return normally, where they raised nats-py's `ConnectionReconnectingError`; an `except` for it around either no longer runs, and the block's own exception is never replaced. `close()` drained and only then marked the client closed, and nats-py refuses a drain while it redials (`ConnectionReconnectingError`), which is when a shutdown path runs, during a broker outage: the client stayed usable, the connection stayed open, and the nats-py error replaced the application's own exception in `async with`. The client is marked closed first, a connection that cannot be drained is closed instead, and a failure to release is logged and not raised. A `close()` that ran during the first connect found nothing to release, and the connection that connect then returned was never closed and reconnected for as long as the process lived; it is now closed and the waiting call raises `RpcConnectionError`.
- **Breaking**: `ServiceClient` raises an `RpcError` for every nats-py failure on the request path. `MaxPayloadError` (an argument larger than the broker's `max_payload`), `OutboundBufferLimitError` (the buffer is full during the reconnect gap) and `ConnectionDrainingError` reached the caller as nats-py exceptions, which a caller's `except RpcError` does not hold. An oversized argument is now an `RpcClientError`, the buffer-full and draining cases and any other nats-py error on the request path are an `RpcConnectionError`, and each keeps the nats-py error as its `__cause__`. An error that is not nats-py's still propagates unchanged. Code that caught `nats.errors.MaxPayloadError`, `OutboundBufferLimitError` or `ConnectionDrainingError` around a client call no longer catches them: catch `RpcClientError` or `RpcConnectionError`, or `RpcError` for all.
- **Fix**: A generated client for a service with an RPC method named `list`, `dict`, `str` or any name a signature reads now imports. A `def` evaluates its annotations in the class body, where the methods defined before it are names, so after `async def list(...)` the next signature's `list[str]` was the method and the import raised `TypeError: 'function' object is not subscriptable`; a method named `str` imported and silently gave the next annotation the method, and a model named `VERSION`, `SERVICE`, `NAMESPACE`, `DESCRIPTION_HASH` or `SIGNATURES` was shadowed by the class attribute. A name that is both bound in the class and read by one of its annotations is read through a module-level alias (`_Unshadowed_list = list`) bound before the class, so `inspect.signature` still returns the real classes. A method named `Literal` in a client whose signatures use `Literal` is refused by name. The generator, `--check` and the service module all succeeded before; only importing the client failed.
- **Fix**: A call admitted before a circuit tripped can no longer reopen the circuit after it has recovered. A call admitted while the circuit was CLOSED carried an epoch that only `reset()` and `trip()` moved, so when `request_timeout` was longer than `recovery_timeout` and such a call failed after a successful probe had closed the circuit, its failure counted in the new closed run, and with a low `failure_threshold` it reopened a dependency that had just recovered, on evidence from before the recovery. A probe that closes the circuit now moves the epoch too, so a late result from an earlier run is recorded (`last_failure`, the totals) and not counted toward the failure run.
- **Breaking**: `stop()` against a broker that has gone silent is bounded by `shutdown_timeout` and does not raise `FlushTimeoutError`; code that caught it around `stop()` reads the warning in the log instead. A path to the broker that drops packets still reads as connected for minutes, so the connection is drained, and the drain waited on an answer that never came: nats-py's own 10 seconds whatever `shutdown_timeout` was, after which `FlushTimeoutError` escaped, so `stop()` raised it and reported a failed stop although every other step had finished. The drain now gets up to `shutdown_timeout` seconds (no deadline when that is `None`), and one that times out is a warning that messages still buffered may not have been sent, followed by the close. A shutdown is therefore up to four `shutdown_timeout` periods in the worst case: the timers' grace, the drain of active tasks, the cancellation grace and the connection's drain; an extension's `stop()` still has no deadline of its own.
- **Fix**: `cliffracer-generate-client --timeout` bounds the whole dial to the broker, the one reconnect attempt included, and a failed dial prints the one-line message and nothing above it. `--timeout 1` took about 2.9 seconds to give up on a broker that accepts a connection and says nothing, and nats-py's default error callback printed about 65 lines of traceback before the message. The reply is still waited for up to the same timeout again, and the help text says so.
- **Fix**: the restart backoff doubles up to the larger of 60 seconds and the configured `restart_delay`. A `restart_delay` above 60 seconds waited that long once and 60 seconds on every later crash, so the backoff shrank after the first restart.
- **Fix**: `await orchestrator.stop()` called before `run()` is honoured: `run()` then starts no service and returns 0. The request was recorded and then undone by `run()`, so every service was constructed, started and stopped once.
- **Behaviour change**: `cliffracer-generate-client` reports a broker that refuses it as exit 3 with the broker's reason: `refused this client's credentials` for a wrong or missing user, password or token, and `refused this client a permission the request needs` (pointing at `--inbox-prefix`) for a role whose permissions do not allow the reply inbox. A refused login was reported as `no broker reachable ... no servers available`, and a permissions violation as `no service answered the describe subject`, which sent the reader to the wrong place; a permissions violation exited 2 and now exits 3. The exit code table now says exit 3 covers both.
- **API Change**: `cliffracer-generate-client` takes `--inbox-prefix`, the inbox prefix its connection's replies arrive on. A client role the broker confines to its own inbox prefix (`docs/broker-permissions.md`) cannot subscribe to the default reply inbox, so the command could not describe a service through it; the broker's user and password or token go in `--nats-url`. The flag applies only without `--class` and takes the same prefixes `ServiceConfig(nats_inbox_prefix=...)` does.
- **Breaking**: `KvExtension` and the bucket and object-store configurations refuse a declaration that cannot work with a `BucketConfigError` naming the bucket and the option: the two configurations when they are built, and `KvExtension` when it reads its declarations at setup. `buckets="profiles"` declared eight one-letter buckets and a single dictionary declared its keys; a single name, configuration or dictionary is now one declaration. A declaration that was none of those raised `AttributeError`, a typo in a nested `placement` or `republish` raised a bare `TypeError`, and a name with a dot, a non-numeric `max_value_size`, a non-bool `direct` or a `limit_marker_ttl` that is not whole seconds built and failed only when the extension started, with an error that was not a `KvError`. A bucket declared twice with different options kept only the last without a word and is now refused.
- **Fix**: `KvExtension.delete(bucket, key, last=n)` raises `ValueError` for a revision that is not a positive integer (`0`, a negative number, a bool, a string, a float), as `get(revision=)` does, before it touches the bucket. nats-py applies the compare-and-delete only to a positive number, so `last=0` and `last=-1` deleted with no check at all: a caller whose stored revision defaulted to `0` deleted without the check it had asked for. `last=None` still deletes unconditionally, and a positive revision is checked as before.
- **Fix**: the warning for an existing bucket that differs from its declaration now covers `republish` and `placement`, the two options the check skipped, so a declared audit republish that the bucket does not have is named at start instead of silently not happening. `KvExtension.start()` takes the buckets and object stores the service already opened (through `get_bucket()` or `get_object_store()` in `on_startup`) from the cache: they are opened and reported once, and the handle the service holds is not replaced by a second one.
- **Breaking**: `put_object()` stores a dataclass, tuple, set, datetime, date, UUID, decimal or enum
  as the JSON `put()` stores, where it raised nats-py's `TypeError: nats: invalid type for object
  store`, naming no type; a value with no JSON form raises, from `put_object()` as from `put()`, a
  `TypeError` naming its type. `serialize_value` (so `put()`, `create()` and the object-store
  writes) refuses an iterator, a generator or a file object, alone or inside a dict or list, where
  it stored the array of its items and left it consumed, and refuses `NaN` and infinity, which are
  not JSON, where it wrote them. Read a file first, or pass it to `put_object()` to stream it.
- **Fix**: `setup_correlation_logging` adds its sinks before it removes the sinks that were installed, as `LoggingConfig.configure` does. A log directory that exists but cannot be written raised `PermissionError` after the host's own sinks had been removed and one console sink added that nothing held an id for, so the process was left logging to the wrong place and the caller had no way to undo it. It now raises with the process's logging exactly as it was found.
- **Fix**: the log files `LoggingConfig.configure` and `setup_correlation_logging` write stay inside the log directory. A path separator in the service name becomes an underscore in the file names: `/srv/x/evil` wrote `/srv/x/evil.log` outside the directory and `a/b` wrote `logs/a/b.log` below it, and now they write `_srv_x_evil.log` and `a_b.log` in it. A `CLIFFRACER_LOG_DIR` that is set and empty is the default `./logs`, where it put the files in the working directory. A name without a separator keeps its file names.
- **Fix**: the sinks `setup_correlation_logging` adds keep a `correlation_id` the call bound, as they keep a bound `service`: `get_correlation_logger(...).bind(correlation_id=...)` and `get_service_logger(...).with_context(correlation_id=...)` printed `no-correlation` outside a request and the ambient id inside one, and now print the id they bound. A line written through `ContextualLogger`, `@log_rpc_calls` or `@log_event_handling` reports the `name`, `function` and `line` of the code that logged (for the decorators, the decorated function), where every such line reported `cliffracer_logging.config:info:448`.
- **Fix**: `OptimizedNATSConnection.connect()` opens the pool once and leaves it empty when it is cut off. Two tasks calling it at the same time on a fresh pool both opened a full set (twice `max_connections`, `utilization_percent` 200); the second now waits for the first. A call cancelled, or cut off by the caller's own `asyncio.wait_for` or `asyncio.timeout`, kept the connections it had opened and the next `connect()` returned at once, leaving the pool at the partial size; it now closes them and raises, so calling again connects the whole pool.
- **Behaviour change**: `PoolExtension` gives its pooled connections the service's `ping_interval` and `max_outstanding_pings`, as it gives them the service's reconnect settings, so a silent partition is noticed on a pooled connection as fast as on the service's own. It passed 120 and 3 whatever the service said, so a service set to 5 and 1 noticed a partition in 5 to 10 seconds on its own connection and in 360 to 480 on its pooled ones. A service that sets neither now has a pool on nats-py's defaults (120 and 2) where the pool used 3 outstanding pings. `PoolExtension(ping_interval=..., max_outstanding_pings=...)` still replaces the service's value, and `OptimizedNATSConnection` built directly keeps its own defaults of 120 and 3.
- **Fix**: `PoolExtension.request()` and `publish()` send the correlation id of the request being handled, in `X-Correlation-ID` and `correlation_id`, as `ServiceClient` and `call_rpc` do. They sent no headers, so the service that answered a pooled request generated a new id and the trace across the two services broke. An id passed in `headers=` wins, and a call with no ambient id sends a new one. `OptimizedNATSConnection.request()` and `publish()` take an optional `headers` argument and send it as given.
- **Fix**: The local supervisor logs why an activation failed. A `TemplateError`, a contract mismatch, a factory error and a failure in `start()` ended as a fixed reason in the snapshot while the exception was retrieved and discarded, and an abandoned task or a failed cleanup was not logged either, so an operator had no record of the cause. A failed activation is now logged at warning level with its address and the exception's type, a failed lifecycle cleanup and an unfinished cleanup (with the number of tasks abandoned) at error level, and none of the lines carries exception text, because inspection excludes it. The monitor also names the half of its condition that fired: `child broker connection closed` when the connection closed for good under a lifecycle that is still running (as with `exit_on_closed=False`), and `child lifecycle terminated` only when the lifecycle ended.
- **Breaking**: `ResilienceExtension(default_calls=, default_window=)` limits only what a caller sends (RPC, async RPC and event handlers). A `{service}.describe` request and a timer or cron firing are not counted against it: with `default_calls=2`, the third `describe` in the window was refused, so a client built with `verify=True` and the generator failed while the window was spent, `/health` counted the refusals under a handler called `unknown`, and a schedule was throttled by its own ticks. A handler that declares `@rate_limit` keeps its limit whatever its kind. A request that lacks the header or payload field its limit is keyed on is refused with `refused: rate-limit key 'x-client' is missing from the declared header source` (naming the key), and counts among that handler's refusals in `/health`. An RPC caller receives `RpcRefusedError` for it, where it received `RpcServerError` (the reply was `internal`, with an error logged per request), so a circuit breaker's default set no longer counts it and an `except RpcServerError` around the call no longer catches it. A durable event missing the field was redelivered up to `max_deliver` and dead-lettered; it is now acknowledged.
- **Behaviour change**: `/health` reports a limiter the extension does not ship by what it says about itself. `RateLimiter.health_details()` returns a dict (default `None`) that `ResilienceExtension` reports under `resilience.rate_limiter`, and under `handler_rate_limiters` for a handler that names its own; `InMemoryRateLimiter` returns `backend: memory`, `status: local` and `tracked_keys` as before. A limiter that returns nothing is reported as `backend: <its class name>`, `status: unreported`. Any limiter other than `KvRateLimiter` and `InMemoryRateLimiter` was reported as `memory` and `local`, which is the opposite of what a distributed custom limiter is.
- **Breaking**: `CyanideConfig` raises `ValidationError` for a setting it cannot carry out, where it accepted them and ran something else. Each `*_weight` must be between 0 and 1 and the four must add up to at most 1: random mode gives each weight a share of one probability in the order written, so `slow_weight=0.8, drop_reply_weight=0.8` injected `drop_reply` for about 20% of messages, not 80%, and a negative or over-1 weight starved or swallowed the modes after it. `mode` must be one of the names the extension knows (`slow`, `raise_after_delay`, `sleep_past_timeout`, `drop_reply`, `random` and their aliases): `mode="slwo"` with `enabled=True` ran the whole soak with no faults, one warning per message, while `/health` reported the mode. `set_mode()` and `configure_handler()` raise `ValueError` for the same names. The environment variables are read through the same model, so `CLIFFRACER_CYANIDE_MODE=slwo` or `CLIFFRACER_CYANIDE_SLOW_WEIGHT=1.5` raises, naming the setting, when `CyanideExtension()` is built. Declared in a service class body, as `cyanide = CyanideExtension()` is, that is when the module is imported, not when the service starts. A header that names no mode is still logged and ignored, since it is the caller's text.
- **Behaviour change**: `CyanideConfig.sleep_timeout_duration` defaults to 60 seconds, not 10. The `sleep_past_timeout` mode is for exercising a caller that gives up and a service that reclaims the work, and the default callers wait 30 seconds (`ServiceClient(timeout=30.0)`, `ServiceConfig.request_timeout`), so a call that reached the mode at 10 seconds was answered 20 seconds before its caller would have timed out and tested neither. A caller or service with a timeout over 60 seconds still needs a longer `sleep_timeout_duration`.
- **Breaking**: `ValidationExtension` runs after the extensions a service declares, not before them. An RPC or async RPC whose payload decodes but is invalid is now
  turned away by a declared gate first: an unauthenticated caller gets `refused: unauthenticated` (`RpcRefusedError`) where it got `validation_failed` (`RpcValidationError`) with the field errors and the
  rejected input, a rate limit spends a permit on a payload that validation then refuses, and a service's own validators do not run on input a gate refused.
  A payload that cannot be decoded (a body that is not JSON or msgpack) is still answered `validation_failed` before any declared extension runs.
  A declared extension's `worker_setup` cannot read `ctx.data["validated_kwargs"]`, which is set after it: reading it raises `KeyError`, which refuses every RPC as `internal` from an
  extension that `fails_closed` and is logged and passed over from any other. `worker_result` can read it. `ctx.payload` is the payload as
  it arrived in every hook, as before.
- **Fix**: a dependency probe that ends cancelled, because something it awaits was cancelled under it (a pool closing under a waiting `acquire`), is
  reported as a failed dependency, so `/ready` and `/health` answer 503 with `dependencies.<name>.ok: false`. The cancellation used to leave
  the health check and the listener closed the connection with no response. A cancellation of the health request itself is still raised.
- **Fix**: every extension's `after_call` runs when the task is cancelled while one of them awaits, as `worker_result` and `worker_teardown` already did.
  The hooks after the one the cancel landed in used to be skipped, so an extension that ends a span or releases a token there left it open. The
  cancellation is raised to the caller once every hook has run.
- **Behaviour change**: `AuthExtension` reads a timer firing's `token_factory` token from the `authorization` header the timer sends it in,
  whichever `header=` the extension reads from messages. With a renamed header the token used to be ignored: a timer was refused as
  `unauthenticated` under `allow_timers=False`, and ran with no identity under `allow_timers=True`. Now it authenticates as the token's
  user, and a token that does not validate is refused instead of being ignored. A message is unchanged: it is read from the configured
  header only.
- **Fix**: `AuthConfig.leeway_seconds` keeps a token valid past its `exp` where requests are checked, as documented. `validate_token`
  returned a context for a token inside the window, but the context's `expires_at` was the token's own `exp`, so `AuthExtension` and
  `requires_auth` refused the request as `unauthenticated` exactly as with no leeway. `AuthContext.expires_at` is now `exp` plus the
  leeway.
- **Behaviour change**: `put()`, `create()` and `put_object()` raise `TypeError` for a `SecretStr` or `SecretBytes`, alone or held at any depth by a model, dict, list or set. The error names where it is (`Login.pw`) and points to `get_secret_value()`. A model holding a `SecretStr` was stored with the mask `**********` in place of the secret and read back as a model whose secret is the mask, with no error. A field declared `Field(exclude=True)` is not part of the dump and is not affected. To store the secret itself, pass `get_secret_value()`; to keep it out of the bucket, store the rest of the model.
- **Breaking**: the distributed cron lock keys carry the service's namespace:
  `cron.<namespace>.<service>.<method>.<epoch>`, and the same stem for the overlap lease and the
  eager lock, in the same `cron_locks` bucket. Two apps on one broker with the same service name and
  the same schedule used one key per firing, so one app's job never ran; each now runs its own. A
  service with no `namespace` has the keys it had, so only a service with a namespace is affected by
  the upgrade. During a rolling upgrade of such a service, replicas on the old and the new version
  arbitrate on different keys, so a firing in that window runs once on each version: stop every old
  replica before the first new one starts, or make sure no scheduled time falls while both are up.
  Anything that reads or deletes a lock by key, a runbook that clears a stuck lease say, names the
  new key. The keys are not escaped, so a service named `a.b` with no namespace and a service named
  `b` in the namespace `a` share their keys and only one of them runs a firing.
- **Fix**: A config overlay is validated as one config. `apply_config_overlay`, which the CLI's `--config` YAML, `ServiceRunner(..., overrides=...)`, the test harness and `construct_service(Service, config)` all use, assigned the fields one at a time, and `ServiceConfig` revalidates the whole model at each assignment, so a pair of settings that is valid together and not alone was refused at its first field in either order: `nats_user` with `nats_password` could never be set on a self-configuring service, and neither could `jetstream_resource_mode="bind"` with `jetstream_update_streams=False` over a service whose own config has updates on. The merged config is now validated once and applied in place, so the container and the dispatchers still hold the same config object, and a refused overlay changes nothing where it used to leave the fields before the refused one changed.
- **Fix**: An inbound correlation ID is printable text of at most 256 characters, or it is not used. Only a CR or LF was refused, so an ANSI escape sequence that rewrites earlier lines in a terminal, a vertical tab, form feed, NEL or U+2028 that splits a logged line into several records, and an ID of any length (600 KB was logged on every line and copied into the headers of every outgoing message) were accepted from a header or a payload field any publisher can set. An ID that is not printable, or is longer than 256 characters, is treated as absent, as one holding a CR or LF already was: the next header or the payload field is tried, and otherwise a new ID is generated. The warning for a refused ID shows it escaped and cut to 64 characters. A tab in an ID is now refused too.
- **Fix**: a cancellation aimed at `service.stop()`, or at `Timer.stop()`, while it waits on another task now reaches it. `stop()` waited for a start in flight with `await start_task` inside `except (CancelledError, Exception): pass`, which also dropped a cancel aimed at the stop itself, so `task.cancel()` had no effect and `asyncio.timeout()` around a `stop()` never fired; `Timer.stop` awaited its cancelled run under the same kind of handler. Both now wait without awaiting the other task, read and discard its outcome, and let their own cancellation propagate.
- **Fix**: a `stop()` that is cancelled while it waits on a timer's run, or during the health-listener stop or the subscription cancel, still stops the health listener and cancels the subscriptions. The cancel jumped past both, nothing ran them later because the stop marked the service stopped, and a service stopped by the closed-connection cut-off reported stopped with its HTTP listener still accepting; a replacement on the same `health_port` could not bind. Both now run after such a cancel, shielded and bounded by `shutdown_timeout`, before `on_shutdown`, the extensions and the disconnect. The task drain does not: a cancel asks the stop to stop waiting for work in flight.
- **Fix**: a timer or cron run that catches its cancellation and carries on no longer makes `service.stop()` last as long as the run. `Timer.stop` cancelled a run that outlived its `shutdown_timeout` grace and then awaited the task with no deadline, so the stop lasted as long as the run, with nothing logged and nothing in `active_tasks`, and the health listener, subscriptions, drain, `on_shutdown`, extensions and disconnect behind it did not start. The service now hands a cancelled run that has not finished to the drain, which waits for it, cancels it again, names it at error level and leaves it in `active_tasks` until it finishes, as it does any task that refuses to stop. The timers' grace, the drain and the cancellation grace stay one `shutdown_timeout` each, before the connection's drain. `Timer.stop()` called on its own, with no hand-over, gives the cancelled run `cancel_grace` seconds and then returns, as a standalone timer stop does.
- **Behaviour change**: cancelling `ServiceRunner.run()` or `ServiceOrchestrator.run()` stops the services they started before the cancellation propagates. It left the service started, connected and answering, with no `on_shutdown` run, for any program that awaits a runner and ends it by cancellation (`asyncio.run` on Ctrl-C, `wait_for`, a `TaskGroup`). The stop is bounded by the service's `shutdown_timeout`; one that outlasts it is logged at error level and cancelled. Ending a runner with `stop()` is unchanged.
- **Fix**: `ServiceRunner.run_forever()` and `ServiceOrchestrator.run_forever()` stop as soon as they are signalled, wherever the service is. A SIGTERM or SIGINT that arrived during a restart backoff took effect only when the backoff ended, up to 60 s later, because the handler set the shutdown event without waking the event loop, so a service that kept failing to start outlived `docker stop`'s grace and was killed. A signal, or `ServiceOrchestrator.stop()`, during a start now cancels the start instead of waiting out `connect_timeout` (30 s by default) for a broker that does not answer.
- **Breaking**: `ServiceConfig` no longer prints `nats_password` and `nats_token`. They are `SecretStr`, as `AuthConfig.secret_key` is: `repr(config)` and `model_dump_json()` show them masked, a Python `model_dump()` keeps the secret objects so overlays and copies still carry the credential, and `config.nats_password.get_secret_value()` reads one. A refusal of a `ServiceConfig` (a model validator, a missing field, a misspelled option, an assignment) used to carry the whole input in the error: `str(error)` printed part of it and `errors()` and `json()` all of it, with every credential and the password embedded in `nats_url`. The input is now hidden in the error wherever it can hold a credential, and a value that belongs to one other field is still shown. Code that reads `config.nats_password` or `config.nats_token` as a `str` calls `get_secret_value()`; passing a `str` when building a config is unchanged.
- **Breaking**: in `cliffracer.testing`, `MockMessage.metadata` raises `NotJSMessageError` when the
  message was given no metadata, as the property on a core `nats` message does, where it answered
  `None`; and `MockJetStreamMetadata.sequence` is a `SequencePair(consumer, stream)` like a real
  delivery's, where it was an `int`. A dead letter made from a delivery the harness builds
  (`ServiceTestHarness.deliver_jetstream`) therefore carries `stream_sequence` and a `Nats-Msg-Id`
  of the form `dlq:<service>:<stream>:<stream_sequence>:<consumer>`. A test that read `msg.metadata`
  from a `MockMessage` it built with none, expecting `None`, catches `NotJSMessageError` or gives
  the message the metadata it means; a `sequence=` given a number is not read by a dead letter, so
  pass it a `SequencePair`.
- **Behaviour change**: `MetricsExtension` counts the refusal the pipeline makes when a `fails_closed` extension's hook raises (an auth backend that is down, a
  validator that crashes) under `errors`, not `rejected`. The reply to the caller already said `internal` for it; `/health` said `rejected`. `rejected` is now the
  refusals an extension authored and the events that fail their schema, so a dashboard that alerts on `errors` sees an auth outage, and one that watched `rejected` for it no longer does.
- **Breaking**: an explicit `publish_event(idempotency_key=...)` is used as given, inside an `@idempotent` handler as outside one: its `Nats-Msg-Id` is `<subject>:<key>`
  with no `#<n>` ordinal, and it does not advance the count the handler's own derived key uses. An id longer than 128 bytes is replaced by its SHA-256 hash, as it was, and an
  empty key counts as no key. Inside an `@idempotent` call the second and later explicit publishes carried an
  ordinal, so the id a key produced depended on publish order and the documented remedy for a handler that publishes from concurrent tasks did not remove that dependence. A
  handler that sent the same explicit key twice, relying on the ordinal to store both, now deduplicates the second against the first, and a retry that straddles the deploy
  can store a message of the old form twice. See the upgrade guide.
- **Fix**: `cliffracer.testing.assert_rpc_permissions` raises `AssertionError` under `python -O`, where its `assert` was removed and it returned for any grant. A
  failed JetStream delivery whose `.metadata` raises `NotJSMessageError` is NAKed (or dead-lettered at the limit), as one whose metadata reads normally is: both
  failure arms read the delivery count through the helper the dead-letter publisher uses, and used to raise out of the dispatch with the message unacknowledged.
- **Breaking**: `@rate_limit`, `RateLimitConfig` and `ResilienceExtension(default_calls=, default_window=)` refuse a limit that cannot work with a `ConfigurationError` where it is
  declared, which for the decorator is at import. `calls` must be an `int` of at least 1: zero or less, a bool, a float (`2.0` as much as `2.5`), a string and `None` are refused.
  `window` must be an `int` or `float` that is finite and above 0: zero or less, a bool, a string, `None`, `nan` and infinity are refused. A default limit takes `default_calls` and
  `default_window` both or neither, and `None` for either is leaving it out. A window of zero or less used to let every call through, `nan` or infinity refused for ever after the first `calls`, `calls` of zero or less
  refused every call, a bool counted as 1 and `calls=2.5` allowed three, each without a word, and a string or `None` raised a `TypeError` at the first call instead of where it was declared.
- **Breaking**: `AuthConfig.algorithm` accepts `HS256`, `HS384` or `HS512` and refuses anything else with a `ValidationError` when the config is built or the field is assigned. `none`, an
  asymmetric algorithm, a lower-case name and a typo used to be accepted, the service started, and the first login raised. A service configured with `none` could neither issue nor validate a token: `authenticate` and `validate_token` both raised.
- **Breaking**: `call_rpc`, `call_async`, `call_rpc_no_wait`, `publish_event` and `broadcast_message` on a service that has no connection raise a named error that
  gives the service and the subject, before any `before_call` hook runs: `RpcConnectionError` for the three calls, as the standalone client raises for a
  connection that is not there, and `ServiceLifecycleError` (a `RuntimeError`, not an `RpcError`) for the two publishes. They raised `AssertionError` with no message
  (`AttributeError` under `python -O`), after the hooks had run. A connection that exists and has closed, or is draining, raises `RpcConnectionError` on all
  five, the nats-py error as its cause: a caller that must handle every send without a live connection catches both classes.
- **API Change**: `RpcRefusedError` carries `retry_after`, the seconds a refusal asked the caller to wait (`None` when it did not), and fills `.details` from
  the refusal reply's `details` object, for `call_rpc` and the generated clients. The reply carried both for a `RetryMessage` refusal, such as the
  rate limiter's, and the client dropped them; a `retry_after` that is not a finite number of 0 or more, and an empty `details`, are left out of the reply. `str()` of a refusal that carries details now ends in `- Details: {...}`, as the other errors that carry them do.
- **Breaking**: `cliffracer run --config` refuses a top-level key other than `global` and `services` with a `ConfigError` (exit status 2) that
  names the key and where a service's settings go, and logs a warning for a `services:` section whose name no service in the run has. Both used
  to be dropped without a word, so a service section written at the top level, which is how the README showed the credentials example, applied
  nothing and the service connected without them. The README example now uses `services:`.
- **Fix**: A stream declared with a `max_age_seconds` under 120 is created with a duplicate window the server accepts. `StreamSpec` always sent two minutes, and the server refuses a window longer than the stream's age, so startup failed with `duplicates window can not be larger then max age`, which names neither field. Declaring `duplicate_window_seconds=0` got past it, because the server stores the age as the window, but every later boot compared the declared two minutes with the stored age and refused the stream as drifted. The default window, left out or `0`, is now the age when that is under two minutes (the `StreamSpec` field still reads 120; the configuration it sends carries the age), so a stream declared either way is created and stays unchanged across boots. A window set longer than the age is refused when the `StreamSpec` is built, naming both fields. The refusal for a stream that already exists with a different declaration now names each differing field with its declared and stored values; it listed the same subjects on both sides when the window was what differed.
- **Fix**: a Pydantic model whose fields declare `alias=` (one name, both read and written) that `put()`, `create()` or an object-store write stored is now read back by `get(as_type=Model)`. The model was written under its field names and validated under its aliases, so the read raised `userId  Field required`. The write now stores the field names when the model reads them back as itself, and the aliases when only those read back; a model that read back under its field names is stored with the bytes it had. A model that reads back as itself from neither form, such as one whose read alias is not also its written name (`validation_alias=` with no matching `serialization_alias`, or an `AliasPath`), is refused when it is written, with `ModelDoesNotReadBackError`.
- **Fix**: A JetStream message that waits for an event-concurrency permit is kept alive while it waits. With `max_event_concurrency` below the number of messages in flight, the in-progress heartbeat began only when the handler did, so a message that waited longer than the `ack_wait` the server enforces for the durable was redelivered to a replica that still held it and its handler ran again, which also brought its dead-letter limit forward. The heartbeat now starts when the message is received, on push and pull listeners alike. A push callback hands each message to its own task and returns at once, so the number of messages held is bounded by `jetstream_max_ack_pending`, and the messages queued behind a busy one are pulsed too; a stopping service finishes the handlers it has running and starts no message that was waiting for a permit: the message is left unacknowledged and the broker redelivers it after the durable's `ack_wait`, so across a stop and a restart each message is handled once.
- **Fix**: A `@timer` or `@cron` handler that calls `service.stop()` no longer deadlocks the shutdown. `Timer.stop`, which every timer type uses, waited for the task it was running in and then cancelled and awaited it: a cancel cycle that ended in `RecursionError` with `stop()` never returning, no `on_shutdown`, no extension stop and the connection still open. A stop that comes from the timer's own run, through any number of tasks (the service's stop spreads over several), now marks the timer stopped and returns; the loop ends when the handler does. A stop from anywhere else still gives a run in flight its `shutdown_timeout`.
- **Breaking**: a stream declaration the client or the server would refuse is refused where it is built, and `ensure_streams` refuses it before it creates the first stream. A stream name nats-py refuses (empty, or holding a wildcard, a dot, a slash, a backslash or white space), a subject the server refuses (an empty token, white space, a `>` that is not last), two subjects of one stream that overlap or repeat, and a `max_age_seconds` or `duplicate_window_seconds` that is negative or not finite each failed inside the create loop, after the streams before it were created, as a builtin `ValueError`, an `OverflowError` or a `ServerError` that named no declaration. A `StreamSpec(...)` holding one raises a `ValidationError` naming the stream, and so does a `ServiceConfig` that lists it. `ensure_streams` checks every declaration again where it is applied, because assigning to a field or `model_construct` skips the check made when it is built, and raises one `StreamDeclarationError` that names each refused stream and creates none.
- **Behaviour change**: the NATS log sink (`LoggingExtension(to_nats=True)`, `LoggingConfig.add_nats_sink`) publishes only the records bound to its own service, whose `extra["service"]` is the service's name. It took every record the process wrote, so with two streaming services in one process a line written for `billing` was published as both `logs.billing.warning` and `logs.orders.warning`, and a line from the host application's own code went out under both names. A record with no `service` binding, such as a host application's own `logger.info`, is no longer streamed; bind one with `logger.bind(service=...)`, or add a sink of your own with a filter, as ADR-0021 describes. The framework's own lines for a service (its logger, `get_service_logger`, the timer, health listener, broker probe, dependency checks and the logging extension's own lines) carry the binding.
- **Fix**: a distributed cron run that finishes removes the `no_overlap` lease only if it is still the one that run wrote. A run that outlived `lease_ttl` is run over by the next, which writes its own lease; the first run, finishing afterwards, deleted the key whatever it held, so the next interval found no lease and started a third run beside the second. The release is now a compare-and-delete on the revision the run's own write returned, and a lease that now belongs to a later run is left and logged at info. A run whose lease write failed deletes nothing.
- **Behaviour change**: the NATS log redactor and the dead-letter publisher now ask one rule, `cliffracer.core.credentials.is_credential_name`, which names are credentials, where each kept its own list. A name is a credential when, in any case and with `-`, `_` and `.` read as the same, it is `cookie` or `set-cookie` or contains `authorization`, `token`, `secret`, `password`, `passwd`, `passphrase`, `credential`, `api_key`, `apikey`, `access_key`, `private_key`, `signing_key`, `encryption_key`, `jwt`, `bearer` or `session`, or is the header an installed extension reads a credential from. The log redactor previously matched only a key equal to, or ending in, thirteen of those words, so `secret_key`, `jwt`, `cookie`, `set-cookie`, `bearer`, `session_id`, `signing_key`, `encryption_key` and `passphrase` were published under their own names, and it did not know the header a configured `AuthExtension(header=...)` reads; they are now redacted, and `LoggingExtension` gives its default redactor the headers of the extensions installed on its service. The dead-letter publisher withholds the same names, and now also `access_key`, `private_key`, `signing_key`, `encryption_key` and `passphrase`. Nothing is redacted or withheld less than before: every name either list matched is still matched. A log key that merely contains one of the words (`token_count`, `session_duration`) is redacted now, where only a key ending in one was.
- **Fix**: a `KvRateLimiter` shared by the services declared with it opens its bucket again when the connection it was opened on has closed. It kept the first service's bucket handle, so after the orchestrator restarted a service (which builds a new one from the same declaration and hands it the same limiter), or after a sibling service sharing the limiter stopped, every rate-limited request failed closed as `internal` until the process restarted. The restarted service now counts in the same bucket on its own connection, and a service whose connection really cannot serve still fails closed.
- **Fix**: An event whose payload carries a `correlation_id` that is not a string (a number, a boolean, a list or an object) is handled. Resolving the id raised `TypeError` before any handler ran, so a core event was dropped, a JetStream delivery was NAKed, and on its last attempt the dead letter raised the same error before publishing: the delivery got no ack, nak or term and no dead letter, and `dead_letters_lost` stayed 0. A value that is not a string is treated as absent, as a string is that is not printable text of at most 256 characters: the header's id is used if there is one, else a new id is made. A dead letter that cannot be built, or whose publisher raises, is now counted in `dead_letters_lost` and the delivery is terminated all the same.
- **Fix**: `cliffracer-generate-client` no longer prints the credentials in `--nats-url` or `$CLIFFRACER_NATS_URL`. The messages that name the broker (among them exit 2 for a service that did not answer and exit 3 for a broker that could not be reached) printed the URL as given, so a password or token in it reached the terminal and a CI log. They print it as `nats://***@host:port`, as every other place that dials a broker does.
- **Security**: `cliffracer-generate-client` refuses a model whose module or qualname in the `describe` reply is not a dotted path of plain identifiers, with exit 4 and the text escaped in the message. The module was written raw into the generated file's `from <module> import ...` line, so a reply from anything that answered `<service>.describe` (a plain subject with no owner) could put statements in the file, and they ran when the file was imported, on the developer's machine or in CI. A service whose models live in importable packages is unaffected: its modules are dotted identifier paths. `emit` applies the same rule when called directly, and the refusals the command prints (a service, method, parameter or error text from the reply) show control characters escaped and no longer print them.
- **Fix**: `docs/benchmarks.md` now reports the kv row's failure and recovery from the flags the benchmark recorded. The generator wrote "Graceful failure & recovery: Verified" as a literal, so a baseline recorded after a failed recovery check still published that sentence. The row says "Verified" only when `stress_failure_handled` and `recovery_verified` are both true, and otherwise names the one that is not. The committed page is unchanged.
- **New package**: `cliffracer-dlq`, a read-only command for the dead letters services publish. `cliffracer-dlq ls` lists them oldest first, `show SEQUENCE` prints one in full with the command that reads the original message, and `count` groups them by service and cause; `ls` and `count` filter by `--service`, `--cause` (`decode`, `delivery-limit`, `invalid`), `--since` and `--original-subject`. It reads the dead-letter stream with the stream's message-get API, so it creates no consumer, acknowledges nothing and writes nothing; replay is not part of it. A message that is not a dead-letter record is listed as `unreadable` with the reason. Exit codes: 0 ran, 3 no broker, 4 no stream, 5 no such message, 6 the broker did not answer a stream request, 7 wrong command line. Lifecycle tier `incubating`.
- **Breaking**: `OtelExtension` names an inbound span for the handler that ran, `{kind} {handler}`
  (`rpc get_order`, `event on_order`, `timer sweep`), where it used the subject (`rpc orders.123.get_order`,
  `timer`): every subject that reaches one handler is now one group, and the subject stays in
  `messaging.destination.name` and `cliffracer.subject`. An inbound `event` span is `CONSUMER` and a `timer`
  span is `INTERNAL`, where both were `SERVER` (`rpc` and `async_rpc` stay `SERVER`), so event handlers and
  timers leave a backend's server-side request and latency views. A `describe` request starts no span and is no longer counted in `spans_total` or, when it fails, in `errors_total` on `/health`.
  A saved query, alert, dashboard or tail-sampling rule that matches a span by its old name, or counts events as
  server spans, stops matching. Outbound spans are named as before.

## 1.1.0
- A message that did not come from JetStream, and whose body cannot be decoded, is dead-lettered. The publisher read the message's JetStream metadata to count its deliveries, and nats-py raises `NotJSMessageError` for a core message there, so nothing was published and the loss was not counted on `/health`. The decode path reads the metadata through the same helper as the delivery fields, and counts one delivery for a core message.
- **Behaviour change**: A dead-letter record carries `original_headers`, `stream`, `stream_sequence` and `consumer`, on all three kinds of record (undecodable, out of deliveries, invalid). The fields are additive: every field a consumer of `dlq.*` read before is unchanged, and a record published for a core NATS message, which has no stream, simply omits the stream fields. `original_headers` holds the headers the message arrived with except those whose name says they carry a credential (`Authorization`, `Cookie`, anything named for a token, secret, password or API key, and the header an installed extension reads a credential from), which `withheld_headers` names: a record outlives the message in a stream others can read. The dead letter is published with a `Nats-Msg-Id` of `dlq:<service>:<stream>:<sequence>:<consumer>` when the server gave all three, so republishing one delivery's dead letter within the stream's duplicate window stores it once; another consumer's dead letter of the same stream message has its own id and is kept. `DeadLetterPublisher.handle_invalid_message` takes an optional `msg`, the delivery being refused, to fill the fields.
- **API Change**: `setup_correlation_logging` takes `replace_existing: bool = True`, as `LoggingConfig.configure` does.
  The default is what it always did: it removes every sink loguru held before the call, the host's own and another
  service's. `replace_existing=False` adds this service's console and file sinks next to them. A sink installed before
  the call is still registered before the correlation sinks, so it does not see the `correlation_id` and `service` keys
  they fill.
- **Fix**: a first dial that `connect_timeout` cuts off, or that is cancelled or refused, closes the client it opened, for the service connection, `ServiceClient` and the metrics connection pool. Against a broker that accepted the connection and then said nothing, each cut dial held its socket open (two descriptors, and a task on the broker's side) until a garbage collection found it. The close does not run the dial's `disconnected_cb`, `closed_cb` or the other connection callbacks: a connection that never existed is not reported closing. The dials go through `cliffracer.core.dial.connect`; a test that patched `nats.connect` to stand in for the broker patches that function, or `nats.NATS`, instead.
- **Breaking**: `/ready` and `/health` now ask the broker, not only nats-py's connection flag.
  While the flag says connected they send a round trip on the existing connection, bounded by the new
  `ServiceConfig.broker_probe_timeout` (2 seconds). One that fails or is not answered in time makes
  the status `disconnected`, with 503 and `nats_connected` false, so an orchestrator now sees a pod go
  out of rotation within about that bound when the broker stalls or a connection goes silent without
  being reset (a partition that drops packets), where it used to keep answering 200 for 240 to 360
  seconds. The payload gains `nats_rtt_ms`, the last round trip in milliseconds, `null` when none was
  measured. The result is reused for the new `ServiceConfig.broker_probe_cache` seconds (1), a failure
  included, and checks that arrive meanwhile share one round trip, so a burst costs one PING.
  `broker_probe_timeout=None` turns it off and readiness reads the flag alone, as before. A service
  with a failure threshold of 1 on its readiness probe can now be taken out of rotation by one
  missed round trip; the architecture guide lists this and the two other ways readiness can go down
  while the broker is fine. `/live` is unchanged and never asks the broker.
- **Behaviour change**: A JetStream event whose payload fails while it is being validated is terminated whatever exception the validation raised. A model validator that raised `TypeError` (or any exception pydantic does not wrap in its `ValidationError`) was treated as the handler failing, so the delivery was NAKed and redelivered up to `jetstream_max_deliver` times and then dead-lettered as a terminated message; it is now refused on the first delivery as an invalid message, routed by `on_invalid` like any other, with one `validator_raised` entry in the record's `errors` and the exception logged at error level. An exception raised inside the handler is still the handler failing and is retried.
- **Fix**: `SimpleAuthService.refresh_token` verifies the token's signature once. It decoded the token in `validate_token` and again for its claims. What it accepts and refuses is unchanged.
- **Fix**: an event that carries no correlation id and reaches more than one listener runs every handler under one id, generated once for the message. Each handler was given its own, so the handlers' log lines could not be tied to the one message. A message with an id in its headers or payload is unchanged.
- **Fix**: a `KvRateLimiter` that has a JetStream context and cannot open its bucket tries to open it at most once every `BUCKET_REOPEN_SECONDS` (5), where every dispatch paid for its own failed request. The dispatches in between are decided without a round trip: by the in-memory fallback when it is on, and with `RateLimiterUnavailableError` when it is not.
- **Fix**: `CircuitBreaker.reset()` and `trip()` are final for the calls admitted before them. A call admitted while the circuit was closed reported its failure when it finished and counted toward the new run of failures, so calls in flight at an operator's `reset()` could reopen the circuit it had just closed. Their results are counted in the totals and no longer count toward the failure run, or clear it.
- `ServiceConfig` has two new fields, `ping_interval` and `max_outstanding_pings`, passed to the
  broker connection. They set how quickly a connection that has gone silent without being reset, as
  when a partition drops packets, is noticed: between `max_outstanding_pings * ping_interval` and
  `(max_outstanding_pings + 1) * ping_interval` seconds after it begins. Both default to unset, which
  leaves nats-py's 120 seconds and 2 in force (240 to 360 seconds), so a service that sets neither
  behaves as before. `ping_interval` must be above 0 and `max_outstanding_pings` at least 1. The
  architecture guide now says `/ready` follows nats-py's connection flag and states that window.
- A service's `on_shutdown` now runs when its stop is cancelled before it got there. A cancel that
  landed while the stop was draining active tasks used to jump past it: `on_shutdown` never ran, for
  that stop or any later one, and the service was marked stopped. The closed-connection stop is
  cancelled at a fixed 10 seconds, so a service whose handlers outlast that, with `shutdown_timeout`
  allowing more, lost `on_shutdown` every time the broker connection closed. It now runs once,
  shielded and bounded by `shutdown_timeout` (a fixed 30 seconds when it is `None`, so a stop can always be ended), before the extensions stop and the connection is
  disconnected: a further cancel does not abandon it, and one that hangs is abandoned at the bound
  and logged. A stop cancelled during `on_shutdown` does not run it again. The 10-second cut-off is
  unchanged, so the close callback can now take up to `shutdown_timeout` longer than that.
- `cliffracer-generate-client` decides that a refused `describe` earns the
  `--header authorization=...` hint from the reply's `code`, and reads the message
  text only for a reply that carries none (an old service). It used to test whether
  the text began `refused: `, so a server fault whose own text began that way was
  told to send credentials and a refusal worded differently was not.
- The API reference lists `@cron` as imported from `cliffracer_cron`, which is
  where it lives, with its full signature and the distributed options. The
  `@cron` docstring example no longer calls 09:00 UTC "local", and
  `examples/timer/timer_with_metrics.py` says it uses `MetricsExtension`, not a
  `PerformanceMetrics` integration. No behaviour changed.
- A dependency probe that returns something that cannot be awaited (a plain `def` that does its check
  and returns, or an `async def` missing its `async`) is reported down with a message that says so:
  "the probe returned bool, which cannot be awaited: ... declare it `async def` or return an
  awaitable". It used to fail with "object bool can't be used in 'await' expression", which names
  nothing the author wrote. The dependency was and is reported unhealthy either way, and the text
  still reaches the `/health` payload only when `expose_internal_errors` is on; the log line always
  carries it. A plain function that returns an awaitable, such as `lambda: client.ping()`, is still a
  working probe, so this is not refused when the dependency is declared.
- A service class that defines a method named `_setup_extensions`, `_start_extensions`,
  `_setup_subscriptions`, `_stop_timers` or `_stop_extensions` no longer replaces the container's own
  phase of that name. The container used to look each of them up on the service first, so such a
  method switched the phase off: the service's declared extensions were never set up, started or
  stopped, and nothing said so. The container now runs its own phases only, and a service's method of
  one of those names is just a method. A test that stood in for a phase this way should replace it on
  the container, or use `ServicePhases` from the test tier.
- Each package's `pyproject.toml` now carries `[tool.cliffracer] lifecycle = "<tier>"`, one of
  `incubating`, `supported` or `deprecated`, so the tier ADR-0018 describes can be read from the
  package without installing it. Nothing reads it at runtime yet: no import warning is emitted and
  no service behaves differently. All eight packages declare `incubating`, so none is
  claimed to be covered by the composition guarantee; a package moves to `supported` when that
  coverage exists for it. A repo check fails when a package lacks the declaration or gives a
  value that is not one of the three.
- An extension argument that is copied for each bound service but is not plain data
  (a list, dict, set, tuple, pydantic model, dataclass or value type such as a
  string, number, date or path) warns with a `FutureWarning` that names the
  extension and the argument. It is still copied, exactly as before. A later
  release will refuse such an argument at bind unless it is wrapped in
  `SharedDependency(...)`, to share one object across services, or given as a
  zero-argument callable, to build one for each; an unconnected `nats.NATS()`
  client is the case that copies silently today and is refused once connected.
- A stopped service's `/health` and `/ready` run no dependency probe: they answer `stopped` at once, with no `dependencies` or `unhealthy_dependencies` key and no call to its downstreams. They ran every probe first, so a stopped service hit its downstreams on every poll and answered as slowly as its slowest probe's timeout. A service that is `connecting` or `disconnected` still runs its probes and reports them.
- Stopping a service that has a `PoolExtension` waits for the requests its handlers have in flight on the pool to get their replies, then drains each pooled connection, all at the same time, instead of closing them at once; a request awaiting its reply used to fail when the service stopped. The wait for the requests and the drains share one bound, the service's `shutdown_timeout` (`None` waits without a bound), so `PoolExtension.stop()` takes no longer than that, apart from closing what is left; a connection that has not drained by then is closed with what it holds, and says so. Once `close()` has begun, `request` and `publish` on the pool raise `RuntimeError` instead of starting new work. `OptimizedNATSConnection` takes `drain_timeout` (default 30 seconds) for a pool built by hand.
- `PoolExtension`'s `/health` entry gains `active_connections` and `closed_connections` beside `connections`: how many of the pool's clients are connected and how many have closed for good. `connections` is still the number of clients the pool holds, closed ones included, which is why one number could say there was a pool while `connected` said it was useless, and nothing said how many of N were alive. The entry is read from the pool's own `get_stats()`.
- When the broker connection closes for good and the stop it starts is cut off at its fixed 10
  seconds, the warning now says so: how long the stop was given, that `shutdown_timeout` does not
  govern it, and that steps it had not reached may not have run (`on_shutdown` still does: the entry on `on_shutdown` after a cancelled stop describes it). It
  used to end on a colon with no reason, and the next line then reported that the service had
  "stopped after NATS connection closed" whether it had or not; that line now appears only when the
  stop finished. A stop that raises logs the exception's `repr`, so one with no message is still
  named. `ServiceConfig.shutdown_timeout`'s description and the configuration reference state the
  10-second cap. The cap itself is unchanged.
- `@cron(bucket=..., lease_ttl=..., no_overlap=...)` without `distributed=True`
  raises `ValueError` when the decorator is applied. Those options used to be
  dropped, and the job ran on every replica. The cron loop also confirms the wall
  clock reached the occurrence after its wait: a clock stepped back during the
  wait no longer fires the occurrence early and then again, and a clock stepped
  forward past occurrences logs a warning naming the ones that were not run.
- `OtelExtension`'s `spans_total` and `errors_total` on `/health` count every
  dispatch and every failure it saw, whether or not the sampler records the span,
  as `active_spans` always did. Under a sampler other than always-on they
  undercounted dispatches, left out the errors of sampled-out messages, and so
  disagreed with `active_spans`; with the default sampler the numbers are
  unchanged.
- **Breaking**: `ServiceConfig` refuses, when it is built, more than one way to
  authenticate to NATS (`nats_user` with `nats_password`, `nats_token`,
  `nats_credentials_file`) and a `nats_user` without a `nats_password` or the
  reverse. nats-py resolved two methods by its own precedence, so which credential
  was used was not decided by the configuration.
- `CircuitBreakerError`, the base class of `RpcCircuitOpenError`, is exported from `cliffracer_resilience`. Catching the family used to need an import from `cliffracer_resilience.circuit_breaker`.
- `MetricsExtension` counts a cancelled dispatch under a new `cancelled` key per dispatch kind in `/health`, and no longer counts it as an `error`. A shutdown that cancelled in-flight handlers, or a timeout that cancelled one, put errors on `/health` for crashes that never happened. `count` still includes it, and `rejected` and `errors` are unchanged.
- `BatchProcessor.get_stats()["items_per_second"]` divides the items processed by the wall-clock time at least one batch was running, not by the sum of each batch's own duration. Batches that overlap used to be counted twice, so four concurrent 50 ms batches reported about 20 items a second where the processor sustained about 80; they now count once, and the idle time between batches still counts for nothing. `processing_time_total_ms` is unchanged. `reset_stats()` restarts the rate.
- `PoolExtension`'s `/health` entry reports the pool's own sockets in `connected`, and the service's own connection to the broker in a new `service_connected` key beside it. `connected`, and `active_connections` and `utilization_percent` in `OptimizedNATSConnection.get_stats()`, used to read down whenever the service had lost its connection, whatever the pool's sockets said; they now count the pool's connected sockets only, so a working pool reads up while its service is cut off, and `service_connected` says the service is. `service_connected` is `null` for a pool built without a service, or with one that has no `is_broker_connected`. Anything that read `connected` as "the service can reach the broker" should read `service_connected`.
- `Extension` has two new hooks, `on_disconnect()` and `on_reconnect()`, run in declaration order when
  the broker connection is lost and regained, before the matching `ServiceConfig.on_disconnect` /
  `on_connect` slot. A service author can now drop a subscription inside `on_disconnect()` and the client
  will not replay it on reconnect. The client waits for `on_disconnect()` before it reconnects, so a hook
  that waits delays the reconnect: keep it quick or hand slow work to a task. `on_reconnect()` runs
  once the connection is back, so traffic is not held up while it runs. A hook that raises is logged
  and the others still run. Extensions that do not define them are unaffected.
- **Breaking**: `cliffracer.core.typed_rpc.python_type`, the inverse of `type_ref`, is removed. Nothing in the framework called it since the HTTP gateway left, and a TypeRef that carries constraints could not be turned back into an annotation that enforces them without it growing a second job. `type_ref` is unchanged. A generator or client that needs the inverse builds it with its own consumer, from the TypeRef kinds `scalar`, `model`, `list`, `dict`, `optional` and `literal` and the `constraints` a ref may carry.
- `ResilientMethodProxy.call_async` is documented, and pinned by a test, as
  admitted without limit while its circuit is half-open: it takes no probe slot,
  and `half_open_max_calls` bounds the awaited calls only. `setup_correlation_logging`
  says the keys its sinks add to the shared record are visible to a handler
  registered after them. No behaviour changed.
- **Breaking**: `BatchProcessor.add_item` takes `results="shared"` (the default) or `"per_item"`, and what a caller receives no longer depends on the shape of what the processor returned. It used to hand each caller its own element when the return value was a list as long as the batch, and the whole value otherwise, so a processor returning one aggregate list was unpacked whenever the list was as long as the batch (always, for a batch of one). Now: a processor that returns one result for the whole batch needs nothing, and every caller gets that value as it is; a processor that returns one result per item must be added with `results="per_item"`, and its callers get their own element only then. Callers of a per-item processor that do not pass it now each receive the whole returned list. With `"per_item"`, a return value that is not a list or tuple with one result for each item of the call fails every caller of that call with a `ValueError` instead of being handed over whole. Items added with the same processor and different `results` are processed in separate calls.
- **Breaking**: `AuthConfig.refresh_max_lifetime_hours` defaults to `720` (30 days)
  where it defaulted to `None`. A token is no longer refreshed once 30 days have
  passed since the login that began its chain, so a leaked token cannot be kept
  alive for ever by refreshing it, and a deployment whose sessions were meant to
  outlive a month must set the field (`None` removes the cap). Refresh itself is
  unchanged and now documented as a re-issue: the token it is given stays valid to
  its own expiry.
- `KvRateLimiter` remembers a refusal. Saying no used to read the key's whole timestamp list twice (once
  to decide, once for the retry hint), so a denied call at `calls=1000` received 53 KB for 210 bytes
  sent, and a client hammering a full limit cost the broker most of what the limit saved. Once a key
  has been refused, the limiter knows when a slot can next open (when enough of the counted
  timestamps have left the window) and answers `False`, and the retry hint, from memory until then:
  the first refusal is one read (26.6 KB at `calls=1000`), the rest are none. The limit stays the
  exact sliding window. What changes for an operator: a `reset()` made on ANOTHER replica is seen by a
  limiter that has a refusal remembered only when that refusal's moment passes, not at once; the
  limiter's own `reset()` clears what it remembers. `deny_cache_size` (default 10,000 keys, oldest
  dropped first; `0` turns it off) bounds the memory, and `KvRateLimiter.denied_locally` counts the
  refusals answered without a read.
- **Breaking**: the service's `call_rpc` raises what the standalone `ServiceClient`
  raises for the same reply. An error envelope is `RpcValidationError` (with
  `.details`), `RpcUnknownMethodError`, `RpcRefusedError` or `RpcServerError`
  where it was a plain `RpcError` whose message began `RPC Error calling <service>.<method>:`
  (an `RpcServerError` message now begins with the subject, and `.details` is filled
  for a validation error only). A connection lost before the reply, nats'
  `ConnectionClosedError` or `StaleConnectionError`, is an `RpcConnectionError`
  with the nats error as its cause. `except RpcError` still matches all of them,
  and the circuit breaker's default set now sees a lost connection and a server
  fault, which it did not.
- **Breaking**: `KvRateLimiter`'s bucket is named under the service's subject prefix when the
  `ResilienceExtension` opens it. With `subject_prefix="px"` the bucket is `px_rate_limits`, where it was
  `rate_limits`; with no prefix the name is unchanged. It was the one bucket that did not carry the
  prefix (a `KvExtension` bucket and the cron lock bucket do), so its counters were shared by every
  prefix and namespace on a broker, including the isolated prefix a test session takes. The name is
  visible on the broker (the stream is `KV_px_rate_limits`): an existing deployment that sets a
  subject prefix gets a fresh bucket on upgrade, so its limits start from zero, and the old
  `rate_limits` bucket keeps its counters until it expires or is deleted (`bucket_ttl` expires entries
  only in a bucket the limiter created with one). A limiter you open yourself, with `init_kv(js=...)` or
  a `kv=` you pass, keeps the name you gave it. A `KvRateLimiter` names one bucket: a limiter shared
  (it is, whenever it is given at declaration) by services with different prefixes raises
  `ConfigurationError` when the second one starts, rather than counting both in one bucket.
  `KvRateLimiter.bucket_wire_name` and `use_subject_prefix()` are new.
- `OptimizedNATSConnection.get_connection()`, and so `request()`, `publish()` and `PoolExtension`'s forwards, skip a pooled connection that has closed for good, which nats-py does once a connection's reconnect attempts run out. They handed it out every Nth call for the life of the process, and each of those calls raised `ConnectionClosedError`. When every connection is closed the call raises `RuntimeError` saying so. Each pooled connection also logs, at WARNING, that it closed and will not reconnect, and, at ERROR, the errors nats-py reports for it; `get_stats()` gains `closed_connections` and `connection_errors`.
- `OptimizedNATSConnection.connect()` does nothing when the pool already holds connections, where a second call added another `max_connections` clients to a pool whose maximum is `max_connections`, which also pushed `utilization_percent` past 100. After `close()` it connects afresh.
- `AuthConfig.leeway_seconds` (default `0`, so nothing changes unless it is set)
  accepts a token whose `iat` is that many seconds ahead of the verifier's clock,
  where a second of skew between the host that minted a token and the one that
  verifies it refused a valid login as not yet valid. It applies to `exp` too, so
  it also keeps every token valid that many seconds past its expiry, and
  `revoke_token` keeps a revocation for that long.
- `AuthExtension(outbound_token_factory=...)` attaches a service-identity token to
  every call, async call and published event the service sends, as
  `authorization: Bearer <token>` under the configured `header`, calling the
  factory (sync or async) once per message. Without it nothing is attached, as
  before. The caller's own token is never forwarded. Until now an authenticated
  service calling another one needed a hand-written `before_call` extension.
- A `BatchProcessor` batch that is cancelled, or ends in anything but an `Exception`, now fails the callers it had not yet answered: each `add_item` raises `RuntimeError` saying the batch was interrupted and the item may or may not have been processed, and the batch logs at ERROR how many callers it failed. They awaited a future nothing would resolve, with no timeout and no log line. A group the batch had already answered keeps its result.
- `PerformanceMetrics.record_latency` gives a request one outcome, a timeout whatever `success` says, and both `success_rate_percent` keys count by it. A timeout recorded with the default `success=True` was a success in `get_latency_stats` and a timeout in `get_throughput_stats`, and the verdict in `check_performance_targets` graded the service on the one that ignored timeouts.
- `PerformanceMetrics` has `record_custom_metric(name, value)`, which reads back as a gauge. A timer's `_metrics` hook calls it for the duration of each firing, and the method did not exist, so every successful firing on a service with a `PerformanceMetrics` as `_metrics` ran its handler and was then logged and counted as a timer error.
- `PerformanceMetrics.get_throughput_stats` follows the clock: it rolls the one-second window on a read as well as a write, so `current_rps` is the last completed second and falls to 0 when traffic stops instead of keeping the last busy second for ever, and idle seconds after the first request count as zeros in `average_rps` instead of being dropped. The time before the first request, and the second a `reset_metrics()` happened in, no longer add a spurious zero sample.
- `OtelExtension` with no `tracer_provider` logs a warning when it installs the
  process-wide provider, saying that provider has no span processor, so spans are
  recorded and not exported until one is added. A second service in the process
  that supplies none logs a warning naming the service whose `service.name` its
  spans will carry, which was silent. The behaviour is unchanged: the spans of
  every service after the first still report the first service's name.
- **Breaking**: A service with `serialization_format="msgpack"` is refused when it starts if the
  `msgpack` package is not installed, with a `ConfigurationError` that gives the
  install command. It used to start, connect and serve, and the first publish,
  call or reply that serialised raised `ImportError` inside a handler or
  publisher.
- **Breaking**: `CircuitBreakerConfig` refuses, when it is built, a `failure_threshold`
  or `half_open_max_calls` below 1, a negative or NaN `recovery_timeout`, and a
  `monitored_exceptions` that is not a tuple or list of exception classes. A
  config like that used to build and then misbehave: a threshold of 0 tripped on
  the first failure, no probes meant the circuit could never close, and a bare
  exception class raised `TypeError` at the first exception, far from the
  mistake. `ResilientRpcProxy` given both `circuit_breaker=` and `config=` raises
  `ValueError` where `config=` used to be ignored.
- `CircuitBreaker.last_state_change` moves when the circuit goes half-open: it is
  the end of the cooldown, whether or not a call has arrived to make the stored
  state follow. It kept the moment the circuit opened for as long as the circuit
  sat half-open. The `RpcCircuitOpenError` that `call_async` raises carries the
  same `details` (`name`, `state`, `failure_count`, `recovery_timeout`) as the one
  an awaited call raises, where it carried `service` and `state` only.
- `ResilienceExtension` finds the `@rate_limit` handlers by reading the service's
  class without evaluating it. A property on the service that raised (a pool that
  exists only after `start()`) used to fail startup from inside the extension,
  and one with a side effect ran once at setup. A handler assigned on an instance
  is not found, as the docstring now says.
- `RpcValidationError` refuses a `details` that is not a list of dicts with a `TypeError` that names the `message=` argument, where `RpcValidationError("username is required")` built a "validation failed" error holding that text as its details. A service's validation reply whose `details` is not a list of objects is read by the client as a reply without details rather than refused.
- `LifecycleManager.stop()` on a manager built without hooks now leaves `is_stopped` true; it ran to the end and left it false, so a supervisor polling the flag waited for ever. A stop whose teardown failed logs a warning that says how many steps failed instead of "stopped", and when several teardown steps fail the first is still the exception raised and each of the others is attached to it as a note, so the disconnect that leaks a socket is no longer visible only in the log.
- `PoolExtension`'s connections now use the service's `nats_inbox_prefix`, so a pooled `request` is answered on the prefix a permission-limited broker user may subscribe to instead of the default `_INBOX`, where it timed out. Each connection is named `<service>-pool-<n>` on the broker, and the pool's start gives up on a connection that is not made within the service's `connect_timeout`, raising `NatsError` as the service's own start does. `ServiceConfig.nats_connect_kwargs()` is the one place the credentials and the inbox prefix are read for a connection, and the service's own connection takes them from it.
- The sinks `setup_correlation_logging` adds keep a `service` the call bound. They
  used to relabel every line with the name they were configured for, so a worker
  that did `logger.bind(service="ingest")` was written as the configured name
  to the console, the text file and the JSON file. A line that bound no
  `service` still carries the configured name.
- A service that declares a stream with `retention="workqueue"` and two durable
  listeners whose subjects overlap on it is refused when it starts, with a
  `StreamDeclarationError` that names the stream, both subjects, both durables
  and both handlers. The server refuses the second durable of such a pair
  (`err_code=10100`, "filtered consumer not unique on workqueue stream"); that
  used to arrive as a bare broker error at the last step of startup, after
  `on_startup` and the timers had run.
- `stop()` called from inside a stop that is already running returns at once. The lifecycle lock is an
  `asyncio.Lock`, documented as "reentrant", and it is not: a teardown hook that called `stop()` again
  (an `on_shutdown` that tries to make shutdown idempotent, or an extension's `stop()`) waited for the
  lock its own stop was holding, and the shutdown never finished, with nothing to time it out. A
  supervised task that calls `stop()` while the running stop is draining it returns at once too,
  instead of waiting until `shutdown_timeout` kills it. A `stop()` from any other task still waits for
  the running one and finds the work done. `LifecycleManager.lock` now says it is not reentrant.
- A timer or cron job that is mid-run when the service stops now gets up to `shutdown_timeout` seconds to
  finish before it is cancelled. `Timer.stop()` cancelled the task at once, so a daily report or a billing
  run was interrupted in the middle of its work by an ordinary shutdown. The service stops all its timers
  together with `shutdown_timeout` as the grace, so a number of timers cost one grace between them, and a
  timer that is only waiting for its next firing is stopped at once. `Timer.stop()` takes `grace`
  (default `0`, cancel at once; `None`, wait for the run), so a direct call behaves as before. Worst-case
  shutdown time is now three `shutdown_timeout` periods (timers, drain, cancellation grace) instead of two,
  and `docs/api-reference.md` says so; `shutdown_timeout=None` waits for a run however long it takes.
- `ResilienceExtension` reports on `/health` under `resilience`: `rate_limits` with, per handler, how many dispatches its limit let through and how many it refused (and the totals), `tracked_keys` beside an in-memory limiter, and `circuits` with the destination, state, failure count and seconds in state of each `ResilientRpcProxy` the service declares. `/info` gains `resilience` with each handler's `calls`, `window` and where its key is read from (never a key value), the limiter class, and the default limit. The existing `rate_limiter` and `handler_rate_limiters` entries are unchanged. A payload that validation refuses before the limit is reached is in neither count.
- **Breaking**: `AuthConfig.secret_key` is a `SecretStr`. `repr`, `str`,
  `model_dump()` and `model_dump_json()` of an `AuthConfig`, and any log line
  that carries one, show `**********` where they used to print the key that
  signs every token. A plain `str` is still accepted, at construction and by
  assignment (`AuthConfig` now validates assignment, so `auth.config.secret_key =
  new_key` still rotates the key, and assigning an out-of-range
  `pbkdf2_iterations` is refused instead of silently kept). Code that read
  `config.secret_key` as a `str` reads `config.secret_key.get_secret_value()`.
- Spans from `OtelExtension` carry `messaging.system` (`nats`),
  `messaging.destination.name` (the subject) and `messaging.operation.type`
  (`process` for a handled `rpc`, `async_rpc`, `describe` or `event`, `send` for
  every outbound call and publish), so a tracing backend's messaging views and
  tail-sampling rules can select NATS traffic. A timer's span carries none of
  them. The `cliffracer.*` attributes, span names and span kinds are unchanged.
- A durable consumer name that the broker would refuse is refused where the handler is declared.
  `@listener(durable=...)` and `@validated_listener(durable=...)` raise `ConfigurationError` at import
  time for a name containing `.`, `*`, `>`, `/`, `\` or whitespace, a name over 255 characters, or one
  that is not a `str`, naming the handler and the offending character. Such a name used to travel
  through discovery unchecked and surface as `invalid consumer name` from the broker when the service
  connected, after its other subscriptions were live and naming neither the handler nor the decorator.
  Names the broker accepts (letters, digits, `-`, `_`, `:`, `,`, non-ASCII letters) are unchanged,
  and an empty or absent durable is still no durable.
- `MetricsExtension` keeps each kind's last 1000 latency samples in a
  `deque(maxlen=1000)`, so the window's bound is enforced by the buffer itself
  rather than by a trim run after every dispatch, and recording a sample no
  longer shifts the window. `/health` output is unchanged: `count`, `errors`,
  `rejected` and `latency_ms` report the same values. Code that read
  `MetricsExtension._latency[kind]` as a list now gets a `deque`.
- `OptimizedNATSConnection.connect()` closes the connections it had already opened when a later one
  fails, and then raises as before. It left them open for a caller that might never reach
  `close()`.
- **Behaviour change**: a `@rate_limit` key function that returns `None` raises `RateLimitKeyError`
  where its result was `str()`-ed into one shared bucket named `"None"`, which turned the limit for
  every request that lacked the partition field into a single global budget. Return a string, or
  raise to refuse the request.
- `start()` returns only after the broker has processed the service's subscriptions. It ended them
  with `nc.flush()`, and nats-py writes a flush's PING straight to the socket while a SUB waits in a
  pending buffer for another task to write, so the PING could reach the broker ahead of the SUBs it
  was meant to confirm. The PONG then proved nothing, and a request from another connection could
  arrive before the broker had read the subscription: `NoRespondersError` from a service that had
  just said it was up. Measured on a loaded host, 75 to 82 of 1800 starts (about 4%) answered a
  request sent straight after `start()` with no responders; with the fix, 0 of 1800. `start()` now
  flushes twice, the second PING being written after the buffered SUBs, so its PONG confirms them.
- The documentation of `StreamSpec.duplicate_window_seconds` says that `0` does not turn deduplication off: nats-server stores a zero window as its default and reports `120.0` back, so a stream declared with `0` has the two-minute window. Behaviour is unchanged.
- **Breaking**: `CliffracerService.call_rpc` raises `RpcNoRespondersError` when nothing is subscribed to the target's subject, where it let nats' own `NoRespondersError` through, which is not an `RpcError`: an `except RpcError` around the call missed it, and a circuit breaker never counted it, so a service that had gone away never opened its circuit. The standalone client already raised `RpcNoRespondersError`. Code that caught `nats.errors.NoRespondersError` around `call_rpc` catches `RpcNoRespondersError` or `RpcError` instead; the nats error is its `__cause__`.
- An extension argument is isolated item by item inside a list or a dict, as it already was inside a
  tuple, so `SharedDependency` works there: `Routers(routers=[SharedDependency(router)])` now binds
  the one `router`. A list or dict used to be deep-copied whole, so a `SharedDependency` inside one
  was never unwrapped: with an uncopyable payload the error named the type `list` and advised the
  wrapper the caller had already applied, and with a copyable one the extension received the wrapper
  object holding a private copy, silently losing the sharing. The snapshot taken when a specification
  is frozen no longer copies what such a wrapper shares either. Two consequences: an uncopyable item
  is now named (`'lock'`, not `'list'`), and a zero-argument callable inside a list or dict is called
  once per bound instance, as it is at the top level and in a tuple; wrap it in `SharedDependency`
  to pass the callable itself. Subclasses of `list` and `dict` are still deep-copied whole.
- `ServiceTestHarness` runs the service's own `on_startup` and `on_shutdown`. It used to run the
  extension hooks and mark the service running without calling either, so a service whose handlers
  depend on what `on_startup` builds (a pool, a cache, a client) passed under the harness and failed
  on its first request in production, and an `on_shutdown` that releases a resource never ran.
  `setup()` now runs `on_startup` between handler discovery and extension `start()`, and
  `teardown()` runs `on_shutdown` before the extensions stop, only for an `on_startup` that
  returned, as a live start and stop do. A hook that raises ends the setup with its error, winds down
  what had started, and leaves the harness spent. A service whose `on_startup` needs a live broker now
  fails under the harness the way it would without one; stub it in the test.
- **Behaviour change**: `describe` refuses the two handler declarations discovery refuses and it
  had skipped: a handler decorated on an underscore-prefixed name, and one named for a
  `CliffracerService` method such as `health_check`. `cliffracer-generate-client --class` reports
  either as exit 4 with the reason, where an underscored `@rpc` was reported as "no @rpc handler
  found". Given a config, a listener's `queue_group` is the queue the runtime subscribes with, the
  durable under the config's `subject_prefix` (`prod_push_worker`); it was the declared name, so a
  prefixed service described a queue it does not use. Without a config it is the declared name.
- A generated client reports a reply that has `success: false` and no `error` as a failure that
  gave no reason, naming `{namespace}.{service}.rpc.{method}`, the subject the request used. It
  said the reply "carries no success key", which it does, and named `{service}.{method}`, which is
  not a subject. The client also labels each request `Content-Type: application/json`, which it
  sent without a label, so a service reads the header the documentation says it reads instead of
  sniffing the bytes; a `Content-Type` the caller sets in `headers=` is left as given.
- **Behaviour change**: `cliffracer.generate_client` exports `emit` and `CannotEmit` only.
  `annotation_text` and `imports_for` are no longer published there, because `annotation_text`
  spells a model differently from the import `emit` writes when it is not given the aliases `emit`
  computes; both remain in `cliffracer.generate_client.emitter`. `from cliffracer import
  PackageNotFoundError`, an importlib exception that leaked into the package namespace, no longer
  resolves. `cliffracer-generate-client` closes its broker connection when it finishes with the
  describe request, where it drained it, so a connection that had already dropped no longer
  replaces the error the exit code is chosen from.
- **Behaviour change**: `await ServiceOrchestrator.stop()` returns once `run()` has finished and
  every service has stopped, where it returned having only set the shutdown event; before `run()`
  has been entered it still only records the request. `ServiceRunner.run()` and
  `ServiceOrchestrator.run()` no longer install process-wide SIGTERM and SIGINT handlers, which
  `run_forever()` installs, so a runner awaited inside a larger program leaves that program's
  signals alone; the runners an orchestrator drives install none, so its own handler is the one in
  effect. A program that awaits `run()` under its own event loop must call `run_forever()`
  instead, or install its own handlers, since `run()` no longer does.
  `ServiceTestHarness.describe()` raises `RuntimeError` when the service sent no reply or answered
  with a failure envelope, where it returned `{}` or the envelope.
- **Behaviour change**: `validate_password`, and so `SimpleAuthService.create_user`, refuses a password whose every character is whitespace (`str.isspace`: spaces, tabs, newlines and the Unicode spaces such as U+00A0 and U+3000) with `Password must contain at least one character that is not whitespace`, after the type and length checks. Eight spaces were accepted, and a user was created who could log in with them. The password is still returned and stored unchanged, surrounding whitespace included, and no composition rule is added. A password of zero-width spaces (U+200B) is invisible but is not whitespace and still passes; refusing it would be a rule about invisible characters. A user already stored with such a password is unaffected: `authenticate` does not validate the password again.
- `ResilienceExtension` finds a handler's `@rate_limit` by the handler's name only. It also kept a map
  from the subjects a limited listener declared to its limit, and used it when the name had none,
  so another handler that received the same subject (an unlimited `orders.*` listener beside a limited
  `orders.created` one) was refused on the limited handler's budget. Every dispatch carries the
  handler's name, so the map was never needed for the handler it was written for. The map and its
  private attribute `_event_rate_limits` are removed, and so is the second fallback that cut an RPC
  subject (`<service>.rpc.<name>`) apart to find a limit, which RPC dispatch never needed either.
- **Behaviour change**: an RPC payload that is a `Mapping` but not a `dict` (a frozen copy another extension left, a `UserDict`) is validated as the object it is, and served when it is valid. It was refused with `validation failed: payload must be an object`, a reply with no field errors unlike every other refusal, and when it carried a `correlation_id` it was refused for that id as an extra field, which a `dict` was not. That refusal is removed; a payload that is not an object is refused with its field errors, as before. Payload validation is now one step, `cliffracer.core.validation.validate_payload`, used by an RPC and by all three event paths (a `@validated_listener`'s schema, a typed listener's one model, and its parameters) where each had its own copy of the rule for removing the message's `correlation_id`. The wire only ever delivers a `dict`, so nothing arriving from the broker changes.
- **API Change**: the extension entrypoint machinery is removed. `cliffracer.entrypoint` (and
  `cliffracer.core.extension.entrypoint`), `Extension.entrypoint_kinds()`, the container's kind map and
  the per-kind binders in discovery existed so a transport extension could register route and
  websocket entrypoints; the last one left with `cliffracer-http`, and nothing in the repository
  declared a kind after that. An extension that still defines `entrypoint_kinds` is built as before and
  the method is never called, and a service template no longer refuses an extension for defining it.
  `Extension.bind` and the `_origin` link are unchanged.
- `LoggingConfig.configure` installs its sinks before it removes the ones that were there, and a
  sink that cannot be opened (a log directory that exists but cannot be written, a `rotation`,
  `retention` or `compression` that loguru refuses) now leaves the process logging as it was. It
  used to remove every handler first, so the failure left the host's sinks gone and nothing of the
  service's installed, with every later line written to nobody; with `replace_existing=False` the
  sinks added before the failing one stayed installed. A failed call raises what loguru raises and
  removes only the sinks it had added. A successful call is unchanged: with `replace_existing=True`
  the handlers that existed before the call are removed, and the ids it returns are its own.
- `register_broadcast_handler` reads the handler's own signature and records its event spec under
  the subject it registers, as discovery does for a declared listener. Dispatch used to find a
  spec for it by the handler's name, in an index of the discovered methods, so a function that
  merely shared a name with a `@listener` method was called with that method's arguments and never
  ran, with the outcome still OK. A typed handler added at runtime now has its payload validated
  like a declared one: a payload it does not declare is dead-lettered as invalid, where it used to
  be called with the raw values. A handler with no annotations is still accepted and still called
  with the payload as keyword arguments. `ServiceRegistry.event_specs`, the name-keyed index, is
  removed; `event_specs_by_subject` holds every spec.
- **Behaviour change**: assigning to the name of an `RpcProxy` or `ResilientRpcProxy` on a service
  instance (`self.inventory = something`) raises an `AttributeError` naming the attribute. It used
  to succeed, and the instance attribute then hid the proxy for that instance without a word. To
  substitute a proxy, for a test double or otherwise, replace the attribute on the class.
- **Behaviour change**: `describe` no longer takes `**kwargs`, so a misspelt keyword
  (`versoin=`, `confg=`) raises a `TypeError` where it was swallowed and the default used.
  It no longer reads `service_name` or `version` off the class, so a property of either name
  cannot make the description unserialisable. A described stream carries
  `duplicate_window_seconds` and no longer a `max_consumers` field that nothing set;
  `EventListenerDescription` no longer takes `subject=`, `handler=`, `is_validated=`,
  `is_broadcast=` or `model_schema_hash=` as constructor arguments, and still has each as a
  read-only attribute derived from the fields it keeps. A validated listener is described as
  `pull=False`, which is what discovery binds it as.
- **Behaviour change**: `SimpleAuthService.verify_password` refuses a stored PBKDF2 record made at fewer than `MIN_PBKDF2_ITERATIONS` (1,000) iterations, where a record of one iteration verified. It returns `False` and logs a warning that names the record's count and the floor, and the login that follows fails: that user's password has to be set again (rehash it with `hash_password` and store the record). `AuthConfig.pbkdf2_iterations` is bounded by the same floor, so a configuration can no longer create a record the verifier refuses; it was `1` or more and is now `1,000` or more. A record at the floor still verifies, and a record between the floor and the configured count is still written again at the configured count at the next login. A refused record spends one PBKDF2 at the configured count, as an unregistered name does, so the time a login takes does not say which users hold one.
- **Behaviour change**: `description_hash` is now the hash of the whole description, less what the
  configuration decides, where it was taken over the methods alone. A change to a listener's
  payload model, to the subject a listener reads, or to an output's contract moves it, so a client
  generated before the change no longer looks current by its `DESCRIPTION_HASH`. The streams and a
  listener's effective subject, durable and queue group are left out, so the hash is the same from
  `describe(cls)` and from a running service. Every service's `description_hash` takes a new value
  once, and a client regenerated after upgrading records it; the per-method `signature_hash` that
  `ServiceClient.verify` compares is unchanged, so no client reports drift for the upgrade.
- **Breaking**: `start()` raises `ServiceLifecycleError` ("was stopped while it was starting") when the service
  was stopped from inside its own startup, for example by an `on_startup` that calls `stop()`. It
  used to return normally as if the service were up, while a stop from another task made it raise
  `CancelledError`; the service ended stopped either way. `ServiceRunner` now logs the stop as a
  crashed start instead of counting a successful start and then reporting a NATS connection that
  "closed unexpectedly". A stop from another task still cancels `start()`, and a caller that cancels
  `start()` itself (a startup timeout) still sees its cancellation.
- **Behaviour change**: `ServiceConfig` refuses a `nats_url` that nats-py cannot connect to and a `health_host` that cannot be bound, when the config is built or assigned, naming the field and the reason. These were accepted and failed late: a wrong scheme (`http://broker`, `NATS://broker:4222`), two servers in one string, a space or a bad port made the connection wait out `connect_timeout` and report "no answer", as if the broker were down. A bare `host` or `host:port`, IPv6, `tls`, `ws` and `wss` still work, `health_host=""` still means every interface, and a name that is only unknown is not refused, since the check is syntactic. A refused `nats_url` is reported with any password in it replaced, in the message, `errors()`, `json()` and the traceback. `redact_nats_url` now lives in `cliffracer.core.endpoints`, and is still importable from `cliffracer.core.connection`.
- `describe` refuses two more classes that the service refuses to start: a listener that declares
  neither a durable nor `fanout=True`, and two subjects sharing one durable name. Given a config
  that leaves JetStream off it also refuses a durable listener that is not `fanout=True`, whose
  durable is inert. **Behaviour change** for that config, including the answer to
  `{service}.describe`: the declared streams are not listed, since none is created, and a
  listener carries no `durable` or `queue_group`, since its subscription has neither. With
  JetStream on, or with no config, the description is as before.
- **Behaviour change**: a timer or cron firing that an extension refuses with `RejectMessage` or
  `RetryMessage` is logged at WARNING without a traceback, and is no longer counted in
  `error_count`, in the error rate or among the executions. It is counted in a new
  `refusal_count`, with the reason in `last_refusal` (`last_error` stays empty), and in a
  `timer_<method>_refusals` metric. A distributed cron firing that was refused is recorded in its
  interval record with status `refused` and a `refusal` reason, where it would otherwise read as
  `completed`; a firing that fails is still `failed`. A refusal was logged at ERROR with a
  traceback and counted as a fault, so an extension that turned a firing away on purpose read as a
  failing timer.
- **Behaviour change**: `/health` and `/ready` carry `dead_letters_lost`, the number of dead letters the service could not publish since it started, whether the message was invalid, undecodable or out of deliveries. It is `0` on a healthy service and never changes `status`; the delivery is still terminated, and the error log line still carries the cause and the payload. The three dead-letter handlers of `DeadLetterPublisher` return `False` for a dead letter that was wanted and not published, and `True` otherwise. An extension may no longer be named `dead_letters_lost`, which joins the other names the payload writes.
- **Fix**: a distributed cron timer that finds a running lease whose `started_at` is missing, not a
  number, not finite, or more than a `lease_ttl` in the future now logs a WARNING naming the lease
  and the reason, and runs the interval. A missing value was read as 0, so the lease was
  infinitely old and was run over in silence; `Infinity` or a date far ahead was read as
  infinitely fresh, so every interval was skipped until the bucket's own expiry removed the
  record; a value that was not a number was reported with Python's own words.
- **Behaviour change**: an `@rpc` handler or a `@validated_listener` whose contract holds `inf`,
  `-inf` or `nan` is refused when the service starts, and by `describe`, with an `UntypedHandler`
  that names the handler and the parameter or the model: a parameter default (`cap: float =
  float("inf")`), a bound (`Field(ge=float("-inf"))`), or a default inside a model it takes or
  returns. The description is published as JSON, which has no such values, and `canonical` wrote
  them as `Infinity` and `NaN`, so a parser in another language rejected the whole description.
  `canonical` now refuses them as well. Write `cap: float | None = None` and treat `None` as no
  limit.
  The settings schema a template is registered with is serialised the same way, so
  `TemplateCatalog.register` refuses a settings model whose JSON schema holds one
  (`cap: float = float("inf")`) with a `TemplateError` naming the template and its revision.
- **Behaviour change**: a caller turned away by `@requires_auth`, `@requires_roles` or `@requires_permissions` gets `code: "refused"` with `refused: forbidden` (`refused: unauthenticated` when there is no identity), where it got the generic internal-error reply, indistinguishable from the service breaking. The roles and permissions the handler requires are not on the wire under any setting of `expose_internal_errors`; they are in one warning line in the service's log, with no traceback, where each denial used to log an error with a full traceback. A denied fire-and-forget request or event is refused the same way, so a durable listener acknowledges it and does not redeliver it, and the metrics extension counts it as a refusal. The decorators still raise `AuthenticationError` / `AuthorizationError` to a direct caller and to a `@timer` firing.
- **Behaviour change**: `KvExtension(nc=...)` now wins over the service's own `js`; it lost to it,
  so a declaration that named a connection was ignored on a service running with
  `jetstream_enabled`. The order is an explicit `js`, an explicit `nc`, the service's `js`, then a
  context built from the service's connection, which is what a service with the default
  `jetstream_enabled=False` uses. `get(..., as_type=dict)` and `as_type=list` raise `ValueError`
  when the stored value is another JSON type, `null` included (a stored `123` came back as the
  integer, whatever the annotation said), and the error for a missing connection no longer tells
  the reader to set `jetstream_enabled`.
- **API Change**: `cliffracer.cli.main.flag_overrides_from_args` is removed. Nothing in the CLI called it: `build_orchestrator` builds its overrides from the parsed `--nats-url` itself. `Extension.bind` no longer re-assigns `_spec_frozen = False` on the instance it returns, which `Extension.__new__` had already set.
- **Removed**: `KvExtension.setup()` no longer reads a `kv_buckets` attribute off the service
  config, and `get()` no longer checks for a `DEL` operation on the entry. `ServiceConfig` has no
  `kv_buckets` field, so only a user-defined config subclass could reach the first, and it was
  never documented; declare buckets on the extension. The second could not run: nats-py raises
  for a deleted key before an entry is returned.
- `EventListenerDescription` no longer has a `stream` field. `from_dict` read it and `to_dict` never
  wrote it, and `describe` never set it, so a value given to it was dropped on the way to the wire
  and nothing read it. A description read from the wire carries what the description that was
  serialised carried.
- `describe(cls, config=...)` refuses a `cross_namespace` listener on a service whose config has no
  namespace, with the `ConfigurationError` that starting the service raises. It described the
  listener as if the service would start, and `cliffracer-generate-client` could emit a client for
  a service that never receives the event. With no config given the check is not applied, as for
  the other rules that need one.
- **Fix**: `KvExtension.get_bucket()` and `get_object_store()` called at the same moment for a
  bucket or store that is not open yet provision it once and hand every caller the same handle.
  Each caller used to miss the cache, call `create_key_value` or `create_object_store` itself and
  keep a handle of its own; with two different declarations for one name the loser got the
  broker's "already in use with a different configuration" error.
- **Behaviour change**: a `@dependency` or `add_dependency` detail key named `ok`, `error` or `latency_ms` raises a `ConfigurationError` naming the dependency when it is declared. The health payload writes those three itself, so a declared key of that name was silently replaced by the framework's value and its own dropped.
- The class the framework's argument checks raise (`cliffracer.core.validation.ValidationError`, from `validate_timeout` and its siblings) now derives from the exported `cliffracer.ValidationError` and is still a `ValueError`, so the `except ValidationError` the API reference shows catches an argument-check failure. A pydantic model or `ServiceConfig` that fails validation raises pydantic's own `ValidationError`, as before, and the API reference and README say so.
- **Behaviour change**: an `@rpc` or `@async_rpc` handler with a parameter named `namespace` is refused when the service starts, and by `describe`, with an error naming the clash: `call_rpc`, `call_async` and `call_rpc_no_wait` take `namespace=` as the routing namespace, so a caller using `RpcProxy` could not pass the argument and got a `TypeError` that named an internal parameter. `service` and `method` are positional-only in those three methods, so a remote argument with either name now reaches the wire instead of colliding.
- **Behaviour change**: `describe`, and so `cliffracer-generate-client --class`, refuses a class whose event handlers the service would refuse to start, where it described them: an event, validated or broadcast handler that is not fully annotated raises `UntypedHandler`, two listeners on one subject, a pull listener with no durable or with fanout raise `ConfigurationError`, and, when a `config` is given, a pull listener without JetStream or a durable together with fanout on a JetStream service do too. The generator exits 4 with the message for either error.
- **Behaviour change**: `AuthConfig` refuses a field it does not have with a `ValidationError` naming it, where it used to ignore it, so a misspelt `pbkdf2_iteration` no longer leaves the default hash cost in place. A caller still passing `enable_auth=` now gets an error saying that declaring `AuthExtension` is what turns authentication on.
- `RpcProxy` and `ResilientRpcProxy` cache the proxy they give a service by the service's identity. Keyed by the service itself, a service whose class defines `__eq__` without `__hash__` (every `@dataclass` service) raised `TypeError: unhashable type` when it read a proxy attribute, and two services that compared equal shared one proxy, so the second sent its calls, and used its circuit breaker, through the first.
- **Fix**: a service whose extension named `kv` is not a `KvExtension` and cannot serve a bucket
  is refused when it declares `@cron(distributed=True)`, at startup, with the message that no
  `KvExtension` is registered. It was accepted on the strength of the name, and the first tick
  failed with an `AttributeError` on `get_bucket`. A stand-in named `kv` that has `get_bucket` is
  still accepted without an explicit `kv_extension=`.
- **Fix**: a service whose handler discovery is refused, for two subjects on one durable say,
  no longer leaves the refused handlers in its registry. `get_service_info()` and the
  description listed them for a service that was never allowed to start; the registry is put
  back as it was before the attempt, and the refusal is still raised on every later call.
- `SimpleAuthService.create_user` copies the roles and permissions it is given, where it kept the caller's set when it was not empty, so one default set passed to several users no longer makes them share it and `add_role` no longer changes the caller's constant. The module-level `_auth_service`, which nothing set or read, is removed.
- **Behaviour change**: `AuthMiddleware` sets the auth context for the request, valid token or none, and restores the one that was set before it, where it used to clear it afterwards and so destroyed any context an outer caller had established. An anonymous request no longer inherits an outer identity. The `Authorization` header name and the `Bearer` scheme are matched without regard to case, as `AuthExtension` matches its header.
- **Behaviour change**: `SimpleAuthService.revoke_token` revokes the token's whole chain, the tokens refreshed from it and the ones it was refreshed from, where it used to revoke only the one `jti` and leave a token already refreshed from it valid and refreshable. Tokens now carry a `cid` claim, set to a login's own `jti` and carried on by every refresh; a token signed without one is treated as a chain named by its `jti`. A chain's revocation is forgotten after one token lifetime.
- **Behaviour change**: a service that declares `KvExtension` is unhealthy while the extension
  cannot reach its buckets. Starting the extension registers a dependency, named for the
  extension's attribute (`kv` by default), whose probe checks that the NATS client is connected
  and asks the broker for the status of every open bucket and object store, bounded at two
  seconds across all of them; a deleted bucket, a lost stream or a closed connection fails it.
  The `connected` field in the extension's `/health` details now reads the client's connection
  state; it was true from the first bucket opened until `stop()`, whatever the connection did.
- **Behaviour change**: when a declared bucket or object store already exists, `KvExtension`
  reads its configuration back and logs a WARNING naming each declared option that differs from
  it (`ttl`, `history`, `max_bytes`, `max_value_size`, `replicas`, `storage`, `description` and
  `direct` for a bucket; the matching ones for an object store). The existing bucket is not
  changed, as before. A declaration that sets none of those options costs no extra read.
- **Fix**: `AuthExtension` awaits the result of an issuer's `validate_token` when it is awaitable. An issuer
  written as `async def` (a key fetch, a remote introspection) used to refuse every request, with a
  never-awaited coroutine warning and an error about `'coroutine'`.
- **Behaviour change**: `AuthConfig` no longer has an `enable_auth` field. It was documented as enabling
  authentication and read by nothing, so setting it to `False` changed nothing; declaring `AuthExtension` is what
  turns authentication on. Passing it raises a `ValidationError` that says so.
- **Fix**: `SimpleAuthService.authenticate` runs the same PBKDF2 for a username that is not registered as for
  one that is, so the time a login takes no longer tells a caller which usernames exist.
- **Behaviour change**: `AuthConfig.pbkdf2_iterations` must be between 1 and 10,000,000, the most
  `verify_password` will run for a stored record. A count above that created users and then refused every
  one of their passwords at login, and 0 failed inside `create_user`; both are now refused when the config is
  built.
- **Fix**: a stored PBKDF2 record made at fewer iterations than `AuthConfig.pbkdf2_iterations` is written again
  at the configured count the next time its user logs in successfully, as a legacy-form record already was. It
  still verifies in the meantime.
- **Fix**: `SimpleAuthService.create_user` numbers users from a counter instead of from the size of the store,
  so removing a record no longer lets the next user take an id that a live user holds.
- **Behaviour change**: a `@dependency` or `add_dependency` timeout that is zero, negative, not finite or not a number raises a `ConfigurationError` naming the dependency when it is declared. It was accepted, and reported the dependency unhealthy for good as "timed out after 0s" for a probe that never ran. `Dependency` is hashable, and compares by identity; its `detail` is a read-only copy of the mapping it was built from. `failed_dependencies` documents that its names come in name order, not declaration order, and `dependency()` that `detail` is published on `/health` as given.
- Every Cliffracer exception survives `pickle`, so an error crosses a process boundary intact: `ClientOutOfDateError` raised a `TypeError` on unpickling, and `RpcRefusedError` and `RpcValidationError` came back with the wrong `args` (a doubled `refused: ` prefix, the default message). `ErrorHandler(reraise=False)` logs the failure it suppresses at WARNING, with the operation, the exception type and its message, where it logged nothing. `wrap_exception` no longer copies the original's `args` into `details`, so the text of an exception that carries a connection string appears once in the wrapped message instead of two or three times; the original, with its arguments, is still `__cause__`.
- **API Change**: `LifecycleManager.active_tasks` (and the container's `_active_tasks`) returns a `frozenset` snapshot of the supervised tasks instead of the live set, so a caller cannot add to it or remove from it; read it again to see a later state. `drain_active_tasks` documents that a `timeout` of `None`, or one that is not positive, waits without a deadline and cancels nothing, which is what `shutdown_timeout=None` asks for. The manager no longer records the event loop it never read.
- **Behaviour change**: the NATS log sink counts what it publishes, loses and has in flight, and
  `LoggingExtension.health_details()` reports it as `nats_sink` once the sink has started; it lets at
  most `max_pending` publishes (1000 by default) be in flight and drops, and counts, a record that
  arrives beyond that, where a slow broker used to grow the backlog without limit. A loop that has
  closed no longer leaves one un-awaited coroutine and one stderr line per record: the record is
  counted as dropped, and a repeated error reaches stderr once until a publish succeeds.
- **Behaviour change**: a failure that escapes an RPC request's dispatch (a reply that could not be
  sent, a bug in dispatch) is now logged at ERROR with its type; it was logged at DEBUG.
  `MessageDispatcher.container`, which nothing read, is gone, and `cliffracer.core.dispatcher.__all__`
  no longer lists the private names `_HandlerMeta` and `_JetStreamHeartbeat` (`_JetStreamHeartbeat` is
  still importable from `cliffracer.core.container` and `cliffracer.core.dispatch`).
- **Behaviour change**: a pull consumer whose unsubscribe fails when its loop exits now logs a
  WARNING naming the durable (it was silent), and `report_consumer_drift` warns when it cannot read a
  consumer even with no pattern (it logged at DEBUG). `OutboundDispatcher` is constructed from
  `(config, pipeline)`: its `connection_provider` and `logger` arguments, which it never read, are
  gone. `pull_once` recognises a fetch timeout by class only; an exception that merely shares the
  name `TimeoutError` is no longer swallowed.
- **Fix**: `KvExtension.get()` on a key stored with an empty value returns `""` (`b""` with
  `as_type=bytes`) instead of `default`: nats-py reports an empty payload as `None`, which was
  read as the key's absence. How a stored string reads back with no `as_type` (`"123"` as `123`,
  `"null"` as `None`) is unchanged and is now stated in the README and the `get()` docstring,
  with `as_type=str` for the exact text.
- **Breaking**: `KvExtension.delete()` and `purge()` return `None`. They returned `True` every
  time, and the "`False` if the key was not found" their docstrings promised never happened:
  each writes a delete or purge marker whether or not the key exists. A caller that tested the
  result reads the key instead. The `KvError` docstring now says it covers the errors the
  package raises itself; nats-py's errors, such as a stale revision, propagate unchanged, as
  they always did.
- `/health`'s `features` block counts a `@broadcast` handler once: `events` counts the listeners that are not broadcasts, where a service with one broadcast and no listeners reported `events: 1, broadcasts: 1`. `ServiceRegistry.entrypoints`, written at discovery and read by nothing, is gone, and `clear()` resets every field the registry declares instead of a hand-kept list.
- `copy.copy` and `copy.deepcopy` work on an `ExtensionSetupContext`: its attribute delegation to the service no longer recurses for ever on an instance whose fields are not restored yet, and does not delegate protocol names such as `__setstate__`.
- **Behaviour change**: `@idempotent` on a generator or async generator function raises a `ConfigurationError` where it was accepted and never attached a key, because calling a generator only builds it. `format_nats_msg_id` measures its 128-character bound in UTF-8 bytes, so a key or subject of non-ASCII text that was over the documented size is hashed; ASCII ids are unchanged.
- `from cliffracer import timer` has the signature of the decorator that builds it, naming `headers` and `token_factory` (the bearer token a timer firing sends, which an `AuthExtension` with `allow_timers=False` requires) where they were hidden in `**kwargs`. The two decorators are one implementation.
- **Behaviour change**: `/live` answers 503 with status `unknown` for a host that has neither a liveness method nor a `_running` flag, where it answered 200 `healthy`, and reads an `is_live()` that returns a boolean instead of failing with a 500. A `CliffracerService` is unaffected.
- **API Change**: the validation extension reads the registry the dispatcher reads and refuses a named RPC handler that has no spec, where it returned and let the raw payload through. `validate_timeout` honours an explicit `min_ms=0` or `max_ms=0` instead of replacing it with the default. `NumericBounds` and `StringLimits` lose the constants nothing read (`DEFAULT_TIMEOUT_MS`, the limit and concurrency defaults, the SQL and identifier lengths), and `validation.SUPPORTED_FORMATS` is gone.
- **Behaviour change**: `publish_event` and `broadcast_message` on a service with `jetstream_enabled` that has no JetStream context, because it is not connected, raise `ServiceLifecycleError`. They published on core NATS, unacknowledged, to a subject no declared stream had been checked against. A service with `jetstream_enabled` off publishes as before.
- **Fix**: `ensure_streams` raises `StreamDeclarationError` naming every stream name that is declared more than once in one list, before it reads or changes the broker. It added the name twice, with different subject sets, and left the broker to settle which one stood.
- `BatchProcessor` processes the items of a batch in one call when they were added with the same
  method of the same object. Every access to `service.handle` builds a new bound-method object, and
  items were grouped by that object's identity, so a batch of N items passed as a method was
  delivered as N calls of one item while the statistics reported one batch of N. Items added with
  the same function are unchanged, and two different closures or partials are still two processors.
- **Behaviour change**: `ensure_streams` now checks every declared stream, against the broker
  and against the others, before it creates or updates any of them. A conflict in a later
  declaration (a subject overlap, or a differing stream already on the broker) used to leave the
  earlier streams already created on the broker, holding their subject claims for a service that
  never came up, and it reported only the first conflict. Now nothing is created or changed
  unless every declaration is accepted, and the conflicts are reported together in one
  `StreamDeclarationError`. A single conflict keeps its message. A stream added by someone else
  between the check and the add can still fail the add.
- **Fix**: a dependency probe registered with `add_dependency` is no longer erased from
  `service.container.registry.dependencies` when `start()` runs handler discovery. Discovery
  used to replace the registry's list with the `@dependency` markers alone, so the registry
  and the list `/health` reads diverged at the first start. Discovery now adds to the list,
  and a name that is both declared and added at runtime keeps the runtime probe in both.
- **API Change**: an RPC refusal reply now adds `retry_after` (seconds) when the refusal carries
  one, as a `RetryMessage` such as a rate limit does, and `details` when it carries a non-empty
  dict (a rate limit's limit, window and key fingerprint), so a client can back off instead of
  retrying blind. The reply's `success`, `error` and `code` are unchanged, and a refusal with
  neither is exactly the reply it was. A refusal that is a crashed hook (`code` `internal`) adds
  nothing.
- **Behaviour change**: `signature_hash` and `description_hash` now carry the `allow_inf_nan` and
  `coerce_numbers_to_str` field constraints, which change what a parameter accepts and were
  dropped from the type, so two versions of a method differing only in one of them hashed the same
  and a generated client could not see the change. A service that uses either constraint gets a new
  hash for that method, and a client generated against the old one reports drift once.
- **Behaviour change**: a service whose handler discovery was refused (pull without a durable, a
  duplicate subject, an undeclared fanout, an untyped handler) is now refused on every later
  `start()` on the same instance, with the same error. The first refusal marked discovery done, so a
  retry returned normally with the refused handler registered and went on to subscribe it.
- **Behaviour change**: a JetStream message whose payload cannot be built into the
  handler's model, such as a MessagePack map with a bytes key, is dead-lettered and
  terminated on its first delivery. The model was built by splatting the payload as
  keyword arguments, where a bytes key is a `TypeError` that no validation handler
  caught, so the message was NAKed and redelivered until the delivery limit and then
  dead-lettered with "keywords must be strings" as its reason. The payload is now
  validated with `model_validate`, which reports the same input as a validation
  error. A model's own `__init__` is no longer run to build the payload model.
- **Behaviour change**: handler discovery now logs a WARNING when a subclass overrides a method
  its base decorated as a handler (`@rpc`, `@listener`, `@validated_listener`, `@broadcast`,
  `@timer` or an extension entrypoint) without decorating the override. The override registers
  nothing, so the inherited handler was silently gone; the warning names the class, the base and
  the handler and says to put the decorator on the override to keep it. Nothing is refused and
  nothing registers differently.
- **Behaviour change**: the dispatch layer (RPC, event, JetStream and dead-letter
  handling, extension hooks, outbound sends) logs through the service's logger as it
  is when each line is written. It took a copy of the logger when the service was
  built, so a service that replaced `self.logger` afterwards, as a logging mixin
  does after calling `super().__init__()`, sent connection and lifecycle lines to the
  new logger and every dispatch line, such as RPC errors, dead-letter warnings and
  hook failures, to the old one. Assigning `dispatcher.logger` is followed too.
- **Breaking**: a listener with `cross_namespace=True` on a service that has no `namespace` is
  now refused when handler discovery runs, with a `ConfigurationError` that names the handler and
  says to set a namespace or drop `cross_namespace=True`. It subscribed to `*.<pattern>`, which
  matches a publisher in any namespace and never one with no namespace, so on a service with
  none it started, held a subscription and received nothing. Services with a namespace are
  unaffected.
- **Behaviour change**: a JetStream handler failure that is going to be retried is
  logged at WARNING when it is NAKed: the handler, the subject, the delivery
  against the limit and which limit decided it, the exception, and the delay the
  redelivery waits. An extension that defers a message with `RetryMessage` is
  logged the same way, with its reason. The retry used to leave no line at all,
  because the handler's exception was re-raised unlogged to be classified, so a
  redelivery storm was invisible until the dead-letter record at the limit.
- **Breaking**: `start()` refuses a service with a durable listener, push or pull,
  whose subject no stream in `jetstream_streams` carries, with a
  `StreamDeclarationError` naming each subject, its durable, its handler and the
  declared claims. This runs before the broker is connected. It used to fail at the
  last step of startup with the server's "not found", after `on_startup` and the
  timers had run, naming none of those; and it started anyway when another service
  on the broker happened to have a stream for the subject. A listener on a subject
  only another service's stream covers must declare the stream it reads in
  `jetstream_streams`.
- **Behaviour change**: a dead-letter record for a message that failed at the delivery
  limit carries the correlation id the failing handler ran under, so the log lines of
  that delivery and the record can be joined. It used to carry a freshly generated id,
  because the dispatch's context was already reset when the record was written. A
  message that cannot be decoded, and one dead-lettered at the limit with no id of its
  own, no longer take their id from the ambient context, which held whatever the
  service's starting task had set and stamped every such message with the same
  unrelated id. The wire header, then the payload, are still read first.
- **Fix**: publishing a dead letter with no connection (or JetStream active but no JetStream
  context) now raises a `ConnectionError` that names the missing piece, instead of a bare
  `assert` that vanished under `python -O` and, in the three handlers that swallow a failed
  dead-letter publish, logged an empty reason (`... ()`). Those log lines now carry the
  exception's type as well as its text.
- **Behaviour change**: freezing an extension specification copies its declaration arguments, so
  changing a declared collection in place afterwards, through the specification's own attribute or the
  caller's list, no longer reaches a service instance bound later. Previously `spec.items.append(...)`
  on a frozen specification raised nothing and rewrote what every later instance was built from.
  Factories (callables that take no arguments), `SharedDependency` values and nested extensions are
  not copied or called when freezing; a callable that takes arguments is a value and is copied. An
  argument that cannot be copied still fails when an instance is bound.
- **Behaviour change**: `PoolExtension` reconnects as its service does. `reconnect_time_wait` and
  `max_reconnect_attempts` default to the service's `ServiceConfig` values, where they were fixed at 1
  second and 10 attempts. With the service's default of unlimited attempts, a broker outage of about
  fifteen seconds no longer leaves every pooled client closed for good while the service reconnects.
  A value passed to `PoolExtension` still replaces the service's.
- **API Change**: `@cron(distributed=True)` finds the service's `KvExtension` by
  type, whatever attribute it is declared under. It used to look only for an
  attribute or extension named `kv`, and a service that declared the extension
  under any other name was refused at startup with "no KvExtension is
  registered", though one was. A service with several `KvExtension`s uses the one
  named `kv`; with several and none named `kv` it is refused naming them. The
  startup check is now made by the timer itself rather than by core discovery.
- **Breaking**: `@timer` and `Timer` refuse, with `ConfigurationError`, an
  `interval` that is zero, negative, not finite, a bool or not a number, and a
  `max_drift` or `error_backoff` that is negative, not finite, a bool or not a
  number, where the decorator is applied. A zero interval ran the method
  back to back with no sleep, a negative one logged a drift warning on every
  iteration, and a string passed decoration and failed inside the loop with a
  `TypeError` that was logged and retried forever.
- **Behaviour change**: `@requires_roles()` and `@requires_permissions()` with no names, and either with an argument
  that is not a string (a list of names, say), raise `ConfigurationError` where the decorator is applied. A
  decorator with no names decorated cleanly and then refused every caller; a list failed on the first call.
  The docstrings and the auth README now say that several names mean any one of them.
- **Behaviour change**: a dependency probe's `timeout` is now a bound the health call keeps. The
  probe runs as a task and is cancelled, not awaited, when the timeout passes, so a probe slow to
  honour its cancellation no longer holds `/health` past it (a new probe is not started for that
  dependency until the old one has finished). A `TimeoutError` the probe raises itself is
  recorded as the probe's failure, with the probe's own words subject to the usual exposure
  policy, instead of as "timed out after Ns" naming a budget nothing waited for. A probe that
  blocks the event loop cannot be interrupted, but it is now reported failed
  (`exceeded its Ns timeout`) instead of `ok: true`.
- **Breaking**: a service whose decorated handler (`@timer`, `@rpc`, `@listener`
  and the rest) is named for a method `CliffracerService` already defines, such as
  `health_check`, is refused at startup with a `ConfigurationError` naming both.
  The decorator only marks the method, so the handler replaced the framework's for
  the whole service: a `@timer` named `health_check` made `/health` and `/ready`
  answer 500 on a healthy service. The `@timer` docstring and the timer example
  that suggested that name now use another.
- **Behaviour change**: `LoggingConfig.configure` merges `service` into loguru's global `extra`
  instead of replacing it, so context the host set stays on every record, and it returns the ids
  of the sinks it added; `replace_existing=False` adds them without removing what is installed.
  A `ContextualLogger` writes the `service_name` it was given on every line, where it used to
  carry whichever service configured logging last.
- **Behaviour change**: `/health`, `/ready` and `/live` answer HTTP 503 with
  `{"status": "error", "error": ...}` when evaluating them raises, where they answered 500. The
  documented unhealthy status is 503, and a prober that reads codes now sees "unavailable"
  instead of an undocumented 500. `/info` is not a probe, and a crash there is still a 500. The
  exception's own words are still withheld unless `expose_internal_errors` is set.
- **Behaviour change**: a `LoggingExtension` timing line now ends with the outcome, `ok` or `failed=<ErrorName>`, and names a timer by its method
  (`timer sweep 12.3ms ok`) where it used to read `timer None 12.3ms`; core puts the timer's method name on the dispatch
  context as `handler_name` so any hook can say which timer fired. A handler that raised no longer logs the same line as one
  that returned.
- **Behaviour change**: `setup_correlation_logging` writes its text and JSON files to the directory `LoggingConfig.configure`
  uses: its new `log_dir=` argument, else `CLIFFRACER_LOG_DIR`, else `./logs`, created when missing. It used to write to
  `logs/` under the current directory whatever the variable said. New: `enable_file=False` writes no files and creates no
  directory. All three of its sinks are now enqueued, as `configure`'s are, so a caller that reads the files straight after
  logging calls `logger.complete()` first.
- **Breaking**: a `StreamSpec` whose subject begins with a wildcard (`*.events.x.*`, `*.>`, `>`) is refused when it is built, with a message that names the subject and the server's `10052`. The server has refused these since 2.10.29, and the service used to fail at startup with text that mentioned neither wildcards nor a version. A lone `*` and a wildcard after the first token (`a.*.>`) are still accepted. A broker older than 2.10.29 accepted the refused shapes; a declaration that relied on that must now enumerate the namespaces.
- **Behaviour change**: a handler decorator (`@rpc`, `@async_rpc`, `@listener`,
  `@validated_listener`, `@broadcast`, `@timer`, or an extension entrypoint) on a method whose
  name starts with an underscore is now refused when the service's handlers are discovered,
  with a `ConfigurationError` that names the method and says to rename it or remove the
  decorator. Discovery has always skipped underscore names, so such a handler registered
  nothing, subscribed to nothing, and said nothing. Plain underscore helpers and `@dependency`
  probes on underscore names (`_check_db`) are unaffected.
- `LoggingExtension` reports `streaming` on `/health` only while its NATS sink is attached to loguru.
  It reported `true` for as long as it remembered the sink's id, so another service configuring
  logging in the same process, which detaches every handler, left `/health` saying logs were
  streaming when nothing was.
- **API Change**: `KvExtension(nc=...)` and `KvExtension(js=...)` take a connection
  or JetStream context where the extension is declared, and every service built
  from that declaration uses that same object. Binding used to copy the argument:
  a live connection failed service construction with `ExtensionIsolationError`
  unless wrapped in `SharedDependency`, and an unconnected client was copied
  silently, so connecting the original left the extension holding a client that
  was never connected. Wrapping in `SharedDependency` is still accepted.
- **Fix**: a revocation is dropped when its token expires, and an already-expired token is no longer stored,
  so the revoked set stays bounded by the live tokens. `revoke_token` returns `True` for an expired token (it
  cannot validate) and `False` for a token without a usable `exp`. The `_revoked_jtis` attribute is now a
  mapping of `jti` to the token's `exp`.
- **Behaviour change**: `SimpleAuthService.validate_token` returns `None` for every token that is not a
  valid one, instead of raising. A correctly signed token that lacks `exp`, `jti`, `user_id`, `username` or
  `email`, or carries a claim of the wrong type, used to raise `KeyError` or `TypeError` to callers that call
  it directly. It also returns `None` for a user the service holds as inactive, so deactivating an account
  ends the access of the tokens it already holds instead of leaving them valid until `exp`.
- **Breaking**: `serialize_value`, and so `put`, `create` and the object-store
  writes, raises `TypeError` for a value with no JSON form, where it stored the
  text of its `repr` and returned a revision as though the write had worked. A
  dataclass, datetime, date, UUID, decimal, enum, set or tuple, alone or inside
  a dict or list, is stored as JSON instead of as `DC(a=1)` or `{1, 2}`.
- **Fix**: when a startup failure's abortive cleanup had a step fail (a disconnect on a
  socket that was already gone), the `stop()` that followed ran the whole teardown again,
  stopping extensions and timers a second time and masking the startup failure with a
  secondary error. The lifecycle now records each teardown step that succeeded and runs
  only the steps that did not.
- **Fix**: assigning `container._run_worker`, the seam that intercepts a message at the extension pipeline, now takes effect on the event path as well as the RPC path. It was written to the RPC dispatcher only, so a test that patched it and then dispatched an event proved nothing about the event.
- **Breaking**: `BucketConfig` and `ObjectStoreConfig` check their options when
  they are built and raise `BucketConfigError` naming the bucket and the field.
  A `ttl` that is a bool or a string, negative, not finite or under 100 ms; a
  `history` outside 1 to 64; `replicas` under 1; and a `storage` other than
  `"file"`, `"memory"` or a `StorageType` used to reach the broker and fail
  there with an unrelated error, or create a different bucket than asked for
  (`history=0` kept every revision). A dictionary with an option the config does
  not have is refused, naming it, where it was silently dropped. A `ttl` of
  `None` in a dictionary now takes the `bucket_ttls` value, as it does in an
  instance. `normalize_ttl_seconds` no longer reads a numeric string as seconds.
- **Behaviour change**: declaring an extension under a name the `/health` or `/info` payload already uses (`status`,
  `service`, `name`, `features` and the rest) now raises `ConfigurationError` at construction. Its contribution is
  published under its own name, so it used to replace that key: an extension called `status` pinned `/health` at 503
  for a healthy service, and one called `name` replaced the service's name in `/info`. Rename the attribute.
- **Behaviour change**: `ConnectionError`, `HandlerError` and `TimerError` are removed from
  `cliffracer.core.exceptions` and from the package root, along with their entries in `__all__`.
  Nothing raised any of them. `ConnectionError` was also a builtin's name, so
  `from cliffracer import *` rebound it and an `except ConnectionError` in the importing module
  stopped catching socket errors. A client call that cannot reach the broker raises
  `RpcConnectionError`, which the circuit breaker already counts by default, so the breaker's default
  monitored list no longer names the removed class. `IdempotencyKeyError` is now a direct
  `ServiceError`; it was a `HandlerError`.
- **Breaking**: `PerformanceMetrics.active_connections` is how many connections
  are open now. It moves up on a `connection_opened` event and down on
  `connection_closed`, and `set_active_connections(n)` sets it outright. It used
  to be the number of times an `active_connections` event had been recorded, which
  only went up, so recording that event now raises `ValueError` pointing at the
  new ones, as does any other event name `record_connection_event` does not know,
  where it was dropped without a word.
- **Behaviour change**: `CircuitBreaker.record_failure()` and `record_success()` no longer let a
  result that arrives after the circuit has left CLOSED decide a later probe. A call that
  started before the trip and failed after the cooldown expired used to count as a failed
  probe, reopening the circuit and restarting the cooldown, and a late success closed a circuit
  whose probe had not run. Both are now read as calls admitted while CLOSED: they count while
  the circuit is CLOSED and otherwise only update `last_failure` and `success_count`. A probe
  is a call admitted through `async with breaker` or `breaker.call(...)`, as before.
- **Behaviour change**: reading `circuit_breaker` from a `ResilientRpcProxy` on the class, when
  no explicit `circuit_breaker=` was passed, now raises `AttributeError` pointing at the
  instance (`service.<attribute>.circuit_breaker`). It used to return a breaker that no call
  went through, which always read CLOSED with no failures while the breaker the calls tripped
  was OPEN. With an explicit breaker the class still returns that shared one, and the
  per-instance breaker an instance exposes is unchanged.
- **Breaking**: `PerformanceMetrics` reports `p95_ms` and `p99_ms` as
  nearest-rank percentiles. They read one sample too high: the p95 of 20 samples
  was the maximum, and the p99 of 100 was the maximum, so a single slow request
  set the figure. The p95 of 1 to 100 ms is now 95, not 96, and
  `check_performance_targets()`, which judges latency on p95, can now pass a
  window it failed.
- **Behaviour change**: `KvRateLimiter.reset()` with no key now clears every counter in its
  KV bucket (it cleared only the local fallback, so a limiter that was reset still refused),
  and a failed delete is reported as `RateLimiterUnavailableError` unless the limiter falls
  back, as `reset(key)` already did. New: `KvRateLimiter(bucket_ttl=...)` sets a time-to-live
  when the limiter creates its bucket, and `KvRateLimiter.prune_expired(window)` deletes the
  entries whose timestamps have all left the window; neither is on by default, so an
  existing bucket keeps growing one entry per partition key until one is used.

## 1.0.0
- `wrap_exception` copies the `details` it is given instead of writing the original
  exception into the caller's mapping, so two exceptions wrapped with one
  `details` each report their own cause, and an `ErrorHandler` used for more
  than one block no longer rewrites the earlier failure's record. A class that
  is not built from a message and details (`RpcRefusedError`,
  `ClientOutOfDateError` and `RpcValidationError`, which takes them in the other
  order) is refused with a `ConfigurationError` that names it, from
  `wrap_exception` and when an `ErrorHandler` is created. Before, the first two
  failed with a constructor `TypeError` while the real failure was being
  handled, and the third was built with its message and details swapped.
- **Behaviour change**: a `CircuitBreaker` with the default config opens only for the errors of a
  failing or unreachable dependency: `RpcTimeoutError`, `RpcNoRespondersError`, `RpcConnectionError`,
  `RpcServerError` and `ConnectionError`. `RpcValidationError`, `RpcUnknownMethodError`,
  `RpcRefusedError` and `ClientOutOfDateError` no longer count: each means the dependency answered and
  the caller was wrong, and a rate-limited dependency answers with a refusal, so its own throttling
  could open every caller's circuit. A bare `RpcError` or `RpcClientError` raised by application code no
  longer counts either; list it in `monitored_exceptions` to keep that.
- **Fix**: each listener in a service's description carries `effective_subject`, the subject the service subscribes to once its namespace and subject prefix are applied, and `*.<pattern>` for a `cross_namespace` listener. `pattern` still holds the declared pattern. The field is null when the description was built without a config, because the declared pattern is not where such a service listens. A description that lacks the field reads as null.
- `get_correlation_logger()` and the `ContextualLogger` that `get_service_logger()`
  returns attach the current correlation id to every record they write, as
  `extra["correlation_id"]`, so the id reaches every sink in the process,
  including ones installed after `setup_correlation_logging`. A
  `correlation_id` bound on the call wins, and a line written outside any
  request carries none. Before, only the sinks `setup_correlation_logging`
  created added it, and the id vanished when `LoggingConfig.configure` replaced
  them.
- A supervised background task that raises, which covers every `@listener` and
  `@async_rpc` handler, and a timer method that raises, are logged with their
  traceback. A braced exception message such as `{'a': 1}` is logged as written:
  before, it was read as a format placeholder, so the supervised task's failure
  was never logged and the timer's error handler raised out of the firing.
- **Fix**: `service.other.method.call_async(...)` is typed as returning a coroutine, so a call that is not awaited, which sends nothing, is reported by mypy as `unused-coroutine`. It was typed `Any`, so the dropped call type-checked.
- **Removed** the `cliffracer-faststream`, `cliffracer-backdoor` and `cliffracer-http`
  distributions, with the examples, documentation, decisions and benchmark metrics that
  described them. `FastStreamExtension`, `BackdoorExtension`, `HttpExtension`,
  `AutoGatewayExtension` and the route and websocket decorators no longer exist.
  `CliffracerService.broadcast_message` publishes the event and no longer calls
  `broadcast_to_websockets` on an extension that defines it.
- **Fix**: a function decorated with `@with_correlation_id` that is called with too many positional arguments raises the argument-count `TypeError` Python gives for the undecorated function. It raised `got multiple values for argument 'correlation_id'`.
- A distributed `@cron` firing whose handler raises leaves `status: "failed"` and
  an `error` of the form `Type: message` in its interval record. The record said
  `completed` for every firing, because the timer handles a handler's failure
  itself and the distributed wrapper never saw it. `Timer` gains `last_error`,
  the latest firing's failure in the same form, or `None` when it succeeded.
  A failed firing is still counted once in `error_count` and does not raise.
- `LoggingConfig.configure` creates a nested `log_dir` with its parents, and only
  when `enable_file` is true, so `enable_file=False` no longer creates a
  directory. The directory is created before any handler is removed, so a
  `log_dir` that cannot be created raises with the process's logging still in
  place instead of leaving it with no sink.
- `LoggingConfig.configure`, `LoggingConfig.add_nats_sink` and
  `setup_correlation_logging` take a service name containing `{` or `}`
  literally. A name such as `svc{0}` used to raise while a log file sink or the
  announcing log line was set up, so a service with that name failed to start
  when it declared `LoggingExtension`; the log files are now named after it and
  the human-readable format carries it unchanged.
- **Fix**: `service.other.method.call_async(...)` publishes to `{service}.async.{method}`, the subject `service.call_async` uses, so the callee runs it under `max_async_rpc_concurrency` instead of `max_rpc_concurrency`. It previously published to `{service}.rpc.{method}`, and a flood of proxy fire-and-forget calls competed with request/reply traffic for the same budget.
- **Fix**: a service builds its `{service}.describe` reply once and serves the same bytes to every later request, where it rebuilt the description for each one and held the event loop for the whole build (about 29 ms for a 30-method service). A config assigned to after the first request is described afresh, and every request still passes through the extensions' worker hooks, so a refusal applies to each.
- **Fix**: a function decorated with `@with_correlation_id` can be called with its `correlation_id` as a positional argument. It raised `TypeError: got multiple values for argument 'correlation_id'`; the passed id is now the one the function receives, and a positional `None` falls back to the request's header.
- **Fix**: `publish_event(..., idempotent=True)` gives the same `Nats-Msg-Id` as a service configured with `idempotent_publishing=True`, so the two deduplicate against each other. A key passed as `idempotency_key=` or set by an `@idempotent` handler is used as given when `idempotent=True` is also passed; the flag no longer hashes it away.
- `log_rpc_calls` and `log_event_handling` wrap a handler with `functools.wraps`,
  so a service that stacks them under `@rpc` or `@listener` starts: handler
  discovery reads the handler's own parameters instead of `*args, **kwargs` and
  no longer raises `UntypedHandler`. A synchronous function stays synchronous
  under either decorator instead of becoming a coroutine function, and the
  wrapped handler keeps its name and docstring.
- Stream deduplication matching now explicitly recognizes NATS's omitted, zero, and 120-second representations of its default window, including the 120-second value stored after a declaration requests zero.
- Half-open circuit-breaker probes now return their admission slot when cancelled or interrupted by an unmonitored application exception, and older in-flight requests cannot decide a later probe's outcome.
- Timezone-aware cron timers now measure waits across daylight-saving changes
  in elapsed time, preventing early, late, back-to-back duplicate, and
  tight-loop firings.
- **Fix** `AuthExtension` preserves the configured issuer by identity, so token
  revocations and user, role, or permission changes reach bound services.
- **Fix** Services refuse incompatible `@validated_listener` handler signatures
  during startup, before a delivery can fail after validation.
- Rate limits on namespaced, wildcard, validated and broadcast event handlers now use the declared extension limiter, and nested rate-limited operations retain their own budgets.
- KV and Object Store reads now raise when a cached handle's backing stream has disappeared instead of returning an absent key, empty bucket or missing object result.
- JetStream stream reconciliation overlays declared fields onto the live broker configuration, preserving operator-managed retention limits, discard policy, replicas, placement, description and metadata.
- NATS log streaming recursively redacts common credential fields before publication; automatic RPC and event logging records argument names and counts without recording handler values, and services can supply a domain-specific record redactor through `LoggingExtension`.
- Distributed rate limits now fail closed by default, report explicit local fallback as degraded health, require an authoritative source for string partition keys, and store or log only key fingerprints.
- Rate-limited durable events are NAKed with the limiter's availability delay instead of being acknowledged and discarded; RPC rate limits remain caller-visible refusals.
- **API Change**: `cliffracer.testing.wait_until` now provides bounded condition
  waits whose failures name the expected observation and elapsed budget.
- **API Change** Add a JetStream bind mode that validates and uses operator-provisioned
  streams and durable consumers without account-wide metadata or resource-creation grants.
- Typed outputs reject payloads whose validation changes JSON value types, including numeric values that compare equal to booleans, before submitting a publication.
- Template retries preserve strict Python types in accepted defaults and honor configured input aliases. Settings with excluded fields or aliases that cannot read their serialized keys are rejected before acceptance, including nested models.
- Preserve accepted optional settings on matching activation retries and refuse revalidation that changes retained values.
- Add typed template outputs with retained activation routes, per-publication tokens, separate event contract verification, inspectable permission families and producer generation metadata.
- Derive scoped NATS service and client permissions, configure dedicated connection inbox prefixes, and check named RPC coverage with broker-enforced order and shipment contracts.
- Preserve cross-namespace listener routing through introspection and omit private event handlers consistently with runtime discovery.
- **Feature**: `ServiceOwner` binds a separate supervisor owner to each parent service, closes its children during normal or interrupted teardown, and retains structured cleanup results. A runnable order/shipment example uses pregenerated clients, runtime progress subjects and independent parent lifetimes.
- **Feature**: `LocalSupervisor` owns bounded local service activations with typed settings, shared startup, generation-specific routing, owner teardown, contract probes, retained outcomes and explicit reactivation. Unfinished cleanup remains visible and prevents replacement; ordinary business RPCs are never replayed automatically.
- **Bug fix**: Template runtime configuration preserves explicitly supplied host callbacks, including bound methods, while isolating mutable configuration data.
- **Bug fix**: Service shutdown unsubscribes intake before draining active handlers even when stopped immediately after startup. Subscriptions created during interrupted consumer inspection are also cleaned up without depending on listener tasks having run.
- **API Change**: Service templates register fixed RPC contracts, isolated typed settings and validated factories through `cliffracer.runners`. Immutable activation references bind generated clients to explicit service, namespace and subject-prefix addresses. `ServiceClient(subject_prefix=...)` supports explicit prefixes while its default continues to read the environment. Supervised activation and owner teardown remain separate lifecycle work.
- The client generator can check a generated file without rewriting it and
  report missing, extra or changed RPCs. Class-based generation and checking can
  require the service definition to resolve under a specified source directory,
  rejecting another checkout or an unverifiable source before emitting output.
- Shutdown bounds task cancellation with an additional drain-sized grace and
  reports tasks that refuse to stop by name. Unfinished tasks remain visible and
  prevent the same service from restarting. Synchronous service entry points
  close their event loop without joining these tasks indefinitely; callers that
  own an event loop remain responsible for its teardown.
- **New API** `publish_event_nowait(subject, *, timeout=2.0, **data)` publishes an
  event without holding its caller. The send runs as a supervised task, so a
  stopping service drains it before closing the connection, and the wait is
  given up after `timeout` rather than after nats-py's five-second JetStream
  acknowledgement wait. A publish whose answer never arrived is counted in
  `unconfirmed_events` by subject and reason and logged; that tally names at
  most 32 subjects and counts the rest under `other`, so a service publishing
  per entity cannot grow it without bound. It is best effort: nothing is
  retried and nothing is queued, so a consumer that must not miss a message
  still needs `publish_event`. A `timeout` there says the wait was given up,
  not that the broker refused the message.
- Each subject a `@validated_listener` declares is validated against the schema
  declared for that subject. A method carrying two `@validated_listener`
  decorators used to validate both subjects against the last schema applied,
  dead-lettering valid messages on the other. A method carrying both a
  `@listener` and a `@validated_listener` used to skip the schema on the
  validated subject and call the handler with whatever the payload held.
- **Breaking**: a `@broadcast` handler, and one added with
  `register_broadcast_handler`, subscribes under the service's `namespace` and
  `subject_prefix`, where `broadcast_message` publishes. On a service with
  either set, the handler used to subscribe to the bare pattern and so never
  received its own service's broadcasts. A publisher outside the namespace that
  sends the bare subject no longer reaches it: publish under the namespace, or
  receive every namespace with `@listener(pattern, fanout=True,
  cross_namespace=True)`. A `@listener` and a `@broadcast` on the same subject
  are now refused under a namespace too, as they already were without one.
- A client from `cliffracer-generate-client` is written the way ruff formats
  and sorts it when a method and parameter name make the call that encodes an
  argument too long for its line, when enough models come from one module that
  their import is too long, and when module or model names need ruff's import
  order. Arguments go one per line, a long import is parenthesised with one name
  per line, and imports sort naturally (`app.v2` before `app.v10`) with
  constants before classes before other names. Each used to need reformatting
  or re-sorting. A client whose lines fit and whose imports already sorted is
  unchanged.
- A client from `cliffracer-generate-client` whose annotations are too long for
  their line is written the way `ruff format` writes it: split at the brackets,
  one element per line with a trailing comma, and before `| None`. It used to be
  written on one line and needed reformatting. A client whose annotations fit is
  unchanged.
- `SimpleAuthService.refresh_token` refuses a token that has no `oiat` claim.
  Every token the service mints carries one. A token signed elsewhere with the
  shared secret and lacking it used to be refreshed with its `iat` standing in
  for the original issue time; it now returns `None` and logs a warning.
- **API Change**: a client generated with `--namespace` records it as
  `NAMESPACE`, and `ServiceClient` constructed without `namespace=` calls in
  that namespace; it used to call the un-namespaced subjects unless every
  caller repeated the namespace. `namespace=""` calls outside it. `--class`
  records `--namespace` too, so both forms still produce the same bytes, and a
  namespace that is not a single subject token exits 7.
- An eager timer whose first firing raises keeps running. For `@timer` and
  `@cron`, an exception escaping the eager firing, such as one reading the
  timer's method, used to end the timer's task while it still reported itself
  running, and nothing was logged or counted. Every timer, including
  `@cron(distributed=True)`, now handles an eager firing's error as it handles
  a scheduled one's: logged, added to `error_count`, followed by `error_backoff`,
  and then the schedule. A distributed timer's eager error used to be logged
  only.
- **API Change**: `cliffracer-generate-client` exits 7 for a mistake in its
  own command line: a missing, unknown or malformed flag, a `--header` that is
  not `NAME=VALUE` (which exited 5, the code for an unimportable `--class`),
  `--version` without `--class`, and `--header`, `--nats-url` or `--timeout`
  with `--class`, which were silently ignored. argparse's own usage errors
  exited 2, the code this command gives a broker that answered without the
  service.
- `cliffracer-generate-client` binds each method's return alias with a `type`
  statement (`type _Return_<method> = ...`) instead of a `typing.TypeAlias`
  annotation, and the generated file no longer opens with a
  `# ruff: noqa: UP040` comment. Regenerating a client changes those lines.
- A `@validated_listener` whose schema declares a `correlation_id` field
  receives the value the event carries, flat or in an envelope's `data`, as a
  typed `@listener` taking the same model does. It used to receive the field's
  default, because the id was removed from the payload whether or not the
  schema declared it. A schema without the field still has the id removed, so
  one that forbids extra fields still validates.
- Starting a `DistributedCronTimer` that is already running warns and does
  nothing, as every other timer does. It used to look up the service's
  `KvExtension` first, so a running timer whose `KvExtension` had gone raised
  `ConfigurationError` instead. Starting a timer that is not running still
  refuses a service without a `KvExtension`.
- **Behaviour change**: every send path serialises its payload before
  `before_call` runs. A hook that sets an attribute on a custom object in the
  payload no longer changes what `publish_event` or `broadcast_message` sends,
  as it already did not for the RPC paths; the caller's object is still
  changed. A payload the serialiser refuses raises before any hook runs, so
  `before_call` and `after_call` no longer fire for it on those two paths. An
  idempotent `publish_event` sends the bytes its payload hash was computed
  from.
  - A one-shot iterable in the payload, such as a generator, is consumed by
    serialisation before `before_call`, so a hook sees it already exhausted on
    `publish_event` and `broadcast_message`, as it already did on the RPC paths.
- `cliffracer-generate-client` reports a `describe` reply that is not JSON as
  exit 4 and shows the start of it, where it used to exit 3 with "no broker
  reachable" after the broker had answered. Exit 3 is now given only for an
  error from the NATS client; any other exception propagates as a traceback
  instead of being reported as an unreachable broker.
- **API Change**: `KvExtension.history()` and `keys()` can raise where they
  returned `[]`. Under load nats-py can queue a watch's end-of-snapshot marker
  before the entries, and both read a key or bucket with entries as empty.
  Both now wait for the entries while the stream holds any, and raise
  `nats.errors.TimeoutError` if none arrive within the JetStream context's
  timeout. A server error met while reading, or while checking the stream, is
  raised too. `[]` still means the bucket held no message for the key, or no
  active key, when read. The `watch()` docstring notes that its `None` can
  precede the snapshot.
- `cliffracer run` exits 2 for a service whose constructor raises, as it does
  for a module that cannot import. The exception escaped as an unhandled
  traceback and exit 1. The message names the service class and the exception
  type, and the log carries the traceback.
- **API Change**: `cliffracer-generate-client --class` records the version it
  is given, or `ServiceConfig`'s default version without `--version`, where it
  used to record `0`. A class cannot see the config it is started with, so pass
  the version that config declares. `--version` without `--class` is refused,
  since a running service reports its own. `describe()` with no version from any
  source falls back to the same `ServiceConfig` default rather than `1.0.0`, so
  a class and the unconfigured service it describes agree.
- **API Change**: `SimpleAuthService.validate_token` refuses a token that has no
  `jti` claim, and so `refresh_token` and `AuthExtension` refuse it too.
  Revocation is keyed on the `jti`, so such a token could never be revoked and
  was accepted until it expired. Every token `SimpleAuthService` mints carries a
  `jti`.
- `SimpleAuthService.revoke_token` returns `True` when the token's `jti` is in
  the revoked set afterwards and `False` when nothing was revoked, because the
  token does not decode or carries no `jti`. It returned `None` in every case.
- `cliffracer-generate-client` checks a `describe` reply before reading it. A
  reply that is not a description -- not an object, a missing `service` or
  `version`, a method without `returns`, a type reference without `kind` --
  exits 4 with a message naming where it went wrong, where it used to escape as
  a traceback and exit 1. An unknown scalar type is refused by `emit` as
  `CannotEmit`, like an unknown kind.
- An `@rpc` handler cannot be named after any public name a `ServiceClient`
  has, including `connect_timeout`, which a generated client's constructor set
  over the method so that every call raised `TypeError`. The names are read
  from `ServiceClient` itself. The generator also refuses a method name that
  starts with an underscore.
- `cliffracer run` exits 2 for a target module that raises anything at import,
  not only `ImportError`. A module-level `ValueError`, `KeyError` or
  `SyntaxError` -- a missing environment variable, a config parsed at import --
  used to escape as an unhandled traceback and exit 1. The message names the
  exception type, and the log carries the cause's traceback, which a one-line
  message would hide.
- **API Change**: an event from `publish_event` carries its payload only under
  `data`. The envelope's top level used to repeat every payload key beside
  `source_service`, `timestamp` and `correlation_id`, as `broadcast_message`
  never did. A reader of the raw message moves from `payload["order_id"]` to
  `payload["data"]["order_id"]`; listeners already read `data` and are
  unaffected. A send-side hook sees the same shape in `ctx.payload`. A
  one-shot iterable in the payload, such as a generator, now reaches listeners
  with its values: the top-level copy used to consume it first, leaving `data`
  with an empty list.
  - A `@rate_limit` key on an event listener resolves against the event's
    payload rather than the envelope, for string and callable keys alike. A
    string key used to find nothing in a `broadcast_message` event, so every
    caller shared one bucket named after the key. A callable key is given a
    context whose `payload` is the event's payload.
  - `cliffracer-cyanide`'s random mode hashes the payload to choose a fault, so
    a seed selects different messages from `publish_event` than it did before.
- **API Change**: MessagePack payloads carry the same values as JSON. Integer
  map keys become strings and `bytes` become `str` under `msgpack` too; a
  receiver used to refuse integer keys after the message was sent. Bytes that
  are not valid UTF-8 are refused when the message is sent, under either format;
  `msgpack` used to carry them.
- **API Change**: `@validated_listener(on_invalid=...)` accepts only
  `"deadletter"`, `"drop"` or `None`, and raises `ConfigurationError` for
  anything else when the decorator runs. Dispatch dead-letters only on exactly
  `"deadletter"`, so a value such as `"dead-letter"`, `"dlq"` or one with a
  trailing space used to start cleanly and drop every invalid message. An empty
  string, which used to mean the service's `default_on_invalid`, is refused too;
  leave `on_invalid` unset for the default.
- `SimpleAuthService` looks a user up by the lowercased name in every method.
  `create_user` already stored it that way, so a user created as "Alice" could
  not authenticate as "Alice", and `add_role` and `add_permission` given that
  name did nothing.
- **API Change**: `SimpleAuthService.add_role` and `add_permission` raise
  `ValueError` for a user that does not exist, instead of doing nothing.
- A `Message` the framework builds for a handler or returns as an RPC result
  carries the request's correlation ID in `correlation_id` when the field was
  left `None`: RPC and async RPC arguments, RPC results, the async RPC result
  `worker_result` hooks receive, and `@listener`/`@validated_listener`
  messages. An RPC reply's `result` used to carry `"correlation_id": null`
  beside the envelope's real ID. Explicit values are kept.
- **API Change**: `NumericBounds.MIN_PORT` and `NumericBounds.MAX_PORT` are
  removed. Nothing validated a port against them.
- `validate_batch_size` refuses `True` and `False`. A bool is an `int` to
  `isinstance`, so `True` was accepted as a batch of one.
- **API Change**: the extension contract is importable from `cliffracer`:
  `Extension`, `ExtensionIsolationError`, `ExtensionSetupContext`,
  `RejectMessage`, `SharedDependency`, `WorkerContext` and `entrypoint` are the
  same objects as in `cliffracer.core.extension`, which keeps working. The guide
  and READMEs import them from `cliffracer`. `SharedDependency.obj` is removed;
  read `.value` or call `unwrap()`. `_safe_clone_arg` is no longer listed in
  `cliffracer.core.extension.__all__`.
