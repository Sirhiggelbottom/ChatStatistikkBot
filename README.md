# ChatStatistikkBot

Telegram reaction scoreboard for groups, with SQLite storage, interactive admin settings,
CSV reports, and optional timeout votes. Python 3.11 or newer is required.

## Setup

1. Install Python 3.11+ and open a terminal in this repository.
2. Create an environment and install the timezone database:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. A local `api.ini` has been created with a placeholder token. On a fresh clone,
   copy `api.example.ini` to `api.ini`. Put the token from BotFather in its
   `[telegram] token` field. Never commit this file or paste the token into chat.
4. Add **@ChatStatistikkBot** to your group and promote it to administrator.
   For automatic timeouts use a **supergroup** and grant **Restrict members**.
   Enable the reactions you want in Telegram's group settings.
5. Each admin who wants reports must open the bot privately and press **Start**.
6. Run:

   ```powershell
   .\.venv\Scripts\python.exe -m chatstatistikk.bot
   ```

7. In the group, run `/settings`. The bot sends an interactive private menu.
   Use **+/−** to adjust weights and thresholds and tap reaction buttons to cycle
   **ignored → positive → negative → ignored**. Automatic timeouts start **OFF**.

On Linux/macOS use `python3 -m venv .venv` and `.venv/bin/python`.
Keep the process running on an always-on machine. Only one instance may use the
token/database. Stop any other polling bot or webhook using the same token first.
This repository does not provision a hosting service.

## Scoring rules

| Setting | Default |
| --- | --- |
| Positive / negative | 👍 / 👎 |
| Member / admin / owner weight | 1 / 2 / 3 |
| Strike threshold | Message score ≤ −10 |
| Strikes per timeout vote | 3 |
| Vote requirement | Yes strictly exceeds 50% of Yes + No |
| Timeout | 3 days |
| Voting deadline | Next midnight in Europe/Oslo |
| Maximum message age | 5 days |

Scores belong to the **message author**, separately for each group. Reaction counts
are unweighted; score is the weighted sum. Multiple selected configured reactions
each contribute. Unconfigured, paid, bot, and anonymous reactions are ignored.
Self-reactions follow the same rules as other reactions.

The first two changes by one member on one message in a rolling 20-second window
replace the prior contribution. The third voids that member's entire contribution
on that message. Continued changes keep it void. A new reaction update after a
full 20 seconds of inactivity can count again; waiting alone does not restore it.
Retractions are changes too. This implements the requested two-change exception.
It does not stop Telegram displaying or accepting rapid reactions.

Each qualifying message creates **at most one permanent strike**. Recovery of the
score or a later retraction does not remove that strike. Reaction and weight
settings affect future updates, not historical contributions; role weights are
captured at the update, with a maximum 60-second role cache and membership-update
invalidation. Five-day-old scores are frozen, including subsequent retractions.

Three lifetime strikes queue a vote; the next complete batch queues another.
Rejected/cancelled votes do not reset lifetime strikes and are not repeatedly
reopened. Enabling timeouts considers existing strikes. Changing the batch size
can qualify a member for a new vote. There is at most one active vote per member.
Votes use native anonymous Yes/No polls with forwarding protection and automatic
midnight closure. A tie or no votes causes no timeout. Admins/owners and members
already restricted by someone else are not timed out. Disabling the feature
cancels queued/open votes; existing timeouts remain cancellable via `/timeouts`.

## Commands (owner/admin only)

Run in a group, or privately append the numeric group ID (e.g. `/score -1001234567890`).
Permissions are checked live against that group, including every settings/cancel button.

* `/resultat` — private UTF-8 CSV: Name, Score, Positive reactions, Negative reactions,
  Strikes, sorted highest score first. Spreadsheet formula names are escaped.
* `/score` — two private messages with the top five and bottom five, including
  names, scores, strikes. Requires ten distinct members with reacted-to messages.
* `/timeouts` — private list of active **bot-issued** timeouts and cancellation buttons.
* `/settings` — private interactive controls for reactions, role weights, strike
  score, strikes per vote, required Yes percentage, timeout days, and enable/disable.

## Database and DBeaver

DBeaver is a database client; the actual database is SQLite at
`data/chatstatistikk.sqlite3` (configurable in `api.ini`). In DBeaver create a
**SQLite** connection and select that file. Use read-only access while the bot runs.

* `scoreboard` view: user ID, name, weighted score, positive/negative counts, strikes.
* `messages`: group ID, message ID, author ID, original timestamp, reacted flag.
* `reactions`: current per-member contribution, raw reaction types, weight, burst state.
* `reaction_events`: accepted update audit history, including voided changes.
* `strikes`: permanent per-message strike records.
* `settings`, `ballots`, `timeouts`: configuration and moderation state.
* `inbox`, `metadata`, `handled_callbacks`: durable update handling and deduplication.

Example reacted-message list for a member:

```sql
SELECT m.chat, m.uid, m.mid AS message_id, m.sent,
       COALESCE(SUM((r.positive-r.negative)*r.weight),0) AS score
FROM messages m LEFT JOIN reactions r ON r.chat=m.chat AND r.mid=m.mid
WHERE m.reacted=1 AND m.chat=-1001234567890 AND m.uid=123456789
GROUP BY m.chat,m.mid;
```

Back up with SQLite's backup API, or stop the bot before copying the database.
Do not copy only the main file while WAL writes are active. All database files,
exports, local secrets, and logs are ignored by Git. Reacted message metadata,
scores and audit history are retained indefinitely. Unreacted message metadata is
pruned after six days. Incoming update payloads are cleared after processing;
pending updates may temporarily contain message text.

## Telegram limitations and recovery

* Ordinary group bots receive **no deleted-message event** and cannot fetch the
  reaction state of a deleted message. The last observed score stays in SQLite;
  deletion cannot erase it. No deletion timestamp or final deleted snapshot is claimed.
* Reactions contain the message ID, not the author's identity or original timestamp.
  The bot must first have seen the message (or a reply containing it). Unknown
  messages are ignored. There is no retrospective group-history import.
* Role weighting requires identifiable users. Anonymous/channel reactions cannot
  be assigned a trustworthy member weight and are ignored.
* Telegram retains incoming updates for at most 24 hours. Long downtime can lose
  reactions/messages. A durable local inbox protects updates already fetched.
* Serialized processing, a bounded batch of 100, role caching, API pacing and
  retry-after backoff provide backpressure. This is a single-worker rate limiter,
  not a distributed load balancer. Sustained traffic beyond one worker's capacity
  needs a separate deployment architecture.
* Poll creation cannot be made exactly-once across Telegram and SQLite. An ambiguous
  network failure/crash leaves the ballot `needs_review` instead of risking a
  duplicate vote. Inspect the group and database; resolve that vote manually.
  Such rows are visible in DBeaver and logged, and never automatically punish anyone.
* Missing final poll counts never trigger punishment. A final result that cannot
  be verified within 24 hours is abandoned. Telegram handles timeout expiration
  even while the bot is offline. Restriction retries reuse a fixed expiry.
* Cancellation will not replace a different restriction subsequently set by an admin.
  If it changed, remove it manually in Telegram. The bot does not list restrictions
  created by other admins/bots because Telegram has no complete restriction-list API.
* Reports may be resent if the process crashes immediately after sending them.
  Scoring updates and strike creation are idempotent.

Official reference: [Telegram Bot API](https://core.telegram.org/bots/api), especially
`Update`, `MessageReactionUpdated`, `sendPoll`, and `restrictChatMember`.

## Tests

```powershell
py -m unittest discover -s tests -v
```

Tests use a fake Telegram transport and temporary SQLite databases. They do not
contact or moderate a live group. Before enabling timeouts, test with a separate
supergroup and confirm the bot's permissions, private reports, and chosen reactions.
