// C5 — Screen decision-return seam (internal logical seam; Presentation ↔ Entities).
// The prototype navigation messages (NextScreenRequest / PrevScreenRequest) carry
// no payload and selection dies with the widget.
//
// D4 (Q12 wins, X7): the stringly-typed DecisionRoute.advance(ScreenAdvance{
// screenId, fieldName, chosenValue}) indirection is REMOVED. Instead, this is
// the APP-HELD DIRECT-WRITE pattern: each cockpit screen writes its own typed
// field directly onto the App-held WizardChoices (from C1,
// WizardChoicesContract) as it advances — there is no generic routed-message
// layer between a screen and the answer-sheet. A screen that collects the
// profile writes `app.choices.profileName` (typed ProfileName) itself; there
// is no `fieldName: string` / `chosenValue: string` indirection to keep in
// sync with C1.
namespace ScreenDecisionContract;

// D4/D13: thread C1's typed enums through C5 (repeated locally —
// self-contained per-file authoring, since each contract must compile
// standalone per the plan's rule, `tsp compile <file> --no-emit`; the shared
// enums, e.g. ProfileName, are repeated in each file and MUST be kept in
// sync) so "a profile decision" is ONE type everywhere.
enum ProfileName {
  minimal,
  balanced,
  max,
}

enum ReRunChoice {
  keep,
  reconfigure,
  deleteAndStartOver,
}

// The App-held answer-sheet a screen writes into directly as it advances.
// Mirrors WizardChoicesContract.WizardChoices's relevant fields; kept minimal
// here since C1 is the authoritative full shape.
model AppState {
  choices: AppStateChoices;
}

// Note: proceedConfirmed (and, elsewhere on the full C1 sheet,
// overwriteConfirmed) is App-held but obtained MID-FLOW via C4
// requestDecision — it is not written by a screen's advance() like
// profileName is (consistent with C1's Y3 note and C4's DecisionRequester).
model AppStateChoices {
  profileName: ProfileName;
  proceedConfirmed: boolean;
}

// A cockpit screen writes its own typed field directly onto AppState.choices
// as it advances — this is a documentation-only illustrative seam, not a
// generic dispatch interface. No DecisionRoute / ScreenAdvance message is
// kept: there is nothing left to route through once the write is direct.
interface ProfileScreen {
  // On advance, the screen sets app.choices.profileName = <selection> directly,
  // then transitions to the next screen. No return value carries the decision
  // back out — the write already landed on the shared, typed answer-sheet.
  advance(app: AppState, selection: ProfileName): void;
}

// D4: the re-run screen. `deleteAndStartOver` routes to
// _execute_force_reinstall (install.py:572). Q18: `keep` is a no-op exit that
// writes nothing (S18); `reconfigure` is an idempotent re-write / self-heal
// (S19). `deleteAndStartOver` additionally requires a typed-yes confirmation
// before the destructive path runs — modeled here as a second, explicit
// boolean rather than folding it into ReRunChoice, so the confirm cannot be
// silently skipped by construction.
interface ReRunScreen {
  // For deleteAndStartOver, `confirmed` must be true (typed-yes) before the
  // caller invokes the equivalent of _execute_force_reinstall; for keep /
  // reconfigure, `confirmed` is ignored.
  reRun(choice: ReRunChoice, confirmed: boolean): void;
}
