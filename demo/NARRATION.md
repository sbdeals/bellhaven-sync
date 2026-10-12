# Narration: Bellhaven CRM review demo

Video: `demo/output/bellhaven-demo.mp4` (2:42, 1280x800, no audio). Timestamps are the real
cue points from the recording run (`demo/output/timestamps.json`, video time). Start each line
at its timestamp. The whole script is about 340 words, a little over two minutes at a steady pace.

| mm:ss | Scene | What the viewer sees | What to say |
|---|---|---|---|
| 00:00 | 1. Queue (snapshot) | Review queue in snapshot mode: Pending 28, Confirmed 18. The cursor passes over the status tabs and the kind chips. | "The first half replays the day-one queue against the captured CRM snapshot, because the live copy is already corrected. The second half approves a real change live." |
| 00:07 | 2. Tiffin, `chow_reparent` | Detail page and its rule. Slow scroll: website vs CRM table, SOP billing check ($84,000 revenue, $12,400 AR), "What Approve will do" (CREATE, then PATCH). A note gets typed. | "Tiffin sits under Cedar Trail with $84,000 in revenue and $12,400 of open AR, so the change-of-ownership SOP applies. Approve creates a new Bellhaven account and points the old one at it." |
| 00:24 | 2. Tiffin, applied | Approve clicked. Green result card: 2/2 steps, new account created. | "Applied: the new account, then the pointer. In snapshot mode this only writes to a local file." |
| 00:32 | 3. Union Square, `create` | Create proposal. Scroll to the "possibly related" Juniper Point column and the note explaining why it was not matched. Back to the queue with no decision. | "Union Square is a create. The Juniper Point account shares the city and zip, but the street, phone and administrator differ, so it's only possibly related." |
| 00:45 | 4. Erie, `duplicate` | Duplicate filter, then Erie. Rule text, then the duplicate (retire) and survivor (kept) columns and the survivor-ordering note. Note typed. | "Erie has a Bellhaven copy and a Harborview copy at the same address. The survivor rule keeps the Bellhaven copy and inactivates the other one. I'm not sure yet, so I'll reject it." |
| 00:59 | 4. Erie, rejected | Back on the queue: "Rejected. Nothing was written to the CRM." Rejected tab shows 1. | "That writes nothing to the CRM. The rejection and my note are stored." |
| 01:06 | 5. Runs (snapshot) | Run #1: 35 website locations, 121 CRM accounts, 46 proposals. | "Every run is logged: 35 website locations, 121 CRM accounts, 46 proposals." |
| 01:12 | 6. Queue (live) | Live app: Pending 2 (Sandusky `chow_out`, Harborview `parent_acquired`), Confirmed 35. | "Now the live CRM. It's already corrected, so only two items are pending." |
| 01:20 | 7. Sandusky, `chow_out` (live) | "Not on the website" badge. Slow scroll: Millstone Care of Sandusky at the same 2715 Columbus Ave, revenue $130,000 / AR $5,200, the existing note from the earlier review, and the single PATCH. Note typed. | "Sandusky is gone from the website, Millstone Care of Sandusky is at the same address, and an earlier review already flagged it. With $130,000 in revenue and $5,200 of open AR, the SOP says keep the status and parent and just set the CHOW pointer." |
| 01:37 | 7. Sandusky, applied | Approve clicked. "Applied to the CRM 1/1 steps": PATCH, updated chow_current_account and note. | "That's one PATCH to the live CRM, and it's applied." |
| 01:44 | 8. CRM browser | Meridian CRM page for Bellhaven of Sandusky: CHOW current account `0017JP8Z1UQ763BVK3`, status Needs Review, appended note. | "In the CRM, chow_current_account now points at Millstone's account. The status is still Needs Review, and the note explains why." |
| 01:56 | 9. Harborview, `parent_acquired` (live) | Parent proposal with the rule; short scroll over its four child accounts. Note typed, Reject clicked. | "Harborview's children are all reconciled, but inactivating a parent is a judgment call. I'll leave it active." |
| 02:06 | 9. Harborview, rejected | Live queue empty. Applied 1, Rejected 1. | "That rejection stays local, and the queue is empty. Meanwhile I've started the daily run again, read-only." |
| 02:14 | 10. Confirmed tab | Confirmed (no change), 35 entries. | "All 35 website locations are confirmed matches." |
| 02:22 | 10. Ashtabula, confirmed | CRM street "PO Box 517" flagged as different. Scroll to the score: street 0/45, phone 20/20, administrator 15/15, total 90, and the "address differs (kept as is)" chip. | "Ashtabula bills to a PO Box, but its phone and administrator both match. It scores 90 and is confirmed, and we record the PO Box without changing it." |
| 02:34 | 11. Runs (live) | Run #3: 0 new, 35 refreshed, 1 previously decided, 0 stale. | "Run 3, after my decisions: no new proposals. Sandusky has converged, and the rejected Harborview item counts as previously decided." |

Notes for the voice-over:

- The video ends at 2:42, so keep the last cue short. In the editor you can hold the last frame
  for a second if you need more time.
- Run 3 shows previously_decided = 1, not 2. Once the pointer is set, the Sandusky account is
  superseded, so the pipeline no longer generates that proposal at all. Only the rejected
  Harborview proposal is generated again and counted as previously decided.
- The Tiffin approval in Part 1 creates account `001SNAP000001` in the snapshot state file. It
  is not a real CRM account.
