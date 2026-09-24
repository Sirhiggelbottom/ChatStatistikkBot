import csv
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from chatstatistikk.store import Store
from chatstatistikk.bot import Bot, csv_bytes, next_midnight
from chatstatistikk.telegram import APIError


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.status = 'member'
        self.yes, self.no = 4, 2

    def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == 'getChatMember':
            return {'status': self.status}
        if method == 'sendPoll':
            return {'message_id': 123, 'poll': {'id': 'poll1'}}
        if method == 'stopPoll':
            return {'options': [{'voter_count': self.yes}, {'voter_count': self.no}]}
        if method == 'getChat':
            return {'permissions': {'can_send_messages': True}}
        return True


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.add_message()
        self.n = 0

    def tearDown(self):
        self.store.db.close()

    def add_message(self, mid=1, chat=-100, uid=7, sent=100):
        self.store.message({'chat': {'id': chat}, 'message_id': mid, 'date': sent,
                            'from': {'id': uid, 'first_name': 'Person'}})

    def react(self, actor=1, types=('👍',), date=101, role='member', mid=1, chat=-100):
        self.n += 1
        event = {'chat': {'id': chat}, 'message_id': mid, 'date': date,
                 'user': {'id': actor}, 'new_reaction': [{'emoji': e} for e in types]}
        self.store.reaction(self.n, event, role)
        return event

    def score(self):
        return self.store.scoreboard(-100)[0]['score']

    def test_requested_weight_example(self):
        self.react()
        self.react(actor=2, types=('👎',), role='administrator')
        self.assertEqual(self.score(), -1)
        self.react(actor=3, role='creator')
        self.assertEqual(self.score(), 2)

    def test_change_and_retract_replace_not_accumulate(self):
        self.react()
        self.react(types=('👎',), date=102)
        self.assertEqual(self.score(), -1)
        self.react(types=(), date=123)
        self.assertEqual(self.score(), 0)

    def test_third_change_voids_and_requires_quiet_window(self):
        self.react()
        self.react(types=('👎',), date=102)
        self.react(date=103)
        self.assertEqual(self.score(), 0)
        self.react(date=121)
        self.assertEqual(self.score(), 0)
        self.react(date=141)
        self.assertEqual(self.score(), 1)

    def test_duplicate_update_does_not_count_as_change(self):
        event = self.react()
        self.store.reaction(self.n, event, 'member')
        self.react(types=('👎',), date=102)
        self.assertEqual(self.score(), -1)

    def test_strike_once_even_if_score_recovers(self):
        settings = self.store.settings(-100)
        settings['strike_score'] = 2
        self.store.save_settings(-100, settings)
        self.react(types=('👎',), role='administrator')
        self.react(types=(), date=125)
        self.react(types=('👎',), date=150, role='administrator')
        self.assertEqual(self.store.scoreboard(-100)[0]['strikes'], 1)

    def test_old_unknown_and_anonymous_ignored(self):
        self.react(date=100 + 5*86400+1)
        self.react(mid=99)
        self.store.reaction(99, {'chat': {'id': -100}, 'message_id': 1}, 'member')
        self.assertEqual(self.store.scoreboard(-100), [])

    def test_five_day_boundary(self):
        self.react(date=100 + 5*86400)
        self.assertEqual(self.score(), 1)

    def test_chat_isolation(self):
        self.add_message(chat=-200)
        self.react(chat=-200)
        self.assertEqual(self.store.scoreboard(-100), [])
        self.assertEqual(self.store.scoreboard(-200)[0]['score'], 1)

    def test_default_reaction_lists_are_isolated(self):
        settings = self.store.settings(-100)
        settings['positive'].append('🔥')
        self.store.save_settings(-100, settings)
        self.assertNotIn('🔥', self.store.settings(-200)['positive'])

    def test_multi_reactions_and_unconfigured_emoji(self):
        s = self.store.settings(-100)
        s['positive'].append('🔥')
        self.store.save_settings(-100, s)
        self.react(types=('👍', '🔥', '👎', '🤡'), role='administrator')
        self.assertEqual(self.score(), 2)
        row = self.store.scoreboard(-100)[0]
        self.assertEqual((row['positive'], row['negative']), (2, 1))

    def test_counts_are_unweighted(self):
        self.react(role='creator')
        row = self.store.scoreboard(-100)[0]
        self.assertEqual((row['positive'], row['negative'], row['score']), (1, 0, 3))

    def test_reacted_message_survives_cleanup_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'test.db')
            store.message({'chat': {'id': -100}, 'message_id': 1, 'date': 100,
                           'from': {'id': 7, 'first_name': 'Name'}})
            store.reaction(1, {'chat': {'id': -100}, 'message_id': 1, 'date': 101,
                'user': {'id': 2}, 'new_reaction': [{'emoji': '👎'}]}, 'creator')
            Bot(store, FakeAPI(), timezone.utc).tick(now=10000000)
            store.db.close()
            store = Store(Path(tmp) / 'test.db')
            self.assertEqual(store.scoreboard(-100)[0]['score'], -3)
            store.db.close()


class VotingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.api = FakeAPI()
        self.bot = Bot(self.store, self.api, timezone.utc)
        with self.store.db:
            self.store.db.execute("INSERT INTO users VALUES(-100,7,'Name')")
            for mid in range(3):
                self.store.db.execute('INSERT INTO strikes VALUES(-100,?,7,100)', (mid,))

    def tearDown(self):
        self.store.db.close()

    def enable(self):
        s = self.store.settings(-100)
        s['enabled'] = True
        self.store.save_settings(-100, s)

    def state(self):
        return self.store.db.execute('SELECT state FROM ballots').fetchone()[0]

    def test_disabled_by_default(self):
        self.bot.tick(now=100)
        self.assertFalse(self.api.calls)

    def test_majority_applies_three_day_timeout_once(self):
        self.enable()
        self.bot.tick(now=100)
        self.bot.tick(now=86400)
        self.bot.tick(now=86401)
        self.bot.tick(now=86402)
        self.bot.tick(now=86403)
        self.assertEqual(self.state(), 'applied')
        calls = [p for m, p in self.api.calls if m == 'restrictChatMember']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['until_date'], 86401+3*86400)
        self.assertFalse(calls[0]['permissions']['can_send_messages'])

    def test_tie_rejected(self):
        self.enable()
        self.api.yes = self.api.no = 2
        for now in [100, 86400, 86401]:
            self.bot.tick(now)
        self.assertEqual(self.state(), 'rejected')

    def test_disable_cancels_open_ballot(self):
        self.enable()
        self.bot.tick(100)
        s = self.store.settings(-100)
        s['enabled'] = False
        self.store.save_settings(-100, s)
        self.bot.tick(101)
        self.assertEqual(self.state(), 'cancelled')
        self.assertFalse(any(m == 'restrictChatMember' for m, _ in self.api.calls))

    def test_admin_never_muted(self):
        self.enable()
        self.bot.tick(100)
        self.bot.tick(86400)
        self.api.status = 'administrator'
        self.bot.tick(86401)
        self.assertEqual(self.state(), 'ineligible')

    def test_missing_final_poll_fails_closed(self):
        self.enable()
        self.bot.tick(100)
        original = self.api.call
        def missing(method, **kwargs):
            if method == 'stopPoll':
                raise APIError(400, 'Already closed')
            return original(method, **kwargs)
        self.api.call = missing
        self.bot.tick(86400)
        self.assertEqual(self.state(), 'open')

    def test_nonadmin_cannot_change_settings(self):
        self.bot.callback({'id': 'x', 'from': {'id': 9}, 'data': 's:-100:enabled:0'})
        self.assertFalse(self.store.settings(-100)['enabled'])

    def test_callback_replay_does_not_increment_twice(self):
        self.api.status = 'administrator'
        q = {'id': 'unique', 'from': {'id': 9}, 'data': 's:-100:member:1',
             'message': {'chat': {'id': 9}, 'message_id': 12}}
        self.bot.callback(q)
        self.bot.callback(q)
        self.assertEqual(self.store.settings(-100)['member'], 2)

    def test_stricter_vote_threshold(self):
        self.enable()
        s = self.store.settings(-100)
        s['yes_percent'] = 75
        self.store.save_settings(-100, s)
        for now in [100, 86400, 86401]:
            self.bot.tick(now)
        self.assertEqual(self.state(), 'rejected')

    def test_cannot_cancel_another_admins_restriction(self):
        with self.store.db:
            self.store.db.execute('INSERT INTO timeouts VALUES(-100,7,123456,1)')
        original = self.api.call
        self.api.call = lambda method, **kw: ({'status': 'restricted', 'until_date': 999999}
            if method == 'getChatMember' else original(method, **kw))
        with self.assertRaises(APIError):
            self.bot.cancel_timeout(-100, 7)
        self.assertFalse(any(m == 'restrictChatMember' for m, _ in self.api.calls))

    def test_cancel_own_timeout(self):
        with self.store.db:
            self.store.db.execute('INSERT INTO timeouts VALUES(-100,7,123456,1)')
        original = self.api.call
        self.api.call = lambda method, **kw: ({'status': 'restricted', 'until_date': 123456}
            if method == 'getChatMember' else original(method, **kw))
        self.bot.cancel_timeout(-100, 7)
        self.assertIsNone(self.store.db.execute('SELECT * FROM timeouts').fetchone())
        self.assertTrue(any(m == 'restrictChatMember' for m, _ in self.api.calls))

    def test_csv_escapes_formula_names(self):
        result = csv_bytes([dict(name='=HYPERLINK("bad")', score=3, positive=1, negative=0, strikes=0)])
        rows = list(csv.reader(io.StringIO(result.decode('utf-8-sig'))))
        self.assertTrue(rows[1][0].startswith("'="))

    def test_oslo_dst_midnight(self):
        zone = ZoneInfo('Europe/Oslo')
        start = datetime(2026, 3, 29, tzinfo=zone).timestamp()
        self.assertEqual(next_midnight(start, zone) - start, 23*3600)


if __name__ == '__main__':
    unittest.main()
