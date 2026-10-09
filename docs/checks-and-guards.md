# Checks and guards

`tests/repo/` holds tests whose subject is the repository: its documentation,
its packaging, its CI workflows, and the test suite itself. They run with every
other tier under `uv run pytest tests/repo/`.

Each entry below says what the guard reads and what makes it fail, so a failure
message points at a rule rather than a mystery. The rule each one enforces is in
the module docstring; this is the index, and it covers every guard in that
directory (every `test_*.py`) -- one of them by description rather than by
filename, for the reason recorded at the end. `test_the_guards_index_names_every_guard.py`
holds the index to that. `tests/repo/test_the_suite_follows_its_conventions.py` holds the
directory to its naming and tier conventions, so a new guard arrives here as a
line rather than as a surprise.

## The suite

| Guard | Reads | Fails when |
|---|---|---|
| `test_every_broker_test_carries_the_marker.py` | What pytest collects for `integration and not nats_required`, and every test module's calls that dial a broker, by syntax tree | A test that dials a broker lacks the marker, so the skip switch and the run summary both miss it |
| `test_nothing_dials_a_broker_with_a_bare_nats_connect.py` | The core's and every package's source, for calls to `nats.connect` | Code dials with `nats.connect` instead of `cliffracer.core.dial.connect` |
| `test_the_suite_has_one_broker_url.py` | Every test and fixture, for a literal broker address | A file pins an address instead of taking `$CLIFFRACER_TEST_NATS_URL`, or an allowlisted file's count drifts from what it pins |
| `test_a_plain_pytest_does_not_dial_a_broker_nobody_named.py` | A child pytest run, with a TCP listener the test owns standing at the default broker address | A run that names no broker dials one anyway, to probe it, to run a `nats_required` test or to clean up at the end, or the probe line does not say no broker was named |
| `test_the_summary_names_marked_tests_that_ran_without_a_broker.py` | The terminal summary of child pytest runs | A `nats_required` test ran with no broker reachable and the summary does not name it, or a healthy run that holds them back warns |
| `test_the_broker_tests_run_together_and_apart.py` | The broker tests in one session with the root suite, and each package's tests on their own | A broker test leaves `CLIFFRACER_SUBJECT_PREFIX` set behind it, a package test imports the root `tests` package, or a package cannot collect its tests alone |
| `test_the_transport_contract_runs_in_both_tiers.py` | What pytest collects for each backend | A contract case is collected against one backend and not the other, or the shared list differs from its recorded size |
| `test_transport_tests_take_the_transport_fixture.py` | Every module in `tests/transport/`, by syntax tree | A test constructs its own transport, or a module that needs one does not take the fixture |
| `test_the_property_checks_run_within_their_budget.py` | A child pytest of every unit-test module that imports the property harness, on its fixed seeds, measured in CPU seconds (`getrusage` of the child) | The property checks together spend more CPU than their budget, fail, pass nothing, or are not found |
| `test_one_mock_message_envelope.py` | Class definitions across the tracked tree | A second class is named `MockMessage` |
| `test_every_message_double_refuses_a_reply_it_cannot_send.py` | Every hand-written message double | A double records a reply where `nats.aio.msg.Msg` refuses one with no reply subject, or an exemption names a double that is gone or gives no reason |
| `test_no_mocked_test_claims_a_delivery_count.py` | Every test module and conftest under `tests/` and `packages/*/tests/`, for a delivery-count attribute read inside an `assert` | A module standing in for the transport asserts how many times a message was delivered -- a value only a broker decides, and one the test itself assigned. Assigning it is an input and is not reported; the guard also fails if its own sweep stops finding modules or mocks |
| `test_no_test_reloads_a_live_module.py` | Every tracked test module and conftest, by syntax tree, for an `importlib.reload` call | A test reloads a module, which rebinds its module-level objects for whatever runs next |
| `test_wire_names_bound_to_a_name_are_still_read.py` | Raw broker calls in the integration tier, resolving a name to the literal it was bound to in the same function | A raw call addresses a stream, bucket or subject by a literal, or a name bound to one, that carries no prefix. An inline f-string and a name returned by another call are not read |
| `test_the_suite_follows_its_conventions.py` | The tier marker and filename of every test module under `tests/` and `packages/`, and every test module's use of `.git` | A module declares no tier, two, or one that does not match its directory; a filename repeats its directory's tag; a qualifier (`adversarial`, `stress`) sits anywhere but the end of a name; a test module checks `.git` with `.is_dir()`, or a repo guard skips for want of git in a worktree |
| `test_a_hang_still_reports.py` | The pytest timeout settings | A run can hang without a deadline, so a stuck test reports nothing |
| `test_the_testing_helpers_import_without_pytest.py` | `cliffracer.testing`, imported with pytest unavailable | A module under that namespace imports pytest at module scope |
| `test_the_tests_a_branch_removes_are_reported.py` | `scripts/report_removed_tests.py`, run over real repositories | A removed test goes unnamed, an added one is named, a rename reads as a removal, or the script fails the job. The report is printed and never gates |
| `test_console_script_resolves_from_the_interpreter.py` | Console-script lookup with nothing on `PATH` | The script beside `sys.executable` is not found, a copy on `PATH` wins over it, or the failure does not say where it looked |
| `test_examples_do_not_pin_a_port.py` | The runnable examples, by syntax tree | An example binds a fixed port, so a second copy cannot start |
| `test_workspace_packages_are_installed.py` | The installed environment, the documented install commands and the workflows' install steps | An extension under `packages/` is absent or not importable, or a documented or workflow install command syncs core alone |

Alongside these, `tests/unit/test_one_correlation_id_var.py` and
`packages/cliffracer-auth/tests/test_one_auth_context_var.py` walk the loaded
modules of a package and assert every `ContextVar` found under one name is the
same object. Two objects under one name means a value set through one is
invisible through the other.

`tests/integration/test_examples_run.py::test_the_example_starts` passes a demo
that exits 0, and a long-running example that prints `EXAMPLE READY:` within 30 s
and then stops cleanly at SIGINT. It fails, naming the example, on a crash line,
on a non-zero exit, and on a long-running example whose marker never comes: the
marker is printed after the example's step has happened, not when it starts.

`tests/integration/test_examples_run.py` derives which examples set their own
`health_port` by reading their syntax tree. An example that takes the default
is in the pool its two-copies test draws from; one that names its own is left
out of that pool and run by `test_two_copies_of_a_port_naming_example_do_not_contend`.

## Composition and structure

| Guard | Reads | Fails when |
|---|---|---|
| `test_extensions_do_not_join_the_service_mro.py` | The MRO of every shipped service and Extension, and every class name ending in `Mixin` | An extension is inherited rather than composed, or a package ships a mixin |
| `test_class_complexity_invariants.py` | Statement counts per class | A class exceeds the ceiling, or the ceiling drifts far above the largest class |
| `test_no_orphan_defs.py` | Module-level definitions and every reference to them | A definition is unreferenced, counting imports, `__all__` entries and pytest's own fixture and class conventions |
| `test_every_exception_class_is_raised_somewhere.py` | Every exception class under `src/` and `packages/*/src`, and every `raise` there, by syntax tree, resolving each raised name through the module's definitions and imports | A class nothing raises, directly or through a subclass, that is not listed in `NOT_RAISED_BY_THE_LIBRARY` with a reason. The list is empty. A same-named builtin or third-party class does not count for the library's |
| `test_core_import_closure_is_within_declared_deps.py` | What importing every `cliffracer.*` module reaches, under an import hook that refuses every installed third-party distribution outside the closure of core's declared dependencies | A core module imports an undeclared distribution unconditionally, which the development environment hides because it installs every extra. A guarded optional import is untouched |
| `test_error_text_is_read_only_for_a_reply_with_no_code.py` | Every dict literal in `src/` that is an RPC error reply, and every decision (prefix, substring, comparison, search) made on a reply's `error` text, by syntax tree | An error reply has no `code`, or a decision is made on its text outside `raise_for_error_envelope`, the one named function that reads it for an old service's reply with no code. ADR-0011 lists the structured signals |
| `test_core_imports_no_web_stack.py` | Core's imports, both as text and under a fresh interpreter | Core reaches for a web framework, including from inside a function where a runtime probe cannot see it |
| `test_persistence_boundary.py` | Core, for persistence reaching past its boundary | Core takes on storage concerns that ADR-0001 places outside it |
| `test_instrumentation_coverage.py` | The dispatcher's call graph | A callback handed to the broker client reaches a path no instrumentation covers |
| `test_logging_policy.py` | Library code, by syntax tree | Library code logs through anything but loguru, with docstrings and comments exempt because the sweep reads code |
| `test_the_timers_read_time_only_through_their_clock.py` | `core/timer.py` and the cron package's `cron.py` and `distributed.py`, by syntax tree | A timer reads the time or waits other than through its `clock`: a `time` call, `datetime.now` or `date.today`, `asyncio.sleep`, `wait_for`, `timeout` or `timeout_at`, an `asyncio.wait` with a bound, the event loop's `.time()`, `.call_later()` or `.call_at()` (on `get_running_loop()` or what holds it, a name bound in the function or passed to it, or an attribute), a clock function taken without being called, any of these under an aliased import, or an import that spells one without its module. The reads that stay on real time, the lease record's stamps and the firing's duration, the build-time check of a cron expression, a stop's grace, a firing's deadline and the bound on a finishing lease write, are listed by file, function and read, with a count and a reason; a count that differs from what is found fails |
| `test_cross_package_reexports.py` | Every `from cliffracer…` and `from cliffracer_x…` import in `packages/*/src` and `packages/*/tests` | A distribution imports a name from core or from another distribution that is not there |
| `test_every_validation_bound_is_read.py` | The constants of `NumericBounds` and `StringLimits`, against `src/` and each package's `src/` | A declared bound is read nowhere, or the sweep stops finding the classes or the sources |
| `test_service_config_call_sites_use_real_fields.py` | Every `ServiceConfig(...)` call | A call passes a field the model does not define |
| `test_pydantic_v2_compliance.py` | Library source as text, and each package imported in a fresh interpreter | A Pydantic v1-era construct survives, in the text or as a deprecation warning at import |
| `test_runtime_classifies_errors_by_type.py` | Every `except` body under `src/` and `packages/*/src`, by syntax tree | An error is classified from its message text with no typed check of the same decision, counting an `isinstance` in the handler and a sibling `except` clause on the same `try` |
| `test_every_wire_subject_is_built_by_a_helper.py` | Every f-string, and every `.format()` over a subject-template field, handed to a transport call under `src/` and `packages/*/src` as its subject (first or `subject=`) or, for a publish, its reply subject (third or `reply=`), by syntax tree | A subject is assembled at the call site, which skips the environment prefix and the namespace. `%` interpolation, `"".join(...)` and `.format()` on a literal are not read |
| `test_only_the_jetstream_dispatcher_acknowledges_a_message.py` | Every module's calls to `ack`, `nak`, `term` and `in_progress` | A module other than `cliffracer.core.dispatch.jetstream` acknowledges a message |
| `test_every_error_envelope_is_labelled.py` | Every error envelope the RPC dispatcher builds (`dispatch/rpc.py`, and `dispatch/rpc_limits.py` for the replies of a request its limits stop), by syntax tree, and the codes the client in `core/exceptions.py` branches on | An envelope carries no `code`, the number of envelopes differs from the reviewed count, or an emitted code is one the client does not understand |
| `test_every_exported_rpc_error_is_raised_somewhere.py` | Every exported name that resolves to an `RpcError` subclass, against the `raise` sites under `src/` | An exported error class has no `raise` site |
| `test_no_package_exports_a_name_that_shadows_a_builtin.py` | Each distribution's `__init__.py`, reading `__all__` without importing the package | An exported name is also a builtin's, such as `ConnectionError`. A spelling of `__all__` the scanner cannot read is refused by file and line |
| `test_decorator_docstring_examples_start.py` | Every `Example:` block in a decorator's docstring, declared on a service class and put through handler discovery | An example declares a handler a service refuses |
| `test_the_service_docstring_says_who_makes_the_outbound_sends.py` | `CliffracerService`'s docstring against its module | The docstring claims the module holds no transport logic while the module makes the sends, or does not name them |
| `test_every_package_declares_its_lifecycle_tier.py` | `[tool.cliffracer] lifecycle` in each `packages/*/pyproject.toml` | A package declares none, declares a value that is not one of the three tiers, or has no `pyproject.toml` |
| `test_the_adr_counts_the_packages_that_read_the_environment.py` | The ADR's `Environment variables.` paragraph, and `os.environ`, `getenv` and `env_prefix` in each `packages/*/src` | The paragraph does not name a package that reads the environment, or its count of the packages that read none is not the number the tree has |
| `test_the_probe_routes_core_serves_are_the_ones_the_docs_name.py` | The routes `HealthListener` dispatches, ADR-0002 and the two documents a reader meets first | The served routes, the ADR's list and the documents' lists differ. A route matched by a regular expression, a table or a variable is not read |

The classification guard's sweep asserts it read more than fifty source files,
including core and a package, so a tree it never opened cannot read as a tree
with no violations. Its controls plant a text test beside a typed check in a
sample, once as an `isinstance` in the same handler and once as a sibling
`except` clause on the same `try`, which is what proves it climbs from the
handler to the enclosing `try`.

## CI workflows

| Guard | Reads | Fails when |
|---|---|---|
| `test_a_killed_pytest_says_it_was_killed.py` | Each "Run tests" step of the Gitea pipeline, run under `bash -e` with a stand-in `uv` that is killed, fails or passes | A step does not say on exit 137 that pytest was SIGKILLed, most likely by the job container's memory cap, says it on another status, or ends with a status other than pytest's |
| `test_ci_workflows_run_the_same_gates.py` | The one CI pipeline each platform holds under `.gitea/workflows/` and `.github/workflows/`, a pipeline being a workflow triggered by `push` or `pull_request` | A platform holds no pipeline or more than one, skips a gate, runs one conditionally, or drifts from the other platform outside the differences recorded with their reasons |
| `test_ci_workflow_rollback_adversarial.py` | The release job's step order and its rollback condition, per platform | The rollback fires when it should not, or a step reference names an id no step declares |
| `test_benchmark_job_runs_alone.py` | The benchmark job's triggers and ordering | The benchmark can run beside the test job, where contention moves a metric further than the threshold that scores it |
| `test_workflows_carry_no_history.py` | Workflow comments and `name:` strings, on both platforms | Workflow prose carries an issue number, a commit hash, a run number, a processor model, a core count or a memory size, or names a term supplied through `CLIFFRACER_PRIVATE_TERMS` |
| `test_a_scheduled_workflow_is_not_a_pipeline.py` | The triggers of every workflow that is not a CI pipeline, and the helpers that tell the two apart | A schedule-only or dispatch-only workflow has a CI trigger, declares no concurrency group or starts a container without a fixed image, or the history and private-term scans stop reading it. A workflow given a `push` trigger becomes a pipeline and every pipeline rule applies to it |
| `test_the_release_step_cannot_hide_a_broken_tool.py` | The release steps of both pipelines | A step swallows the release tool's failure with `2>/dev/null` or `\|\| true`, a dispatch input offers `major`, no pipeline runs the release tool, or something is published without the decision saying so |
| `test_a_release_dispatch_cuts_the_prereleases_own_version.py` | The Gitea decision step run for `level: release` in a temporary repository whose history holds conventional breaking commits, and `scripts/release_target.py` over prerelease and final tags | The step tags what semantic-release computes (a major from a `1.1.0` prerelease) instead of the prerelease's base version, picks a prerelease other than the newest by version, or promotes with no prerelease, with the final tag already present, with a higher final release tagged or from a commit the prerelease is not an ancestor of |
| `test_release_decision_uses_local_history.py` | The release job's `decide` step, run in a throwaway repository | The step contacts the forge, fails on the runner's forge URLs, or decides on a broken release configuration instead of refusing |
| `test_release_tag_can_be_bumped.py` | The prerelease tag the release job cuts, built into a version | The token normalises to a `.devN` version, which the version backend cannot bump past |
| `test_agents_md_names_the_gates.py` | `AGENTS.md`, against the gate commands in `.gitea/workflows/ci.yml` | A gate the pipeline runs is missing from the file, or the list is satisfied trivially |
| `test_compose_files_run_what_the_tree_holds.py` | Every compose file in the tree | A file builds an image, or mounts a relative host path the repository does not hold |
| `test_kv_compatibility_is_a_gate.py` | The KV compatibility script and both pipelines' calls to it | A contract that skipped or did not execute reports green, a stalled probe is not closed, a failed run removes a broker it did not start, or a pipeline does not run the contract after the full suite and clean up |
| `test_message_schedules_is_a_gate.py` | The message-schedule script, both pipelines' calls to it, the collection of its live rows, and the docker calls of the rows that restart the broker | A contract that skipped or did not execute reports green; the broker is not the pinned nats-server 2.12.0 on a loopback port the script found free, mapped fixed so a restart keeps it; a port taken before the broker binds it is not retried, or is retried more than three times or with a fallback; a failed run removes a container it did not create; the contract is not told its container's name, or is given the restart rows with `--broker-url`; a restart row's docker call names any container but the gate's; a pipeline does not run it after the full suite; or the live rows are collected without the gate's flag |
| `test_a_behaviour_change_carries_a_changelog_fragment.py` | `scripts/check_changelog_fragment.py`, over a throwaway remote and a branch cut from it, with the pull-request environment | A change under `src/` or `packages/*/src/` adds no fragment and carries no opt-out trailer, or the check acts outside a pull request |
| `test_commit_check_fetches_exclude_tags.py` | The arguments the commit-message check hands git | A fetch can follow a tag, which changes the version the build derives |
| `test_the_commit_check_says_which_base_it_measured.py` | The commit-message check's success and failure lines | The line omits the range, the remote or whether the fetch worked, or prints a credential from the remote URL |
| `test_the_commit_check_says_why_it_cannot_answer.py` | The commit-message check, over histories built to fail each way | A refusal names one cause for several: a failed deepen, an unfetchable base, unrelated histories, exhausted history, or a missing pattern set |

Both platform directories are swept, and a test asserts each one yielded a
file, so deleting either is a failure rather than a smaller sweep.
`tests/repo/ci_workflows.py` is the discovery every workflow guard shares, so
they cannot disagree about which files are pipelines.

The workflow prose sweep reads comments through the YAML scanner, so a `#`
inside a quoted scalar is a value rather than a note. It shares the pattern set
the documentation, source and commit-message checks use, and adds the shapes
that set has no reason to carry: a run number, a processor model, a core or
thread count, a memory size. Terms this repository does not publish are not
written down here; the runner supplies them in `CLIFFRACER_PRIVATE_TERMS`, a
failure names the file and line without repeating the term, and a run without
the variable skips with a reason rather than passing quietly.

## Packaging and release

| Guard | Reads | Fails when |
|---|---|---|
| `test_workspace_versions_are_lockstep.py` | Built artifacts | A member derives its version from anything but the VCS tag |
| `test_member_wheels_carry_metadata.py` | Built wheels | A wheel ships without a description or a license |
| `test_the_shared_build_is_redone_for_a_changed_tree.py` | `tree_key` over a planted git tree | An edit of equal size and timestamp, an added file or a new commit leaves the key of the build the packaging guards share unchanged |
| `test_core_sdist_is_core_only.py` | The core source distribution | Core's sdist carries a workspace member package |
| `test_dependency_lists_agree.py` | The two dev dependency lists in `pyproject.toml` | The lists disagree, or either has no `pytest`, `ruff` or `mypy` |
| `test_release_note.py` | `scripts/release_note.py` and the workflow that calls it | The note generator or its call shape breaks, tested before the one run that cannot be undone |
| `test_a_release_note_over_128_kib_is_posted_whole.py` | The release-note steps of both workflows, run with a stand-in renderer, `curl` and `gh` | A note larger than one argument or environment string may be (128 KiB) reaches the step's `exec` through the environment or an argument, or arrives at the forge other than as rendered |
| `test_a_release_note_carries_the_changelog.py` | The release note renderer, over real repositories, reading each tag's own tree | A release candidate's note omits a pending fragment or orders them differently from assembly, a final release's note omits `CHANGELOG.md`'s section for its version, a prerelease tagged after assembly omits the section of the version it leads to or carries another version's, a fragment is read from the working tree instead of the tag, or a release job does not pass the tag to the renderer |
| `test_changelog_fragments_assemble_into_a_release.py` | `scripts/assemble_changelog.py`, over repositories the test builds | Two branches adding fragments conflict, assembly orders or folds the entries wrongly, a malformed fragment does not stop it by name, a version that has a section is accepted, or an empty assembly is accepted |
| `test_the_python_floor_is_the_one_the_code_needs.py` | Every `requires-python`, the lowest classifier, the README badge, every Python base image, and the source parsed at the declared floor | Two places state different floors, or the source uses syntax newer than the floor |
| `test_the_pydantic_floor_is_a_version_the_suite_passed_on.py` | Every pydantic requirement in the workspace's `pyproject.toml` files, the locked pydantic in `uv.lock`, and the floor and verified versions written in the guard | Two declarations name different floors, a declaration's floor is not the guard's, the guard's floor is not a version recorded as verified, or the locked version is below the floor |

## Benchmarks

| Guard | Reads | Fails when |
|---|---|---|
| `test_benchmark_metrics_declare_direction.py` | Every metric in the baseline | A metric says nothing about which way it is supposed to move, leaving the gate to guess |
| `test_a_different_machine_is_not_scored.py` | The regression gate, run on a baseline and a run whose machine fields differ | The gate scores the run, or passes a regression on the other machine, instead of refusing with exit 2 and naming each differing field |
| `test_benchmark_numbers_carry_their_load.py` | The recorded runner block, the committed baseline and the gate's failure text | A run records no load reading and no absence of one, the committed baseline records none, a differing load turns the gate off, or a failure does not say what the load was |
| `test_the_benchmark_fingerprint_records_the_cgroup_memory_limit.py` | The cgroup `memory.max` reader on a number, `max`, an absent file, a non-numeric value and a directory, the runner block a run records, and the regression gate on a run whose memory cap differs from the baseline's | A limit is misread as a figure in GiB or as none, the memory cap joins `RUNNER_SPEC_FIELDS`, or a differing cap makes the gate refuse the run as another machine |
| `test_the_load_refusals_agree.py` | The headroom multiple and floor in the benchmark gate and in `cliffracer.testing.host_load` | The two refusals use different limits for a busy host |
| `test_benchmarks_doc_says_what_the_benchmark_does.py` | `docs/benchmarks.md` against what `scripts/run_benchmarks.py` writes for the committed baseline and history, the regression gate's threshold and statistic, and the benchmark functions' own bodies | The page differs from what the generator writes, the gate's threshold or the median it scores moves, a figure's described call is not the call measured, or a speedup is not the JSON time over the MessagePack time |
| `test_the_benchmark_page_reports_the_recovery_it_measured.py` | The kv row of the page `scripts/run_benchmarks.py` writes, from a baseline whose `stress_failure_handled` and `recovery_verified` flags are each true, false and absent | The row says "Verified" for a baseline that recorded no recovery, does not name the flag that is not true, or the committed page differs from what the committed baseline writes |

## Documentation and source prose

| Guard | Reads | Fails when |
|---|---|---|
| `test_docs_carry_no_history.py` | Every tracked `.md` | Prose carries an issue number, a commit hash, or narration about how the code came to be this way |
| `test_docs_state_scope_positively.py` | Every tracked `.md` | A document describes the framework by what it lacks rather than what it does |
| `test_source_carries_no_history.py` | Comments and docstrings under `src/` and `packages/` | Source prose narrates history, using the pattern set the documentation guard owns |
| `test_docs_code_blocks_resolve.py` | Fenced code in every document | A fence imports a module or names a symbol that does not exist |
| `test_the_dead_letters_page_names_what_a_record_holds.py` | `docs/dead-letters.md`, against the fields a delivery adds to a dead-letter record and the header names the publisher withholds | A field or a withheld header name exists in `DeadLetterPublisher` and the page does not name it |
| `test_the_service_templates_example_builds_its_child.py` | The Python fence in `docs/service-templates.md`, run | The example fails to register or construct, or the child it leaves is started or lacks the runtime the page says it is given |
| `test_the_upgrade_guide_covers_every_breaking_or_removed_entry.py` | Every `changelog.d/` fragment that starts `- **Breaking**` or `- **Removed**`, and the `<!-- changelog.d: ... -->` comments in `docs/upgrading.md` | A Breaking or Removed fragment has no section in the upgrade guide, or one fragment is the subject of two sections, or a section names a pending fragment that is neither Breaking nor Removed. A comment for a fragment the release assembly has deleted is accepted |
| `test_every_stream_spec_field_is_documented.py` | The "Stream fields" section of the API reference, against `StreamSpec` | A `StreamSpec` field has no line there, a line names a field that is gone, a stated default differs from the model's, the leading-wildcard rule has no section, or the section the guard reads is not found |
| `test_docs_carry_no_emoji.py` | Every tracked `.md` | A document carries an emoji |
| `test_docs_handlers_would_start.py` | Every documented service class, through one shared walk | A documented handler carries annotations that would fail at startup, or the walk stops finding the services it reports on |
| `test_generated_docs_are_in_sync.py` | Generated tables against the models they come from | A table drifts from the model it is generated out of |
| `test_docs_extension_count_matches.py` | The README extensions table against `packages/` | The table and the workspace disagree |
| `test_every_environment_variable_the_code_reads_is_in_the_readme.py` | The README's environment-variable table, against the variables `src/` and `packages/*/src/` read (`os.environ.get`, `os.environ[...]`, `os.getenv` with a literal name, and a settings class's `env_prefix`) | The code reads a variable the table does not name |
| `test_every_distribution_is_in_every_package_table.py` | The distribution table in `docs/ARCHITECTURE.md`, `docs/extensions.md` and `docs/api-reference.md` against `packages/` | A distribution under `packages/` is missing from, listed twice in, or extra in one of the tables |
| `test_the_extensions_guide_quotes_the_example.py` | The extensions guide against its worked example | The quoted block and the example diverge |
| `test_no_stale_project_urls.py` | Repository files, for project URLs | A file points at a legacy hostname |
| `test_adr_status_tracks_package_presence.py` | `docs/decisions.md`, by its `Package` bullets, against `packages/` | A decision that owns an extension says Accepted without it existing or Proposed while it does, or an extension a decision names and that exists is owned by no decision |
| `test_every_adr_a_file_cites_exists.py` | Every `ADR-nnnn` in every tracked text file, against the headings of `docs/decisions.md` | A file cites a decision that is not in the decisions file, as after a decision is removed with the feature it governed. One file that builds decisions as test data is exempt, with its reason |
| `test_adr_0018_says_which_of_its_machinery_exists.py` | ADR-0018's Implementation line, against the workflows and the source | What the record says is not built stops being true: no seed option, the chaos soak as the only scheduled workflow, nothing in the source reading a tier, no import warning |
| `test_adr_0019_says_what_is_not_built.py` | ADR-0019's status and Implementation line, against the source | The record says its two errors are not defined, and either is, or it stops saying it is Proposed |
| `test_the_connection_decisions_state_the_numbers_the_code_has.py` | The constants in `cliffracer.core.connection` and in the installed nats-py, against ADR-0007, ADR-0008 and the guides | A document states a bound, ping interval or shutdown cap that differs from the constant, or `ServiceConfig` exposes the ping settings the record says it does not |
| `test_every_config_field_is_described.py` | Every `ServiceConfig` field's description, and the table that pairs with it, read only between the generator's `service-config-fields` markers | A field has no description of its own, the table and the model name different fields, or the markers are missing |
| `test_source_names_no_session_identity.py` | Comments and docstrings across every tree, package tests included | A comment or docstring credits a finding to a session identity rather than to what the finding was |
| `test_the_guards_index_names_every_guard.py` | This document's table rows, against the guard modules in `tests/repo/` | A guard has no row, a row names a module that is gone, a row sits outside its table, or the one guard described by description is not exactly one module |

The history and scope sweeps read `docs/decisions.md` like any other document,
but neither can judge a decision's `Status` against what exists, so the
decisions guard does. Ownership of an extension is declared there in a
`Package` bullet rather than inferred from the prose: a decision may name a
distribution to say where something else belongs, and a sweep that read the body
would judge it as being about a package it merely cites. Deciding which of those
a sentence is doing would be the substring classification ADR-0011 forbids.

`scripts/check_commit_messages.py` applies the same patterns to the commit
messages in a pull request.

One guard is described here without its filename. It sweeps the whole tracked
tree for the name of a decorator that the framework has stopped offering, with
no path list, so that the name cannot be read anywhere as something a caller
could still use. The two files that legitimately name it are listed with an
exact count of the lines that do. Its own filename contains that name, which
means writing it in this table fails the guard. Look for it by that
description in `tests/repo/`; the omission here is the check working.

## Exemptions

Several guards carry an allowlist: documents outside a sweep, files that pin a
broker address, metrics scored by hand. Each entry records a reason, and each
allowlist has a test asserting its entries are load-bearing, so an entry whose
subject is gone fails rather than sitting there exempting nothing.

An allowlist that empties is removed along with the branch that reads it,
rather than kept as an empty list: the broker-address guard asserts its list is
not empty for that reason. Two lists are empty today and kept, because each is
the place a new entry goes with its reason: the documentation scope guard's
`EXEMPT_REASONS`, which a test asserts stays empty, and the exception guard's
`NOT_RAISED_BY_THE_LIBRARY`, whose entries are checked for staleness.
