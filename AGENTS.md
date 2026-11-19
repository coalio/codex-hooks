# Global Codex Guidance

## Usage tool

The `codex_usage` MCP server exposes the `usage` tool, its setup and JSON contract
are documented in `tools/usage/README.md`

Before enabling `use_reset_automatically`, tell the user that lifecycle cleanup
will stop the hook once work is complete, `keep_running` may be set only when
the user explicitly requests continuation, interruption and session end still
expire it

The worker owns polling and reset decisions, agents should not poll in a loop,
the originating `CODEX_THREAD_ID` supplies `thread_id`, never the selected UI
conversation, explicit `expire_hooks` applies to the caller's hook

## Context-aware architecture review gate

Do not proactively spawn an architecture gate agent after repository work. The global
Stop hook owns routing because Stop matchers are not applied by Codex. It permits work
outside Git, sessions with no net repository change, and the repository whose initial
and current normalized canonical remote is `github.com/GreysteelCompany/salesforce`
without a model call.

For other changed repositories, obey the Stop hook's exact instruction, obvious
engineering changes first go to a fresh `service_contract_reviewer`, followed by a
fresh `architecture_reviewer`, ambiguous changes go to `architecture_scope_classifier`
and enter both review stages when architecture review is required

Each role receives no inherited conversation history, pass only the compact original
request and the machine context supplied by Stop, including the protected delta and,
for the contract specialist, the structural inventory and its hash

An `ARCHITECTURE_NOT_REQUIRED` classifier verdict approves only its exact fingerprint.
An `ARCHITECTURE_REQUIRED` verdict requires both review stages, an architecture `PASS`
cannot replace the contract specialist's required evidence, a `FAIL` is blocking: fix every
actionable finding, validate, then attempt to finish so Stop refreshes the fingerprint,
manifest, protected artifact and structural inventory before routing a fresh reviewer. Never reuse an old
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
validation. For paths dirty at session start, all four roles must use the protected
session-delta artifact as the authority for this session's content and index changes.
Sessions outside Git or with no net repository change do not require a conclusion.

All four custom agents are read-only by instruction, their permissions still inherit
from the parent session, the architecture reviewer may publish review
comments only when the user explicitly requests that external write and identifies
the exact open pull request; that exception never permits repository edits or other PR
changes. The contract specialist, classifier and conclusion writer never publish anything.


## Responsibility ownership and service structure

Organize shared code around stable domains, integration providers, and cohesive
capabilities. Choose the broadest accurate owner while keeping each service focused
on one responsibility; maximum generality and file size are not design goals.

Before adding or extending a service, inspect its existing owner and neighboring
capabilities, including relevant services introduced by earlier changes. Use one
service while the responsibility remains cohesive. When distinct capabilities need
independent services, group them within a common domain or provider namespace and
name each service for the capability it owns. Do not leave a broad owner service
beside independently implemented feature or subdomain services for that same owner
at the shared layer. Consolidate the implementations or establish the common
namespace and give its peers explicit responsibilities. A public facade may expose
the whole owner's contract by delegating to services within that namespace.

A provider namespace groups integration implementations; it does not imply one
business domain, one shared model, or one deployable service. Keep workflow-specific
decisions and orchestration with their owning feature layers, reusable domain rules
with their domain owner, and provider transport/authentication with the integration
boundary. Preserve the repository's documented layer contracts.

Apply the same principle to configuration and CI: shared quality checks express
repository policy, with scope selected through shared configuration or discovery.
Do not create permanent feature-named quality gates or allowlists as each feature
arrives. Preserve existing strictness and checked coverage when consolidating them;
an incremental rollout needs an explicit migration scope and completion condition.

A rename or folder move is insufficient if overlapping responsibilities, duplicated
implementation, or feature-coupled contracts remain. Review actual imports, public
contracts, consumers, and reasons to change. One consumer can justify a well-defined
owner; hypothetical reuse does not justify speculative abstraction. Report concrete
ownership violations in the affected boundary without demanding unrelated cleanup.

## Service and contract checks

The service-contract stage explicitly validates five rules against every affected
owner, new folder and file name, changed signature and type, its evidence is required
before a pass and is bound to the exact structural inventory and repository fingerprint

1. Service justification, the service owns a real domain capability, provider boundary,
   lifecycle or invariant, workflow-specific decisions and sequencing remain with the
   workflow or its owning feature layer, extraction needs a reason beyond convenience
2. Responsibility, the owner follows the actual objects and reasons to change, listing,
   report and activity capabilities retain distinct ownership even when one workflow
   combines them into an audience
3. Naming, parameters and return types, the signature must describe the operation
   without caller context, retrieval returns the named object, an export advertises
   its file result and side effects, misleading contracts are architectural defects
4. Type cohesion, distinct domain objects have distinct contracts, optional-field bags,
   broad dictionaries and artifact wrappers must not erase object identity or invariants,
   a shared file envelope belongs at serialization boundaries
5. Domain generality, names and contracts survive removal of the current workflow and
   addition of related operations, company change and deletion belong to company
   ownership with workflow policy kept outside shared provider mechanics

The specialist profile contains the SalesforceAudienceService and
SalesforceCompanyChangeService anti-patterns, including retrieval methods returning
SalesforceAudienceArtifact, examples must be checked against actual implementation
and cannot be reduced to a name blacklist or fixed by renaming mixed responsibilities

These repository conventions apply the following Microsoft guidance. The sources
support the principles; they do not prescribe our exact names, folders, or CI layout.
Microservice boundary reasoning is adapted to in-process components without requiring
distributed deployment.

- Architectural principles: separation of concerns, encapsulation, and single responsibility.
  https://learn.microsoft.com/en-us/dotnet/architecture/modern-web-apps-azure/architectural-principles
- Design for evolution: high cohesion, loose coupling, and separation of domain logic from infrastructure.
  https://learn.microsoft.com/en-us/azure/architecture/guide/design-principles/design-for-evolution
- Identify microservice boundaries: domain-led boundaries and pragmatic, initially coarse granularity.
  https://learn.microsoft.com/en-us/azure/architecture/microservices/model/microservice-boundaries
- Use domain analysis to model microservices: business capabilities, bounded contexts, and explicit external integrations.
  https://learn.microsoft.com/en-us/azure/architecture/microservices/model/domain-analysis
