"""Scripted wizard answers shared by tests that feed ``input()`` a fixed sequence."""

# The collection-description question (FE-7) is asked in every interactive run, after the
# HyDE / RAG Fusion / graph questions, whatever flags were given; `n` declines it. Append this to a
# scripted run that does not care about descriptions; `Proceed?` and later prompts follow it.
DECLINE_DESCRIPTION = "n"

# A script that declines `Proceed?` ends `n` + this sentinel. Aligned, the sentinel is never read.
# If a prompt were added or removed ahead of it, `Proceed?` would read the sentinel instead of the
# `n` (or an EOF that also reads as no) and the run would succeed, so the test's exit-code check fails.
MISALIGNMENT_SENTINEL = "y"
