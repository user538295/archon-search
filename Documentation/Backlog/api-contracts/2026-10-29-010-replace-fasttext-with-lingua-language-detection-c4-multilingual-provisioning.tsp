// C4 — Multilingual install provisioning seam (user-facing CLI and wizard contract).
//
// Layer edge: Presentation (`archon-search install` options and the wizard summary)
// -> Use Cases (`install/installer.py`) -> Frameworks & Drivers (the `multilingual` extra install).
//
// Today the wizard, for a multilingual profile, asks the operator to accept the CC-BY-SA licence
// (or pass `--accept-fasttext-license` when non-interactive), downloads and verifies `lid.176.ftz`,
// and degrades to English-only if the download fails. After this change none of that exists:
// provisioning for language detection is the extra install alone, which already reverts
// `multilingual` to off if it fails. The option is REMOVED, not accepted-and-ignored (clean
// cutover); scripts that still pass it get the CLI's unknown-option error, and BREAKING.md says so.
//
// Whether the wizard keeps a separate "language detection" bullet and step is open (Q5); the
// summary wording, if kept, must not name fasttext.
//
// Logical only.

namespace MultilingualProvisioningContract;

model InstallOptions {
  /// Chosen multilingual profile / `--multilingual`.
  multilingual: boolean;

  /// No prompts. With the licence gate gone, nothing about language detection needs an
  /// acceptance flag in this mode.
  nonInteractive: boolean;
}

union ExtraInstallOutcome {
  /// The `multilingual` extra installed; detection will work at next start.
  installed: "installed",

  /// The extra could not be installed; `multilingual` is written as off and the server boots
  /// English-only (the existing revert behaviour, unchanged).
  revertedToEnglishOnly: "reverted_to_english_only",
}

interface MultilingualProvisioning {
  provision(options: InstallOptions): ExtraInstallOutcome;
}
