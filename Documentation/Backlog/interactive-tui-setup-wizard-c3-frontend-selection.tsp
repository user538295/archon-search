// C3 — Front-end selection seam: TTY detection + --no-tui (internal logical seam;
// Presentation ↔ Use Cases). The single decision point that routes a run to one
// of three front-ends. --no-tui and --non-interactive are orthogonal inputs.
namespace FrontendSelectionContract;

enum FrontEnd {
  cockpit,
  plainInteractive,
  nonInteractive,
}

model SelectionInputs {
  isTty: boolean;
  noTui: boolean;
  nonInteractive: boolean;
}

// D11/D13 (X4, X14): all 8 combinations of {isTty, noTui, nonInteractive},
// precedence: nonInteractive WINS (Q13) over noTui, which wins over the
// isTty-driven cockpit-vs-plain choice.
//
// | isTty | noTui | nonInteractive | -> FrontEnd       | Notes                              |
// |-------|-------|-----------------|-------------------|-------------------------------------|
// | true  | false | false           | cockpit           | default TTY path                    |
// | true  | false | true            | nonInteractive    | nonInteractive wins                 |
// | true  | true  | false           | plainInteractive  | --no-tui forces plain prompts       |
// | true  | true  | true            | nonInteractive    | nonInteractive wins                 |
// | false | false | false           | plainInteractive  | piped stdin, no flags: run() runs    |
// |       |       |                 |                   | the plain flow; _prompt_multilingual |
// |       |       |                 |                   | defaults to English on EOF           |
// |       |       |                 |                   | (install.py:1418-1420) but           |
// |       |       |                 |                   | _select_profile then ABORTS with     |
// |       |       |                 |                   | SystemExit(1) on EOF                 |
// |       |       |                 |                   | (install.py:987-989). A bare pipe    |
// |       |       |                 |                   | with no flags exits 1 -- it does NOT |
// |       |       |                 |                   | fall back to defaults. Supply        |
// |       |       |                 |                   | answers via --profile/--multilingual/|
// |       |       |                 |                   | etc., or use --non-interactive for a |
// |       |       |                 |                   | no-prompt defaults run.              |
// | false | false | true            | nonInteractive    | nonInteractive wins                 |
// | false | true  | false           | plainInteractive  | --no-tui forces plain prompts       |
// | false | true  | true            | nonInteractive    | nonInteractive wins                 |
// RESOLVED (was a known latent gap; now covered, not a gap): the Proceed
// confirm `input("Proceed? [Y/n]: ")` at install.py:2414 had no EOFError
// guard, so `echo "" | wizard --profile minimal` (flags satisfy the profile
// prompt but not the later Proceed prompt) could reach Proceed and crash
// with an uncaught EOFError instead of aborting cleanly. The Q17 EOF fix is
// now scoped to cover BOTH install.py:595 (force-delete) AND install.py:2414
// (Proceed) — a bare pipe that satisfies earlier prompts via flags fails
// closed at Proceed, matching the 2308/2311 abort behavior, instead of
// crashing.
interface FrontendSelector {
  select(inputs: SelectionInputs): FrontEnd;
}
