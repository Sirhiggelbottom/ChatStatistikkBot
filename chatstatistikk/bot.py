import configparser
import csv
import io
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .store import Store
from .telegram import APIError, Telegram

LOG = logging.getLogger(__name__)
NUMERIC = {
    'member': ('Member weight', 0, 20),
    'administrator': ('Admin weight', 0, 20),
    'creator': ('Owner weight', 0, 20),
    'strike_score': ('Negative score for strike', 1, 100),
    'strike_count': ('Strikes per vote', 1, 20),
    'yes_percent': ('Yes votes must exceed %', 50, 99),
    'timeout_days': ('Timeout days', 1, 30),
}
EMOJIS = ['👍', '👎', '❤', '🔥', '👏', '😁', '🤔', '🤯', '😱', '🤬', '😢', '🤮', '💩', '🎉', '🤡', '💯']
SEND_PERMISSIONS = ['can_send_messages', 'can_send_audios', 'can_send_documents',
                    'can_send_photos', 'can_send_videos', 'can_send_video_notes',
                    'can_send_voice_notes', 'can_send_polls', 'can_send_other_messages',
                    'can_add_web_page_previews']


def next_midnight(now, zone):
    local = datetime.fromtimestamp(now, zone)
    return int(datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), zone).timestamp())


def csv_bytes(rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['Name', 'Score', 'Positive reactions', 'Negative reactions', 'Strikes'])
    for row in rows:
        name = row['name']
        if name.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
            name = "'" + name
        writer.writerow([name, row['score'], row['positive'], row['negative'], row['strikes']])
    return output.getvalue().encode('utf-8-sig')


class Bot:
    def __init__(self, store, api, zone):
        self.store, self.api, self.zone = store, api, zone
        self.roles = {}
        self.command_times = {}
        self.username = ''

    def send(self, chat, text, **kwargs):
        return self.api.call('sendMessage', chat_id=chat, text=text, **kwargs)

    def member(self, chat, uid):
        return self.api.call('getChatMember', chat_id=chat, user_id=uid)

    def admin(self, chat, uid):
        return self.member(chat, uid)['status'] in ('creator', 'administrator')

    def role(self, chat, uid):
        key = (chat, uid)
        cached = self.roles.get(key)
        if not cached or cached[0] < time.time():
            value = self.member(chat, uid)['status']
            self.roles[key] = (time.time() + 60, value)
        return self.roles[key][1]

    def menu(self, chat, target, mid=None):
        s = self.store.settings(chat)
        buttons = [[{'text': f"Automatic timeouts: {'ON' if s['enabled'] else 'OFF'}",
                     'callback_data': f's:{chat}:enabled:0'}]]
        for key, (label, _, _) in NUMERIC.items():
            buttons.append([{'text': '−', 'callback_data': f's:{chat}:{key}:-1'},
                            {'text': f'{label}: {s[key]}', 'callback_data': f's:{chat}:noop:0'},
                            {'text': '+', 'callback_data': f's:{chat}:{key}:1'}])
        for emoji in EMOJIS:
            state = '+' if emoji in s['positive'] else '−' if emoji in s['negative'] else 'ignored'
            buttons.append([{'text': f'{emoji} {state} (tap to cycle)',
                             'callback_data': f'e:{chat}:{EMOJIS.index(emoji)}:0'}])
        text = (f'Settings for chat {chat}\nReaction buttons cycle: ignored → positive → negative.\n'
                'Weight/reaction changes apply to future reaction updates. Existing scores and strikes remain.\n'
                'Vote settings are captured when a ballot is queued. Turning OFF cancels pending votes.\n'
                'Changing strikes per vote applies to lifetime strikes; it may trigger a new vote.')
        if mid:
            self.api.call('editMessageText', chat_id=target, message_id=mid, text=text,
                          reply_markup={'inline_keyboard': buttons})
        else:
            self.send(target, text, reply_markup={'inline_keyboard': buttons})

    def callback(self, q):
        parts = q.get('data', '').split(':')
        if len(parts) != 4 or parts[0] not in ('s', 'e', 't'):
            return
        kind, raw_chat, key, raw_delta = parts
        try:
            chat, delta = int(raw_chat), int(raw_delta)
        except ValueError:
            return
        if not self.admin(chat, q['from']['id']):
            self.api.call('answerCallbackQuery', callback_query_id=q['id'], text='Admins only.', show_alert=True)
            return
        self.api.call('answerCallbackQuery', callback_query_id=q['id'])
        if kind == 't':
            if not key.isdigit():
                return
            try:
                self.cancel_timeout(chat, int(key))
            except APIError as e:
                if e.code != 409:
                    raise
                self.send(q['from']['id'], 'Another admin changed this restriction. Review and cancel it in Telegram.')
                return
            self.send(q['from']['id'], 'Timeout cancelled (or already expired).')
            return
        if self.store.db.execute('SELECT 1 FROM handled_callbacks WHERE id=?', (q['id'],)).fetchone():
            return
        s = self.store.settings(chat)
        if kind == 's':
            if key == 'noop':
                return
            if key == 'enabled':
                if not s['enabled']:
                    info = self.api.call('getChat', chat_id=chat)
                    own = self.member(chat, self.bot_id)
                    if info['type'] != 'supergroup' or not own.get('can_restrict_members'):
                        self.send(q['from']['id'], 'Enable requires a supergroup and bot admin permission to restrict members.')
                        return
                s['enabled'] = not s['enabled']
            elif key in NUMERIC and delta in (-1, 1):
                _, low, high = NUMERIC[key]
                s[key] = max(low, min(high, s[key] + delta))
            else:
                return
        else:
            if not key.isdigit() or not 0 <= int(key) < len(EMOJIS):
                return
            emoji = EMOJIS[int(key)]
            if emoji in s['positive']:
                s['positive'].remove(emoji)
                s['negative'].append(emoji)
            elif emoji in s['negative']:
                s['negative'].remove(emoji)
            else:
                s['positive'].append(emoji)
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (chat, json.dumps(s)))
            self.store.db.execute('INSERT INTO handled_callbacks VALUES(?)', (q['id'],))
        self.menu(chat, q['message']['chat']['id'], q['message']['message_id'])

    def command(self, m):
        text = m.get('text', '')
        if not text.startswith('/') or not m.get('from') or m.get('sender_chat'):
            return
        words = text.split()
        command, _, mention = words[0].partition('@')
        if mention and mention.lower() != self.username.lower():
            return
        uid, origin = m['from']['id'], m['chat']['id']
        if command in ('/start', '/help'):
            self.send(origin, 'ChatStatistikk records reactions on messages it has seen.\n'
                      'Admins: /settings, /resultat, /score, /timeouts in your group. '
                      'Start this bot privately first to receive reports. In private, append your numeric group ID.')
            return
        if command not in ('/settings', '/resultat', '/score', '/timeouts'):
            return
        chat = origin
        if m['chat']['type'] == 'private':
            if len(words) != 2 or not words[1].lstrip('-').isdigit():
                self.send(uid, 'Run this command in the group, or append its numeric group ID.')
                return
            chat = int(words[1])
        # Bound report/API abuse; authorization is always checked live.
        if self.command_times.get((chat, uid), 0) > time.monotonic():
            return
        self.command_times[(chat, uid)] = time.monotonic() + 2
        if not self.admin(chat, uid):
            self.send(origin, 'Only the group owner and admins can use this command.')
            return
        try:
            if command == '/settings':
                self.menu(chat, uid)
            elif command == '/resultat':
                self.api.document(uid, csv_bytes(self.store.scoreboard(chat)))
            elif command == '/score':
                rows = self.store.scoreboard(chat)
                if len(rows) < 10:
                    self.send(uid, 'Not enough data: need at least 10 members with reacted-to messages.')
                else:
                    for title, group in [('Top 5', rows[:5]), ('Bottom 5', list(reversed(rows[-5:])) )]:
                        self.send(uid, title + '\n' + '\n'.join(
                            f"{r['name']}: {r['score']} points, {r['strikes']} strikes" for r in group))
            else:
                rows = self.store.db.execute('SELECT t.*,u.name FROM timeouts t LEFT JOIN users u '
                    'ON u.chat=t.chat AND u.uid=t.uid WHERE t.chat=? AND until_date>?', (chat, int(time.time()))).fetchall()
                if not rows:
                    self.send(uid, 'No active bot-issued timeouts.')
                for row in rows:
                    until = datetime.fromtimestamp(row['until_date'], self.zone).isoformat()
                    self.send(uid, f"{row['name'] or row['uid']} — until {until}", reply_markup={'inline_keyboard': [[
                        {'text': 'Cancel timeout', 'callback_data': f"t:{chat}:{row['uid']}:0"}]]})
        except APIError as e:
            if e.code == 403:
                self.send(origin, 'I could not send you a private message. Open @ChatStatistikkBot, press Start, then retry.')
            else:
                raise

    def cancel_timeout(self, chat, uid):
        row = self.store.db.execute('SELECT * FROM timeouts WHERE chat=? AND uid=?', (chat, uid)).fetchone()
        if not row:
            return
        current = self.member(chat, uid)
        if current['status'] == 'restricted' and current.get('until_date') == row['until_date']:
            permissions = self.api.call('getChat', chat_id=chat).get('permissions', {})
            self.api.call('restrictChatMember', chat_id=chat, user_id=uid, permissions=permissions,
                          use_independent_chat_permissions=True)
        elif current['status'] == 'restricted':
            raise APIError(409, 'Restriction changed by another admin; cancel it manually in Telegram.')
        with self.store.db:
            self.store.db.execute('DELETE FROM timeouts WHERE chat=? AND uid=?', (chat, uid))

    def handle(self, update):
        if 'message' in update:
            m = update['message']
            if m['chat']['type'] in ('group', 'supergroup'):
                self.store.message(m)
                if m.get('reply_to_message'):
                    self.store.message({**m['reply_to_message'], 'chat': m['chat']})
            self.command(m)
        elif 'message_reaction' in update:
            r = update['message_reaction']
            message = self.store.db.execute('SELECT sent FROM messages WHERE chat=? AND mid=?',
                                            (r['chat']['id'], r['message_id'])).fetchone()
            if r.get('user') and message and 0 <= r['date']-message['sent'] <= 5*86400:
                self.store.reaction(update['update_id'], r, self.role(r['chat']['id'], r['user']['id']))
        elif 'chat_member' in update:
            m = update['chat_member']
            self.roles.pop((m['chat']['id'], m['new_chat_member']['user']['id']), None)
        elif 'callback_query' in update:
            self.callback(update['callback_query'])
        elif 'poll' in update:
            poll = update['poll']
            with self.store.db:
                self.store.db.execute('UPDATE ballots SET yes=?,no=? WHERE poll_id=?',
                    (poll['options'][0]['voter_count'], poll['options'][1]['voter_count'], poll['id']))

    def set_ballot(self, bid, **fields):
        with self.store.db:
            self.store.db.execute('UPDATE ballots SET ' + ','.join(k+'=?' for k in fields) + ' WHERE id=?',
                                  (*fields.values(), bid))

    def tick(self, now=None):
        now = int(time.time()) if now is None else now
        self.store.enqueue_ballots()
        rows = self.store.db.execute("SELECT * FROM ballots WHERE state IN ('pending','open','decided','muting')").fetchall()
        for row in rows:
            try:
                self.advance_ballot(row, now)
            except APIError as e:
                LOG.warning('Ballot %s deferred: API status %s', row['id'], e.code)
                if e.code in (400, 403):
                    self.set_ballot(row['id'], state='failed')
                    self.send(row['chat'], f"Timeout vote #{row['id']} could not complete. Check bot permissions and logs.")
        with self.store.db:
            self.store.db.execute('DELETE FROM timeouts WHERE until_date<=?', (now,))
            # Keep reacted-to messages indefinitely; remove unseen/unreacted metadata after the age limit.
            self.store.db.execute('DELETE FROM messages WHERE reacted=0 AND sent<?', (now - 6*86400,))
            self.store.db.execute('DELETE FROM inbox WHERE done=1 AND id < (SELECT COALESCE(MAX(id),0)-10000 FROM inbox)')

    def advance_ballot(self, row, now):
        bid, chat, uid = row['id'], row['chat'], row['uid']
        if not self.store.settings(chat)['enabled']:
            if row['state'] == 'open':
                try:
                    self.api.call('stopPoll', chat_id=chat, message_id=row['mid'])
                except APIError as e:
                    if e.code != 400:
                        raise
            self.set_ballot(bid, state='cancelled')
            return
        if row['state'] == 'pending':
            if self.member(chat, uid)['status'] != 'member':
                self.set_ballot(bid, state='ineligible')
                return
            other = self.store.db.execute("SELECT 1 FROM ballots WHERE chat=? AND uid=? AND id<>? "
                "AND state IN ('open','sending','decided','muting')", (chat, uid, bid)).fetchone()
            active = self.store.db.execute('SELECT 1 FROM timeouts WHERE chat=? AND uid=? AND until_date>?',
                                          (chat, uid, now)).fetchone()
            if other or active:
                return
            name = self.store.db.execute('SELECT name FROM users WHERE chat=? AND uid=?', (chat, uid)).fetchone()[0]
            deadline = next_midnight(now, self.zone)
            if deadline - now < 10:
                return  # Telegram requires close_date at least five seconds ahead.
            # Persist before a non-idempotent send. A crash is flagged for review, never resent blindly.
            self.set_ballot(bid, state='sending', deadline=deadline)
            try:
                result = self.api.call('sendPoll', chat_id=chat,
                    question=f"Timeout {name[:80]} for {row['days']} days? Vote #{bid}",
                    options=[{'text': 'Yes'}, {'text': 'No'}], is_anonymous=True,
                    allows_multiple_answers=False, protect_content=True, close_date=deadline)
            except APIError as e:
                self.set_ballot(bid, state='pending' if e.code == 429 else 'needs_review')
                if e.code != 429:
                    LOG.error('Ballot %s needs manual review: poll delivery was not confirmed', bid)
                raise
            self.set_ballot(bid, state='open', poll_id=result['poll']['id'], mid=result['message_id'])
        elif row['state'] == 'open' and now >= row['deadline']:
            if now - row['deadline'] > 86400:
                self.set_ballot(bid, state='failed')
                self.send(chat, f'Vote #{bid} expired without a verified final result; no timeout applied.')
                return
            # stopPoll provides authoritative final counts, including votes missed during downtime.
            try:
                poll = self.api.call('stopPoll', chat_id=chat, message_id=row['mid'])
            except APIError as e:
                # Telegram may reject an already auto-closed poll. Only a saved closed poll is safe.
                if e.code != 400 or not self.store.db.execute(
                    "SELECT 1 FROM metadata WHERE key=? AND value=1", ('closed:' + row['poll_id'],)).fetchone():
                    raise APIError(409 if e.code == 400 else e.code, 'Final poll result not available') from None
                poll = {'options': [{'voter_count': row['yes']}, {'voter_count': row['no']}]}
            self.set_ballot(bid, state='decided', yes=poll['options'][0]['voter_count'], no=poll['options'][1]['voter_count'])
        elif row['state'] == 'decided':
            total = row['yes'] + row['no']
            if not total or row['yes'] * 100 <= row['percent'] * total:
                self.set_ballot(bid, state='rejected')
                return
            # Avoid stale punishment after long outages.
            if now - row['deadline'] > 86400 or self.member(chat, uid)['status'] != 'member':
                self.set_ballot(bid, state='ineligible')
                return
            self.set_ballot(bid, state='muting', until_date=now + row['days']*86400)
        elif row['state'] == 'muting':
            if now >= row['until_date']:
                self.set_ballot(bid, state='expired')
                return
            current = self.member(chat, uid)
            ours = current['status'] == 'restricted' and current.get('until_date') == row['until_date']
            if current['status'] != 'member' and not ours:
                self.set_ballot(bid, state='ineligible')
                return
            if not ours:
                self.api.call('restrictChatMember', chat_id=chat, user_id=uid,
                    permissions={key: False for key in SEND_PERMISSIONS},
                    use_independent_chat_permissions=True, until_date=row['until_date'])
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO timeouts VALUES(?,?,?,?)',
                                      (chat, uid, row['until_date'], bid))
                self.store.db.execute("UPDATE ballots SET state='applied' WHERE id=?", (bid,))

    def run(self):
        me = self.api.call('getMe')
        self.username, self.bot_id = me['username'], me['id']
        self.api.call('setMyCommands', commands=[{'command': c, 'description': d} for c, d in
            [('settings', 'Interactive admin settings'), ('resultat', 'CSV scoreboard in private'),
             ('score', 'Top and bottom five in private'), ('timeouts', 'List and cancel timeouts')]])
        with self.store.db:
            self.store.db.execute("UPDATE ballots SET state='needs_review' WHERE state='sending'")
        LOG.info('Started @%s', self.username)
        while True:
            try:
                pending = self.store.db.execute('SELECT * FROM inbox WHERE done=0 ORDER BY id LIMIT 100').fetchall()
                for item in pending:
                    try:
                        update = json.loads(item['payload'])
                        self.handle(update)
                        if update.get('poll', {}).get('is_closed'):
                            p = update['poll']
                            with self.store.db:
                                self.store.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,1)', ('closed:'+p['id'],))
                    except APIError as e:
                        if e.code in (0, 429) or e.code >= 500:
                            raise
                        LOG.warning('Update %s rejected: API status %s', item['id'], e.code)
                    with self.store.db:
                        self.store.db.execute("UPDATE inbox SET done=1,payload='{}' WHERE id=?", (item['id'],))
                self.tick()
                offset = self.store.db.execute("SELECT value FROM metadata WHERE key='offset'").fetchone()
                updates = self.api.call('getUpdates', offset=offset[0] if offset else 0, timeout=10, limit=100,
                    allowed_updates=['message', 'message_reaction', 'callback_query', 'poll', 'chat_member', 'my_chat_member'])
                with self.store.db:
                    for update in updates:
                        self.store.db.execute('INSERT OR IGNORE INTO inbox(id,payload) VALUES(?,?)',
                                              (update['update_id'], json.dumps(update)))
                    if updates:
                        self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES('offset',?)",
                                              (updates[-1]['update_id'] + 1,))
            except APIError as e:
                LOG.warning('Telegram operation deferred: status %s', e.code)
                time.sleep(min(max(e.retry_after, 3), 30))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = configparser.ConfigParser(interpolation=None)
    config.read('api.ini', encoding='utf-8-sig')
    token = config.get('telegram', 'token', fallback='').strip()
    if not token or token == 'PUT_BOTFATHER_TOKEN_HERE':
        raise SystemExit('Set your BotFather token in api.ini before starting.')
    database = config.get('storage', 'database', fallback='data/chatstatistikk.sqlite3')
    zone = ZoneInfo(config.get('bot', 'timezone', fallback='Europe/Oslo'))
    # One process per database/token. A local lock avoids competing getUpdates workers.
    lock_path = Path(database).with_suffix('.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as lock:
        try:
            import os
            if os.name == 'nt':
                import msvcrt
                lock.seek(0)
                lock.write(b'0')
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit('Another bot process is already using this database.') from None
        Bot(Store(database), Telegram(token), zone).run()


if __name__ == '__main__':
    main()
