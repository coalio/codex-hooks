# Global Codex Guidance

## Context-aware architecture review gate

Do not proactively spawn an architecture gate agent after repository work. The global
Stop hook owns routing because Stop matchers are not applied by Codex. It permits work
outside Git, sessions with no net repository change, and the repository whose initial
and current normalized canonical remote is `github.com/GreysteelCompany/salesforce`
without a model call.

For other changed repositories, obey the Stop hook's exact instruction. It sends
obvious engineering changes directly to a fresh, independent
`architecture_reviewer`, and sends ambiguous or apparently non-engineering changes to
a fresh `architecture_scope_classifier`. Spawn either role with no inherited
conversation history, passing only the requested compact original-request summary,
the machine-generated session-delta manifest, and the classifier reason when present.

An `ARCHITECTURE_NOT_REQUIRED` classifier verdict approves only its exact fingerprint.
An `ARCHITECTURE_REQUIRED` verdict requires the architecture reviewer. A direct
architecture `PASS` is valid without classification. A `FAIL` is blocking: fix every
actionable finding, validate, then attempt to finish so Stop refreshes the fingerprint,
manifest, and protected artifact before routing a fresh reviewer. Never reuse an old
handoff after a file change. Repeat until the exact current state receives `PASS`.
Never let the implementation agent review its own work, and do not modify repository
files after a verdict or `PASS` that is meant to approve them.

There is no agent-callable Salesforce bypass. For Salesforce work outside the exempt
canonical repository, only the operator may manually disable the hooks through
`/hooks` and re-enable them afterward.

After the current fingerprint is exempt or has its required classifier/reviewer
approval, Stop requires a fresh `conclusion_writer` with no inherited history. Pass
it the machine context from Stop plus a concise completion ledger containing the
original request, material decisions, every problem or failed attempt and its
resolution, validation commands/results, commits, deployments, and remaining operator
actions. Its response is the final answer: return the complete response instead of
replacing it with a shorter "work completed" or "reviews passed" message. A conclusion
created before the final approval, in an earlier parent turn, or before a later
repository edit is stale.

The conclusion writer is pinned to `gpt-5.6-terra` with high reasoning and is
read-only. It must inspect the session manifest and exact diff, explain what changed
and how, pair every encountered problem with its resolution, and report concrete
validation. For paths dirty at session start, all three roles must use the protected
session-delta artifact as the authority for this session's content and index changes.
Sessions outside Git or with no net repository change do not require a conclusion.

All three custom agents are read-only. The architecture reviewer may publish review
comments only when the user explicitly requests that external write and identifies
the exact open pull request; that exception never permits repository edits or other PR
changes. The classifier and conclusion writer never publish anything.
