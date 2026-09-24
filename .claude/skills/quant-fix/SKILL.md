---
name: quant-fix
description: >
  Use this whenever the user reports or pastes something from this repo's live trading
  system — a Telegram alert screenshot, a `--analyze`/성적표 report, a market/journal excerpt
  they're comparing against the bot, or a plain "이거 왜 이래" / "헛점 없나 점검해줘" — even
  without an explicit question. Also use it proactively whenever you're about to fix a bug in
  macro_watcher.py or quant_dashboard.py, or asked to audit either file for similar structural
  issues. This skill is the required process for that work in this repo: root-cause the real
  code (never guess), check the whole codebase for the same defect class before calling it done,
  verify every fix with a mock test before pushing, log it in HANDOFF.md in the repo's existing
  format, and ship it through the repo's PR workflow. Do not skip this skill for "quick" fixes —
  the quick-looking ones are exactly where an unverified push burns the user's trust in a system
  they trade real money against.
---

# Fixing macro_watcher.py / quant_dashboard.py

This repo is a live Telegram trading-alert bot (`macro_watcher.py`) and its Streamlit dashboard
(`quant_dashboard.py`), run by a non-technical Korean retail trader against real money. The user
can't read the code themselves — they find problems empirically, by noticing something in a
real Telegram message or report that doesn't match what they expect. Every fix you make either
earns back or spends their trust that the alerts are worth acting on. Move deliberately.

## 1. Take the report seriously, even without a question

The user often just pastes a Telegram message, a weekly `--analyze` report, or a trading-journal
excerpt from someone else, with little or no explicit question. Treat this as "investigate
whether our system handles this correctly" by default — that has been the standing expectation
all session. Don't wait to be asked "is this a bug?".

## 2. Find the actual root cause in the code

Grep first for the relevant function names/constants, then read the specific function bodies —
this file is large (7000+ lines), so don't try to read it end to end. Trace the real logic path
that would have handled (or failed to handle) the reported case. Never explain a discrepancy by
guessing ("maybe it's a timing issue") when you can instead find the line that proves it.

If the explanation depends on data you don't have access to in this sandbox — the user's actual
`macro_watcher_state.json`/`signal_scorecard.json` from their PC, real KIS API responses, what
was actually in that day's lineup — say so explicitly rather than presenting a guess as fact.
(The JSON/lock files present in this checkout are stale sandbox artifacts, not the user's live
data — don't treat them as evidence about what actually happened on the user's machine.)

## 3. Before calling a fix done, check for the same defect class elsewhere

This is the single most valuable habit this session established. Three times in a row, a bug
found in one function turned out to be the same pattern repeated in several other functions:
a narrow hardcoded scan universe (`lineup`, 6 stocks) found in `check_nxt_after`, then found
again in four more alert functions (`check_snipers`, `check_supply_turn`, `check_bar15`,
`check_entries`), then found a third time under a completely different name (`TA_UNIVERSE`, 11
stocks) in `check_turnaround`. Each time, fixing only the first instance and stopping would have
left the user with the same complaint a day later.

So: once you understand *why* something was a bug, grep for the same shape elsewhere (same
constant used in multiple functions, same silent-`continue`-on-no-match pattern, same
day-not-reset pattern, same threshold hardcoded in one place but not a sibling function) before
reporting back. If you find a structurally similar case that's ambiguous — e.g. a fixed list
that might be *intentionally* narrow for a real risk reason (see `SECTORS`/`SECTOR_LEADERS`,
which are deliberately small representative-sector-leader lists, not scan universes, and were
already reviewed and left alone once) — say so and let the user decide rather than changing it
silently.

## 4. Verify with a mock test before you push — always

Never push a change to this repo on the strength of reading the code alone. This sandbox has no
live KIS/Telegram/Gemini credentials, so "verify" means: write a throwaway Python script that
monkeypatches the specific functions the fix touches (commonly `_volume_rank`,
`_price_and_turnover`, `_investor_est`, `send_telegram`) with fake data, and confirms the new
code path actually fires (or doesn't) as intended. Where the user gave you real numbers (a
journal excerpt, a screenshot), use those numbers in the mock rather than arbitrary ones — that
lets you tell the user "I replayed your exact case and it fires" instead of a vaguer claim.

Also run `python3 -m py_compile macro_watcher.py` (and `quant_dashboard.py` if touched) before
every commit — it's free and catches typos the mock test's happy path might not exercise.

Report the verification honestly. If something can't be verified end-to-end in this sandbox
(e.g. whether a specific stock would actually have been in that day's real top-40 ranking), say
exactly what was and wasn't confirmed instead of rounding up to "confirmed working."

## 5. Log it in HANDOFF.md

New entries go at the **top** of section `## 7. 다음 작업(대기)`, in reverse-chronological order.
Before writing, run `grep -n "^### 7\.[0-9]*" HANDOFF.md | head -5` to find the current highest
number (they're inserted at the top, so the highest number is near the top of section 7, not the
bottom of the file) and use the next integer.

Match the existing entries' shape and tone — terse, evidence-heavy Korean, no fluff, concrete
line/number references, not a marketing summary:

```
### 7.NN <one-line 제목>

**배경**: 어떻게 발견됐는지(사용자 제보/실데이터/재감사) + 무엇이 실제 원인이었는지, 파일:줄번호 인용.

**수정**: 정확히 뭘 바꿨는지 — 새 함수/헬퍼를 만들었으면 그 이유, 기존 패턴 재사용했으면 어떤 걸.

**검증**: py_compile 결과 + 모의 테스트 시나리오와 결과. 확인 못한 부분이 있으면 명시.
```

## 6. Ship it through the repo's PR workflow

This repo works on a single long-lived branch (`claude/quant-dashboard-check-i8he8u`) that gets
PR'd against `main` repeatedly. Every time, in order:

1. `git fetch origin main`, then confirm your branch is rebased on the latest main
   (`git merge-base --is-ancestor <your-head> origin/main`). If it's behind, stash, rebuild the
   branch from `origin/main`, and pop the stash before committing — don't commit on a stale base.
2. Commit with a Korean message describing the root cause and the fix (not just "fix bug"), and
   end it with the attribution footer given to you in this session's own system reminder
   (`Co-Authored-By:` / `Claude-Session:` lines) — copy it from there, don't reuse an old one
   from memory, since it's session-specific.
3. `git push -u origin claude/quant-dashboard-check-i8he8u`.
4. Open a PR via the GitHub MCP tools, title and body in Korean, body with a `## Summary` (what
   was found + what changed) and a `## Test plan` checklist (compile + mock test, checked; live
   verification in the user's real environment, unchecked — be explicit that's still pending).
5. Subscribe to PR activity, then wait for the notification — never poll or sleep for CI.
6. When it arrives, check runs + reviews, merge if green, unsubscribe.

Only go through this full flow for an actual code change. A pure question (like "what is a
skill" or "does the lock file need deleting") doesn't need a branch, commit, or PR — just answer
it.

## 7. Report back honestly, in Korean

Tell the user concretely what was found and fixed, referencing the real numbers/cases involved
where you can. If part of the picture is genuinely unverifiable from here (no live data access,
no real historical ranking snapshot, etc.), say that plainly instead of implying full certainty
— this system is trusted with real trading decisions, and an overclaimed fix is worse than an
honestly incomplete one.

## What not to do

- Don't widen a fix beyond the actual defect — e.g. don't add new alert types, new thresholds,
  or speculative "while I'm here" features. Match the existing code's style (Korean comments,
  existing helper patterns like `_lineup_plus_turnover`) rather than introducing a new one.
- Don't treat every fixed list in the codebase as a bug — some (like `SECTORS`/`SECTOR_LEADERS`)
  are deliberately scoped and already reasoned about; changing them needs the user's call.
- Don't commit, push, or open a PR for something you haven't run the mock test on.
