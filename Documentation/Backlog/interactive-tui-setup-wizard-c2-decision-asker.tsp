// C2 — Pure-decision + injected-asker seam (internal logical seam; Presentation ↔ Use Cases).
// Generalises the existing _prompt_provider(ask_choice=...) pattern: each
// _prompt_*/_select_*/_pick_* splits into a pure decision function that takes an
// injected asker and returns a decision, plus the asker itself. The three
// front-ends supply different askers; the decision logic lives once.
namespace DecisionAskerContract;

// D4/D13: thread C1's typed enums through C2's decision returns (repeated
// locally — self-contained per-file authoring, since each contract must
// compile standalone per the plan's rule, `tsp compile <file> --no-emit`;
// the shared enums, e.g. ProfileName, are repeated in each file and MUST be
// kept in sync) so "a profile decision" is ONE type everywhere, matching
// WizardChoicesContract.ProfileName in C1. Authoritative copy = C1; keep
// this copy in sync.
enum ProfileName {
  minimal,
  balanced,
  max,
}

// D2: KEEP the two-callable Asker shape (Q11 updated — this contract was
// already correct). The `AskRequest(kind, …)` descriptor considered as an
// alternative is speculative: out-of-scope free-text/Ollama-multi-field cases
// don't ship in v1, and the multi-field case is stateful/re-entrant, not a
// pure single call. The codebase already proves the cheaper pattern; a
// kind-switch descriptor is more machinery than ~6 helpers justify.
//
// The injected asking primitive, unified from _prompt_provider's ask_choice and
// the welded closures inside _prompt_optional_features.
interface Asker {
  // `valid` is logically a SET of unique options — Python's real
  // _prompt_provider(ask_choice: Callable[[str, set[str], str], str]) at
  // install.py:1145 takes set[str]. TypeSpec has no set type, so this is
  // modeled as string[] with the invariant that callers pass unique values
  // and order carries no meaning.
  askChoice(promptText: string, valid: string[], defaultValue: string): string;
  // Y10 NOTE (AUTHORITATIVE COPY — C4 DecisionRequester.requestDecision
  // points back here, not vice versa): askYesNo is THE single boolean-ask
  // primitive in this system. C4's cockpit implements requestDecision by
  // DELEGATING to this same askYesNo — one modal implementation, two entry
  // points: question-time via this injected asker (plain flow, ordinary
  // prompts) vs mid-flow via the C4 worker seam (cockpit, while the worker
  // thread is running — e.g. the overwrite/Proceed confirms). requestDecision
  // is askYesNo's mid-flow/worker-thread entry point; it supplies the
  // confirm's baked-in default itself rather than taking one as a parameter.
  // Do not build two separate modal implementations.
  //
  // Asymmetry: askYesNo takes a defaultValue, requestDecision does not.
  // requestDecision is realized as two thin CALL-SITE wrappers, not one
  // generic entry point: the overwrite site (install.py:2308) hardcodes
  // default=No, the Proceed site (install.py:2414) hardcodes default=Yes —
  // each wrapper bakes in its own default before delegating to askYesNo.
  // Adding a defaultValue param is a candidate follow-up, not required for
  // the current two call sites. Do NOT change either method's signature —
  // this note is the fix, not a code change.
  askYesNo(promptText: string, defaultValue: boolean): boolean;
}

// D2 (X18): the pure-decision/asker split applies only to helpers with
// non-trivial decision logic (optional-features matrix, GPU intent). Trivial
// picks (profile, multilingual) may call the asker inline — do not manufacture
// an interface per prompt (consistent with Q2's per-slice unwelding).
//
// A pure decision function: given an asker and current inputs, returns the chosen
// value without owning any I/O of its own.
interface ProfileDecision {
  decide(asker: Asker, profileFlag: string | null, nonInteractive: boolean): ProfileName;
}
