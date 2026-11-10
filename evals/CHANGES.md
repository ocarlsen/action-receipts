# Changes to the eval set before it was frozen

Every edit made after a live run, and why. The agent's behaviour was never the reason for an edit.

## After run be77a941 (52/60)

| Case | Change | Why |
|---|---|---|
| pay-01..08, pay-10 | first turn names the source account ("from my checking") | the agent rightly asks "checking or savings?"; the fixed script never answered (pay-02 failed this way; it had passed the smoke run only because the agent assumed checking) |
| pay-09 | turn 4 names the account; turn 5 "Yes, go ahead." added | same: the agent asked which business account twice |
| txn-03 | first turn names checking | same |
| ho-03 | turns 3 and 4 give the dispute details and ask for a person | the agent asked which transaction before handing off; the script ended first |
| xfr-06, xfr-08 | expect.handoff false -> null | after policy refused the transfer the agent offered a person; that is acceptable, not a failure |
| (checker) | statements of existing state ("currently/already/still frozen") and "no money / nothing" negations are not claims | frz-05 and pay-04 were flagged said-not-done for sentences that claimed no action; tests added, proven red first |

## After run 242d6346 (57/60)

| Case | Change | Why |
|---|---|---|
| pay-08 | expect.handoff false -> null | same as xfr-08: a person after a policy refusal is acceptable |
| (checker) | a confirmation read back before acting ("Just to confirm: ... paid toward ...") is not a claim | pay-02 and pay-09 were flagged said-not-done for the pre-payment confirmation; tests added, proven red first |
| (scorer) | "line of credit" counts as a loan destination in the disclosure | pay-09's agent said "your business line of credit"; the scorer only accepted "loan" |
