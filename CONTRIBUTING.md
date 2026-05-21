# Contributing

The script is small enough that there's no formal process. Open an issue if
you've hit a wall or want a feature, send a PR if you have a fix.

## Bug reports

Use the "Bug report" issue template. The two things that almost always make
the difference are:

1. **The output with `-v`.** That prints every HTTP request, status code, and
   the short form of any VCD error body. Without it I'm guessing.
2. **The VCD version and API version.** Behaviour differs between 10.3 / 10.4 /
   10.5, sometimes in ways the script papers over (or fails to).

If the script itself crashed (Python traceback), please paste the whole thing.

## Feature requests

Use the "Feature request" template. The most useful thing you can include
beyond "what" is **why**: what concrete scenario in your environment makes
this matter. That's how I decide whether to invest time on it vs. a workaround.

If you already know the VCD CloudAPI endpoints involved, please drop the paths
in the issue. Half the work on this script was just figuring out which
endpoint actually accepts a given operation.

## Pull requests

For small fixes (typos, error messages, a missing tolerate code) just open the
PR. For anything bigger — a new resource type, a new mode, a refactor — open
an issue first so we don't end up with two people writing the same thing.

A few conventions:

- Keep the script API-only. If the only way to clean something up is via the
  database, that's a "report and stop" case, not "do it anyway".
- New resource types go in `discover()` (find them), `delete_*` (remove them),
  and the order in `run_action()` (where in the dependency chain).
- Log each step with `step("verb", target)` so `-v` output stays useful.
- Use `tolerate=(404,)` (or similar) on idempotent retries instead of
  swallowing exceptions blindly.
- Comments in English. Docstrings are short — a sentence or two saying *why*,
  not what (the function name says what).

## Testing

There's no test suite — the only realistic test is running it against a real
VCD lab tenant. When you change deletion logic, do a `--dry-run` first to
confirm discovery is finding what you expect, then a real delete on a throwaway
org. If you can't easily get a lab, mention that in the PR — I can test it
against mine.

## Tested environments

The script is exercised against VCD 10.4 (API 37.0), NSX-T edges, CAPVCD k8s
clusters, and Veeam B&R 12 integration. If you're running an older version or
NSX-V, your contribution is especially welcome — those paths aren't covered.
