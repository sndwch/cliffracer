"""`_redact_pairs`, as written or with its end test removed, in a process of its own.

`python -m tests.fixtures.redact_pairs_process SCENARIO` prints `RETURNED <text>` or
`RAISED <type> <message>`. `as_written` runs the function as it is. `never_ends` runs a copy built
from its source with `if end < 0:` read as never true, so the loop goes back to the first pair
instead of returning after the last one. The process caps its own address space, so a copy whose
loop grows its list without end fails here with MemoryError instead of taking memory from the host.
"""

import inspect
import resource
import sys
import textwrap

from cliffracer.core import endpoints

#: The address space this process may hold. The process starts at about 85 MB of it. With the end
#: test removed and no bound, the copy grows about 11 MB a second and fails here with MemoryError
#: after about 20 s; as written it returns in well under a second.
ADDRESS_SPACE = 256 * 1024**2

PAIRS = "a=1&password=x"


def redact_pairs(scenario: str):
    source = textwrap.dedent(inspect.getsource(endpoints._redact_pairs))
    if scenario == "never_ends":
        if source.count("if end < 0:") != 1:
            raise SystemExit(
                "the end test is no longer `if end < 0:`; this scenario must follow it"
            )
        source = source.replace("if end < 0:", "if False:")
    elif scenario != "as_written":
        raise SystemExit(f"no scenario {scenario!r}")
    namespace = dict(vars(endpoints))
    exec(compile(source, endpoints.__file__, "exec"), namespace)
    return namespace["_redact_pairs"]


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE, ADDRESS_SPACE))
    function = redact_pairs(sys.argv[1])
    try:
        print("RETURNED", function(PAIRS), flush=True)
    except Exception as exc:
        print("RAISED", type(exc).__name__, exc, flush=True)
