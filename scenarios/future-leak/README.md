# future-leak

Ported from the original future-leak probe, now with **seeded answer-option shuffling**. The first
run showed two small models picking option B most of the time, and B happened to be right for 12 of
20 events, which inflated their scores. Options are now shuffled per event (`--seed`, default 0) and
the scores include `always_fav_letter_acc`, the accuracy of always choosing the model's favourite
letter, as a position-bias check.

Each event is asked twice:

| Mode | Context |
|---|---|
| `gated` | Only Epochal results with `Accept-Datetime` = event date minus 7 days |
| `ungated` | None: the model answers from its own weights |

`leak_score_upper_bound` = gated accuracy minus chance. A week-ahead web legitimately predicts some
events (polls, priced-in Fed moves), so this is an upper bound. The cleanest signal is gated accuracy
on events before the model's training cutoff compared with events after it.
