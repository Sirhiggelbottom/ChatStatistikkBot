import json
import copy
import sqlite3
from pathlib import Path

DEFAULTS = dict(member=1, administrator=2, creator=3, strike_score=10,
                strike_count=3, yes_percent=50, timeout_days=3, enabled=False,
                positive=['👍'], negative=['👎'], max_age_days=5)


class Store:
    def __init__(self, path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS settings(chat INTEGER PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(chat INTEGER, uid INTEGER, name TEXT NOT NULL,
          PRIMARY KEY(chat,uid));
        CREATE TABLE IF NOT EXISTS messages(chat INTEGER, mid INTEGER, uid INTEGER,
          sent INTEGER NOT NULL, reacted INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(chat,mid));
        CREATE TABLE IF NOT EXISTS reactions(chat INTEGER, mid INTEGER, actor INTEGER,
          positive INTEGER NOT NULL, negative INTEGER NOT NULL, weight INTEGER NOT NULL,
          role TEXT NOT NULL, types TEXT NOT NULL, changes TEXT NOT NULL,
          voided INTEGER NOT NULL, last_update INTEGER NOT NULL,
          PRIMARY KEY(chat,mid,actor));
        CREATE TABLE IF NOT EXISTS reaction_events(update_id INTEGER PRIMARY KEY,
          chat INTEGER, mid INTEGER, actor INTEGER, date INTEGER, types TEXT,
          weight INTEGER, voided INTEGER);
        CREATE TABLE IF NOT EXISTS strikes(chat INTEGER, mid INTEGER, uid INTEGER,
          date INTEGER, PRIMARY KEY(chat,mid));
        CREATE TABLE IF NOT EXISTS ballots(id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat INTEGER, uid INTEGER, batch INTEGER, state TEXT NOT NULL DEFAULT 'pending',
          poll_id TEXT UNIQUE, mid INTEGER, deadline INTEGER, yes INTEGER DEFAULT 0,
          no INTEGER DEFAULT 0, percent INTEGER, days INTEGER, until_date INTEGER,
          UNIQUE(chat,uid,batch));
        CREATE TABLE IF NOT EXISTS timeouts(chat INTEGER, uid INTEGER, until_date INTEGER,
          ballot INTEGER, PRIMARY KEY(chat,uid));
        CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
          done INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value INTEGER);
        CREATE TABLE IF NOT EXISTS handled_callbacks(id TEXT PRIMARY KEY);
        CREATE VIEW IF NOT EXISTS scoreboard AS
        SELECT u.chat,u.uid,u.name,
          COALESCE((SELECT SUM((r.positive-r.negative)*r.weight)
            FROM reactions r JOIN messages m ON m.chat=r.chat AND m.mid=r.mid
            WHERE m.chat=u.chat AND m.uid=u.uid),0) AS score,
          COALESCE((SELECT SUM(r.positive) FROM reactions r JOIN messages m
            ON m.chat=r.chat AND m.mid=r.mid WHERE m.chat=u.chat AND m.uid=u.uid),0) AS positive,
          COALESCE((SELECT SUM(r.negative) FROM reactions r JOIN messages m
            ON m.chat=r.chat AND m.mid=r.mid WHERE m.chat=u.chat AND m.uid=u.uid),0) AS negative,
          (SELECT COUNT(*) FROM strikes s WHERE s.chat=u.chat AND s.uid=u.uid) AS strikes
        FROM users u WHERE EXISTS (SELECT 1 FROM messages m
          WHERE m.chat=u.chat AND m.uid=u.uid AND m.reacted=1);
        ''')

    def settings(self, chat):
        row = self.db.execute('SELECT value FROM settings WHERE chat=?', (chat,)).fetchone()
        return {**copy.deepcopy(DEFAULTS), **(json.loads(row[0]) if row else {})}

    def save_settings(self, chat, values):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                            (chat, json.dumps(values)))

    def message(self, m):
        user = m.get('from')
        if not user or user.get('is_bot') or m.get('sender_chat'):
            return
        chat = m['chat']['id']
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO users VALUES(?,?,?)',
                            (chat, user['id'], display_name(user)))
            self.db.execute('INSERT OR IGNORE INTO messages(chat,mid,uid,sent) VALUES(?,?,?,?)',
                            (chat, m['message_id'], user['id'], m['date']))

    def reaction(self, update_id, r, role):
        chat, mid = r['chat']['id'], r['message_id']
        if not r.get('user') or r['user'].get('is_bot'):
            return
        actor, date = r['user']['id'], r['date']
        s = self.settings(chat)
        with self.db:
            if self.db.execute('SELECT 1 FROM reaction_events WHERE update_id=?', (update_id,)).fetchone():
                return
            m = self.db.execute('SELECT * FROM messages WHERE chat=? AND mid=?', (chat, mid)).fetchone()
            if not m or date < m['sent'] or date - m['sent'] > s['max_age_days'] * 86400:
                return
            old = self.db.execute('SELECT * FROM reactions WHERE chat=? AND mid=? AND actor=?',
                                  (chat, mid, actor)).fetchone()
            if old and update_id <= old['last_update']:
                return
            changes = [t for t in json.loads(old['changes']) if date-t < 20] if old else []
            # A burst stays void until a full quiet window has elapsed.
            voided = bool(old and old['voided'] and changes) or len(changes) >= 2
            changes = (changes + [date])[-3:]
            types = sorted(set(reaction_key(t) for t in r['new_reaction']))
            pos = sum(t in s['positive'] for t in types) if not voided else 0
            neg = sum(t in s['negative'] for t in types) if not voided else 0
            weight = s.get(role, s['member'])
            self.db.execute('INSERT OR REPLACE INTO reactions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (chat, mid, actor, pos, neg, weight, role, json.dumps(types),
                             json.dumps(changes), int(voided), update_id))
            self.db.execute('INSERT INTO reaction_events VALUES(?,?,?,?,?,?,?,?)',
                            (update_id, chat, mid, actor, date, json.dumps(types), weight, int(voided)))
            self.db.execute('UPDATE messages SET reacted=1 WHERE chat=? AND mid=?', (chat, mid))
            score = self.db.execute('SELECT SUM((positive-negative)*weight) FROM reactions WHERE chat=? AND mid=?',
                                    (chat, mid)).fetchone()[0]
            if score <= -s['strike_score']:
                self.db.execute('INSERT OR IGNORE INTO strikes VALUES(?,?,?,?)', (chat, mid, m['uid'], date))

    def scoreboard(self, chat):
        return self.db.execute('SELECT * FROM scoreboard WHERE chat=? ORDER BY score DESC,uid', (chat,)).fetchall()

    def enqueue_ballots(self):
        with self.db:
            for row in self.db.execute('SELECT chat,uid,COUNT(*) n FROM strikes GROUP BY chat,uid').fetchall():
                s = self.settings(row['chat'])
                if not s['enabled']:
                    continue
                # One ballot for each fresh complete batch; don't repeat failed votes.
                batch = row['n'] // s['strike_count']
                if batch:
                    self.db.execute('INSERT OR IGNORE INTO ballots(chat,uid,batch,percent,days) VALUES(?,?,?,?,?)',
                                    (row['chat'], row['uid'], batch, s['yes_percent'], s['timeout_days']))


def display_name(user):
    return ' '.join(filter(None, [user.get('first_name'), user.get('last_name')])) or str(user['id'])


def reaction_key(value):
    return value.get('emoji') or ('custom:' + value['custom_emoji_id'] if 'custom_emoji_id' in value else 'paid')
