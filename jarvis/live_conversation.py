"""Durable, per-identity serialized text chat with explicit release provenance."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

from .conversation_workspace import identifier, text
from .subscription_chat import ChatTransportError, release_text


class LiveConversations:
    def __init__(self, workspace, providers, capacity):
        self.workspace, self.providers = workspace, providers
        self.pool = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix='subscription-chat')
        self.capacity, self.running = capacity, {}
        with workspace.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cc_live_bindings(
                    agent_id TEXT PRIMARY KEY, provider TEXT, model TEXT, epoch TEXT);
                CREATE TABLE IF NOT EXISTS cc_live_turns(
                    turn_id TEXT PRIMARY KEY, scope TEXT, agent_id TEXT, provider TEXT,
                    model TEXT, epoch TEXT, message_id TEXT, response_id TEXT,
                    state TEXT, kind TEXT, parent TEXT, created_at REAL,
                    usage TEXT, release_digest TEXT);
            ''')
            # Additive, nullable timing columns; older preview databases gain them in place.
            columns = {r['name'] for r in db.execute('PRAGMA table_info(cc_live_turns)')}
            for column in ('started_at', 'finished_at'):
                if column not in columns:
                    db.execute(f'ALTER TABLE cc_live_turns ADD COLUMN {column} REAL')
            db.execute("UPDATE cc_live_turns SET state='INTERRUPTED' WHERE state IN ('QUEUED','RUNNING')")
            db.execute("UPDATE cc_messages SET state='INTERRUPTED' WHERE state IN ('LIVE_QUEUED','STREAMING','LIVE_DELIVERED','CONNECTING')")

    def binding(self, db, agent, provider, model):
        old = db.execute('SELECT * FROM cc_live_bindings WHERE agent_id=?', (agent,)).fetchone()
        if old and (old['provider'], old['model']) == (provider, model):
            return old['epoch']
        epoch = identifier('epoch')
        db.execute('INSERT OR REPLACE INTO cc_live_bindings VALUES (?,?,?,?)', (agent, provider, model, epoch))
        return epoch

    def change_binding(self, agent, provider, model):
        with self.workspace.db() as db:
            self.binding(db, agent, provider, model)

    def send(self, scope, agent, payload):
        if set(payload) - {'body', 'kind', 'request_id', 'run_id', 'attachments'}:
            raise ValueError('Unsupported message fields.')
        if not isinstance(payload.get('attachments', []), list):
            raise ValueError('Attachments must be a list.')
        if payload.get('attachments'):
            raise PermissionError('Live chat cannot upload attachments. Remove locally staged attachments before sending.')
        body, request = release_text(text(payload.get('body'))), text(payload.get('request_id'), 200)
        kind = payload.get('kind', 'discuss')
        if not isinstance(kind, str) or kind not in {'discuss', 'work', 'steer', 'reply'}:
            raise ValueError('Unsupported live conversation type.')
        aid, provider, model = agent.agent_id, agent.model_provider, agent.model_name
        digest = hashlib.sha256(json.dumps([scope, aid, body, kind, payload.get('run_id')]).encode()).hexdigest()
        with self.workspace.db() as db:
            old = db.execute('SELECT * FROM cc_messages WHERE request_id=?', (request,)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ValueError('Request ID reused for different content.')
                return dict(old)
            if agent.lifecycle.value != 'RUNNING':
                raise ValueError('Enable or resume this agent before sending to its provider.')
            epoch = self.binding(db, aid, provider, model)
            parent = payload.get('run_id')
            if kind in {'steer', 'reply'}:
                target = db.execute('SELECT * FROM cc_live_turns WHERE turn_id=? AND scope=? AND agent_id=? AND epoch=?',
                                    (parent, scope, aid, epoch)).fetchone()
                if not target or target['state'] not in {'RUNNING', 'QUEUED', 'PAUSED', 'INTERRUPTED'}:
                    raise ValueError('Targeted live turn is no longer active under this model binding.')
            tid = identifier('live')
            mid = self.workspace._append(db, scope, aid, 'operator', kind, body, 'LIVE_QUEUED', tid, request, digest)
            rid = self.workspace._append(db, scope, aid, 'assistant', 'response',
                                         'Queued for subscription response.', 'LIVE_QUEUED', tid)
            db.execute('''INSERT INTO cc_live_turns(turn_id, scope, agent_id, provider, model, epoch,
                              message_id, response_id, state, kind, parent, created_at, usage, release_digest)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (tid, scope, aid, provider, model, epoch, mid, rid, 'QUEUED', kind, parent, time.time(), '{}', None))
            return dict(db.execute('SELECT * FROM cc_messages WHERE message_id=?', (mid,)).fetchone())

    def tick(self, agents):
        enabled = {a['agent_id'] for a in agents if a['lifecycle'] == 'RUNNING'}
        for aid, (future, event) in list(self.running.items()):
            if future.done():
                self.running.pop(aid)
        with self.workspace.db() as db:
            queued = db.execute("SELECT * FROM cc_live_turns WHERE state='QUEUED' ORDER BY created_at").fetchall()
            for row in queued:
                aid = row['agent_id']
                if aid not in enabled or aid in self.running or len(self.running) >= self.capacity:
                    continue
                db.execute("UPDATE cc_live_turns SET state='RUNNING',started_at=?,finished_at=NULL WHERE turn_id=?",
                           (time.time(), row['turn_id']))
                event = threading.Event()
                future = self.pool.submit(self.execute, dict(row), event)
                self.running[aid] = (future, event)

    def execute(self, row, cancel):
        w, tid = self.workspace, row['turn_id']
        try:
            with w.db() as db:
                current = db.execute('SELECT state FROM cc_live_turns WHERE turn_id=?', (tid,)).fetchone()
                if current['state'] != 'RUNNING' or cancel.is_set():
                    return
                if row['parent']:
                    target = db.execute('SELECT state FROM cc_live_turns WHERE turn_id=?', (row['parent'],)).fetchone()
                    if not target or target['state'] != 'COMPLETED':
                        raise ChatTransportError('The targeted turn did not complete. Send a new message with the context you want to use.')
                history = db.execute('''SELECT t.*,m.body AS user_body,r.body AS assistant_body
                    FROM cc_live_turns t JOIN cc_messages m ON m.message_id=t.message_id
                    JOIN cc_messages r ON r.message_id=t.response_id
                    WHERE t.scope=? AND t.agent_id=? AND t.epoch=? AND t.state='COMPLETED'
                    AND t.created_at<? ORDER BY t.created_at''',
                    (row['scope'], row['agent_id'], row['epoch'], row['created_at'])).fetchall()
                messages = []
                for turn in history:
                    messages.extend([{'role': 'user', 'content': turn['user_body']},
                                     {'role': 'assistant', 'content': turn['assistant_body']}])
                body = db.execute('SELECT body FROM cc_messages WHERE message_id=?', (row['message_id'],)).fetchone()['body']
                messages.append({'role': 'user', 'content': body})
                serialized = release_text(json.dumps(messages, ensure_ascii=False))
                # Decision is content- and actor-bound; no memory/tool/attachment input path exists.
                release = hashlib.sha256(json.dumps([row['scope'], row['agent_id'], row['provider'],
                           row['model'], row['epoch'], 'text-release-v1', serialized]).encode()).hexdigest()
                db.execute('UPDATE cc_live_turns SET release_digest=? WHERE turn_id=?', (release, tid))
                db.execute("UPDATE cc_messages SET state='CONNECTING' WHERE message_id=?", (row['message_id'],))
                db.execute("UPDATE cc_messages SET body='Connecting to selected subscription…',state='STREAMING' WHERE message_id=?", (row['response_id'],))
            def progress(body):
                with w.db() as db:
                    active = db.execute('SELECT state FROM cc_live_turns WHERE turn_id=?', (tid,)).fetchone()['state']
                    if active == 'RUNNING' and not cancel.is_set():
                        db.execute('UPDATE cc_messages SET body=? WHERE message_id=?', (body, row['response_id']))
                        db.execute("UPDATE cc_messages SET state='LIVE_DELIVERED',delivered_at=COALESCE(delivered_at,?) WHERE message_id=?", (time.time(), row['message_id']))
            adapter = self.providers[row['provider']]
            result, usage = adapter.chat(row['model'], messages, cancel, progress)
            with w.db() as db:
                state = db.execute('SELECT state FROM cc_live_turns WHERE turn_id=?', (tid,)).fetchone()['state']
                if state != 'RUNNING' or cancel.is_set():
                    return
                db.execute("UPDATE cc_live_turns SET state='COMPLETED',usage=?,finished_at=? WHERE turn_id=?",
                           (json.dumps(usage), time.time(), tid))
                db.execute("UPDATE cc_messages SET body=?,state='COMPLETED' WHERE message_id=?", (result, row['response_id']))
                db.execute("UPDATE cc_messages SET state='APPLIED',applied_at=?,delivered_at=COALESCE(delivered_at,?) WHERE message_id=?", (time.time(), time.time(), row['message_id']))
        except Exception as exc:
            detail = str(exc) if isinstance(exc, (ChatTransportError, PermissionError)) else 'Subscription connection failed. Check CLI availability and send a new message to reconnect.'
            with w.db() as db:
                current = db.execute('SELECT state FROM cc_live_turns WHERE turn_id=?', (tid,)).fetchone()['state']
                if current != 'RUNNING' or cancel.is_set():
                    return
                adapter = self.providers.get(row['provider'])
                if adapter and getattr(adapter, 'status', None) != 'BLOCKED':
                    adapter.status, adapter.reason = 'ERROR', detail
                db.execute("UPDATE cc_live_turns SET state='FAILED',finished_at=? WHERE turn_id=?", (time.time(), tid))
                db.execute("UPDATE cc_messages SET state='FAILED' WHERE message_id=?", (row['message_id'],))
                db.execute("UPDATE cc_messages SET body=?,state='FAILED' WHERE message_id=?", (detail, row['response_id']))

    def control(self, agent, action):
        with self.workspace.db() as db:
            rows = db.execute("SELECT * FROM cc_live_turns WHERE agent_id=? AND state IN ('QUEUED','RUNNING','PAUSED','INTERRUPTED')", (agent,)).fetchall()
            for row in rows:
                if action in {'start', 'resume'}:
                    if row['state'] not in {'PAUSED', 'INTERRUPTED'}:
                        continue
                    state = 'QUEUED'
                    message = 'Resuming: this text turn restarts from its saved authorized context.'
                else:
                    state = 'PAUSED' if action == 'pause' else 'CANCELLED'
                    message = 'Paused. Resume restarts this text turn.' if action == 'pause' else 'Cancelled locally; already-sent text cannot be recalled.'
                # A resumed turn restarts from scratch, so its previous start time no longer applies.
                db.execute('''UPDATE cc_live_turns SET state=?,
                                  started_at=CASE WHEN ?='QUEUED' THEN NULL ELSE started_at END,
                                  finished_at=CASE WHEN ?='CANCELLED' THEN ? ELSE NULL END
                              WHERE turn_id=?''',
                           (state, state, state, time.time(), row['turn_id']))
                db.execute('UPDATE cc_messages SET state=? WHERE message_id=?', ('LIVE_QUEUED' if state == 'QUEUED' else state, row['message_id']))
                db.execute('UPDATE cc_messages SET state=?,body=? WHERE message_id=?', (state, message, row['response_id']))
        # Persist operator intent before waking a transport that may immediately
        # raise on cancellation. Its error handler must observe PAUSED/CANCELLED.
        if action in {'pause', 'stop'} and agent in self.running:
            self.running[agent][1].set()

    # Where a RUNNING turn actually is, read from its operator message's delivery state.
    _PHASES: ClassVar[dict[str, str]] = {'LIVE_QUEUED': 'starting', 'CONNECTING': 'connecting',
                                         'LIVE_DELIVERED': 'responding'}
    _ACTIVE = ('QUEUED', 'RUNNING', 'PAUSED', 'INTERRUPTED')

    @classmethod
    def _describe(cls, row):
        row = dict(row)
        running = row['state'] == 'RUNNING'
        row['phase'] = cls._PHASES.get(row.pop('operator_state'), 'starting') if running else None
        body = row.pop('response_body') or ''
        # Only a responding or finished turn has provider text; before that the body is a placeholder.
        received = row['phase'] == 'responding' or row['state'] in {'COMPLETED', 'FAILED'}
        row['response_chars'] = len(body) if received else 0
        row['result_preview'] = body[:600] if row['state'] == 'COMPLETED' else None
        row['error'] = body[:600] if row['state'] == 'FAILED' else None
        return row

    def snapshot(self, scope, agent):
        with self.workspace.db() as db:
            rows = [self._describe(r) for r in db.execute('''SELECT t.*,m.body AS instruction,
                m.state AS operator_state, r.body AS response_body FROM cc_live_turns t
                JOIN cc_messages m ON m.message_id=t.message_id
                JOIN cc_messages r ON r.message_id=t.response_id
                WHERE t.scope=? AND t.agent_id=? ORDER BY t.created_at''', (scope, agent))]
            return {'live_turns': rows, 'live_usage': [json.loads(r['usage']) for r in rows if r['state'] == 'COMPLETED']}

    def agent_activity(self):
        """Per-agent live-turn summary across every chat, for truthful roster status."""
        marks = ','.join('?' * len(self._ACTIVE))
        with self.workspace.db() as db:
            counts = {}
            for row in db.execute(f'''SELECT agent_id, state, COUNT(*) AS n FROM cc_live_turns
                                      WHERE state IN ({marks}) GROUP BY agent_id, state''', self._ACTIVE):
                counts.setdefault(row['agent_id'], {})[row['state']] = row['n']
            detail = '''SELECT t.*, m.state AS operator_state, r.body AS response_body FROM cc_live_turns t
                        JOIN cc_messages m ON m.message_id=t.message_id
                        JOIN cc_messages r ON r.message_id=t.response_id'''
            running = {r['agent_id']: self._describe(r) for r in
                       db.execute(detail + " WHERE t.state='RUNNING' ORDER BY t.created_at")}
            latest = {r['agent_id']: self._describe(r) for r in db.execute(detail + '''
                WHERE t.created_at = (SELECT MAX(created_at) FROM cc_live_turns x WHERE x.agent_id=t.agent_id)''')}
        agents = set(counts) | set(running) | set(latest)
        return {
            'agents': {a: {'counts': counts.get(a, {}), 'running': running.get(a), 'latest': latest.get(a)}
                       for a in agents},
            'running_total': sum(c.get('RUNNING', 0) for c in counts.values()),
            'capacity': self.capacity,
        }

    def close(self):
        with self.workspace.db() as db:
            db.execute("UPDATE cc_live_turns SET state='INTERRUPTED' WHERE state IN ('QUEUED','RUNNING')")
            db.execute("UPDATE cc_messages SET state='INTERRUPTED' WHERE state IN ('LIVE_QUEUED','STREAMING','LIVE_DELIVERED','CONNECTING')")
        for future, event in self.running.values():
            event.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
