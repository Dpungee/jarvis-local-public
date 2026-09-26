"""Durable conversational inbox and explicit offline checkpoint simulation.

No model, tool, network or delegation execution is provided by this module.
Transactions serialize operator controls with simulator acknowledgements.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .redaction import contains_secret

ACTIVE = ('QUEUED', 'RUNNING', 'AWAITING_REPLY', 'PAUSED', 'INTERRUPTED')
KINDS = {'discuss', 'work', 'steer', 'reply', 'delegate'}


def identifier(prefix):
    return prefix + '_' + uuid.uuid4().hex


def text(value, limit=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('Text is required and must fit the field limit.')
    if '\x00' in value:
        raise ValueError('Invalid text.')
    return value.strip()


class ConversationWorkspace:
    def __init__(self, path: Path, *, roots=None):
        self.path = path
        self.roots = {key: Path(value).absolute() for key, value in (roots or {}).items()}
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cc_chats(
                    chat_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, title TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cc_local_grants(
                    project_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    capability TEXT NOT NULL, enabled INTEGER NOT NULL,
                    PRIMARY KEY(project_id,agent_id,capability));
                CREATE TABLE IF NOT EXISTS cc_attachments(
                    attachment_id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                    agent_id TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
                    message_id TEXT, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cc_projects(
                    project_id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cc_messages(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT UNIQUE NOT NULL, project_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL,
                    body TEXT NOT NULL, state TEXT NOT NULL, run_id TEXT,
                    request_id TEXT UNIQUE, digest TEXT, created_at REAL NOT NULL,
                    delivered_at REAL, applied_at REAL);
                CREATE TABLE IF NOT EXISTS cc_work(
                    run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, state TEXT NOT NULL, checkpoint INTEGER NOT NULL,
                    instruction TEXT NOT NULL, steering TEXT NOT NULL,
                    model TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cc_proposals(
                    proposal_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, request TEXT NOT NULL,
                    readback TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cc_artifacts(
                    artifact_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    name TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS cc_thread ON cc_messages(project_id,agent_id,sequence);
            ''')
            rows = db.execute("SELECT * FROM cc_work WHERE state IN ('RUNNING','AWAITING_REPLY','QUEUED')").fetchall()
            for row in rows:
                db.execute("UPDATE cc_work SET state='INTERRUPTED',updated_at=? WHERE run_id=?", (time.time(), row['run_id']))
                self._append(db, row['project_id'], row['agent_id'], 'system', 'status',
                             'Work interrupted by restart. Context and checkpoint retained; resume explicitly.',
                             'RECORDED', row['run_id'])
            db.execute("UPDATE cc_messages SET state='QUEUED' WHERE state='DELIVERED'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def ensure_project(self, project_id, name=None):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO cc_projects VALUES (?,?)',
                       (text(project_id, 200), text(name or project_id, 120)))

    def create_project(self, payload):
        if set(payload) != {'name'}:
            raise ValueError('Only a project name may be supplied; filesystem roots require startup configuration.')
        project = {'project_id': identifier('project'), 'name': text(payload['name'], 120)}
        self.ensure_project(**project)
        return project

    def projects(self):
        with self.db() as db:
            return [dict(row) | {'files_enabled': row['project_id'] in self.roots}
                    for row in db.execute('SELECT * FROM cc_projects ORDER BY name')]

    def chats(self, project, agent):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM cc_chats WHERE project_id=? AND agent_id=? ORDER BY created_at DESC', (project,agent))]

    def create_chat(self, project, agent, title='New conversation'):
        chat = {'chat_id':identifier('chat'),'project_id':project,'agent_id':agent,
                'title':text(title,120),'created_at':time.time()}
        with self.db() as db:
            db.execute('INSERT INTO cc_chats VALUES (?,?,?,?,?)',tuple(chat.values()))
        return chat

    def delete_chat(self, project, agent, chat_id, *, check_only=False):
        """Remove one chat and everything stored under its scope. Refused while its work is live."""
        scope = self.chat_scope(project, agent, chat_id)
        with self.db() as db:
            live = db.execute("SELECT 1 FROM cc_work WHERE project_id=? AND agent_id=? AND state IN"
                              " ('RUNNING','AWAITING_REPLY','QUEUED')", (scope, agent)).fetchone()
            if live:
                raise PermissionError('This conversation still has work in progress.')
            if check_only:
                return
            for table, column in (('cc_messages', 'project_id'), ('cc_work', 'project_id'),
                                  ('cc_proposals', 'project_id'), ('cc_artifacts', 'project_id'),
                                  ('cc_attachments', 'scope')):
                db.execute(f'DELETE FROM {table} WHERE {column}=? AND agent_id=?', (scope, agent))
            db.execute('DELETE FROM cc_chats WHERE chat_id=? AND project_id=? AND agent_id=?', (chat_id, project, agent))

    def chat_scope(self, project, agent, chat_id):
        if chat_id is None:
            return project  # Legacy API thread, retained without mixing with new chats.
        chat_id = text(chat_id, 200)
        with self.db() as db:
            row = db.execute('SELECT * FROM cc_chats WHERE chat_id=? AND project_id=? AND agent_id=?', (chat_id,project,agent)).fetchone()
            if not row:
                raise PermissionError('Conversation does not belong to this agent and project.')
        return project + '::' + chat_id

    def grants(self, project, agent):
        result = {'attachments':False, 'file_preview':False}
        with self.db() as db:
            for row in db.execute('SELECT * FROM cc_local_grants WHERE project_id=? AND agent_id=?',(project,agent)):
                result[row['capability']] = bool(row['enabled'])
        return result

    def set_grant(self, project, agent, payload):
        if set(payload) != {'capability','enabled'} or not isinstance(payload['capability'],str) or payload['capability'] not in {'attachments','file_preview'} or type(payload['enabled']) is not bool:
            raise PermissionError('Only local attachment and file-preview grants can be changed here.')
        with self.db() as db:
            db.execute('INSERT INTO cc_local_grants VALUES (?,?,?,?) ON CONFLICT(project_id,agent_id,capability) DO UPDATE SET enabled=excluded.enabled',
                       (project,agent,payload['capability'],int(payload['enabled'])))
            self._append(db,project,agent,'system','permission',
                         f"Local {payload['capability']} permission {'enabled' if payload['enabled'] else 'revoked'}. No cloud or tool authority added.",'RECORDED')
        return self.grants(project,agent)

    def attachment(self, project, agent, chat_id, payload):
        scope = self.chat_scope(project,agent,chat_id)
        if not self.grants(project,agent)['attachments']:
            raise PermissionError('Local attachment staging is not permitted. Enable it in Permissions.')
        if set(payload) != {'name','body'}:
            raise ValueError('Only explicit text attachments are accepted.')
        name, body = text(payload['name'],120), text(payload['body'],20000)
        if any(c in name for c in '/\\:') or name.startswith('.') or Path(name).suffix.lower() not in {'.txt','.md','.csv'}:
            raise PermissionError('Choose a plain text, Markdown or CSV file without a protected name.')
        if any(word in name.casefold() for word in ('credential','secret','token','private','password','id_rsa')) or contains_secret(body):
            raise PermissionError('Attachment withheld by secret screening.')
        aid = identifier('attachment')
        with self.db() as db:
            db.execute('INSERT INTO cc_attachments VALUES (?,?,?,?,?,NULL,?)',(aid,scope,agent,name,body,time.time()))
        return {'attachment_id':aid,'name':name,'characters':len(body),'state':'STAGED_LOCAL_ONLY'}

    def remove_attachment(self, project, agent, chat_id, attachment_id):
        scope = self.chat_scope(project,agent,chat_id)
        with self.db() as db:
            result = db.execute('DELETE FROM cc_attachments WHERE attachment_id=? AND scope=? AND agent_id=? AND message_id IS NULL',(attachment_id,scope,agent))
            if result.rowcount != 1:
                raise PermissionError('No staged attachment belongs to this thread.')
        return {'removed':True}

    @staticmethod
    def _append(db, project, agent, role, kind, body, state, run=None, request=None, digest=None):
        mid = identifier('message')
        db.execute('''INSERT INTO cc_messages(message_id,project_id,agent_id,role,kind,body,
                      state,run_id,request_id,digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                   (mid, project, agent, role, kind, body, state, run, request, digest, time.time()))
        return mid

    def send(self, project, agent, payload, *, simulated, enabled, model):
        if set(payload) - {'body', 'kind', 'request_id', 'run_id', 'attachments'}:
            raise ValueError('Unsupported message fields.')
        body = text(payload.get('body'))
        kind = payload.get('kind', 'discuss')
        if not isinstance(kind,str) or kind not in KINDS:
            raise ValueError('Choose discussion, new work, steering, reply or delegation proposal.')
        request = text(payload.get('request_id'), 200)
        attachments = payload.get('attachments',[])
        if not isinstance(attachments,list) or len(attachments)>5 or any(not isinstance(a,str) for a in attachments) or len(set(attachments)) != len(attachments):
            raise ValueError('At most five unique staged attachment references are accepted.')
        digest = hashlib.sha256(json.dumps([project, agent, body, kind, payload.get('run_id'),attachments]).encode()).hexdigest()
        with self.db() as db:
            old = db.execute('SELECT * FROM cc_messages WHERE request_id=?', (request,)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ValueError('Request ID reused for different content.')
                return dict(old)
            for aid in attachments:
                attached = db.execute('SELECT 1 FROM cc_attachments WHERE attachment_id=? AND scope=? AND agent_id=? AND message_id IS NULL',(aid,project,agent)).fetchone()
                if not attached:
                    raise PermissionError('Attachment is not staged in this conversation.')
            run = None
            state = 'QUEUED' if simulated and enabled else 'BLOCKED'
            if kind in {'steer', 'reply'} and state != 'BLOCKED':
                run = db.execute('SELECT * FROM cc_work WHERE run_id=? AND project_id=? AND agent_id=?',
                                 (payload.get('run_id'), project, agent)).fetchone()
                if run is None or run['state'] not in ACTIVE:
                    raise ValueError('The targeted work is no longer active. Message was not applied.')
                if kind == 'reply' and run['state'] != 'AWAITING_REPLY':
                    raise ValueError('This work is not waiting for clarification.')
                run = run['run_id']
            if kind == 'work' and state != 'BLOCKED':
                run = identifier('work')
                db.execute('INSERT INTO cc_work VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (run, project, agent, 'QUEUED', 0, body, '', model, time.time(), time.time()))
            mid = self._append(db, project, agent, 'operator', kind, body, state, run, request, digest)
            for aid in attachments:
                db.execute('UPDATE cc_attachments SET message_id=? WHERE attachment_id=?',(mid,aid))
            if kind == 'delegate':
                proposal = identifier('proposal')
                readback = 'Requested delegation: ' + body + '\nApprove this exact request? Dispatch is unavailable in this preview.'
                db.execute('INSERT INTO cc_proposals VALUES (?,?,?,?,?,?,?)',
                           (proposal, project, agent, body, readback, 'AWAITING_APPROVAL', time.time()))
                db.execute("UPDATE cc_messages SET state='AWAITING_APPROVAL',run_id=? WHERE message_id=?", (proposal, mid))
                self._append(db, project, agent, 'system', 'approval', readback, 'RECORDED')
            elif state == 'BLOCKED':
                self._append(db, project, agent, 'system', 'status',
                             'Message saved locally. Live conversational execution is unavailable.' if not simulated
                             else 'Message saved locally but not delivered: enable or resume this agent, then send again.', 'RECORDED')
            return dict(db.execute('SELECT * FROM cc_messages WHERE message_id=?', (mid,)).fetchone())

    def approve(self, project, agent, payload):
        if set(payload) != {'proposal_id', 'decision'} or not isinstance(payload['decision'],str) or payload['decision'] not in {'approve', 'deny'}:
            raise ValueError('An exact proposal and approval decision are required.')
        with self.db() as db:
            proposal = db.execute('SELECT * FROM cc_proposals WHERE proposal_id=? AND project_id=? AND agent_id=?',
                                  (payload['proposal_id'], project, agent)).fetchone()
            if not proposal or proposal['state'] != 'AWAITING_APPROVAL':
                raise ValueError('No pending readback matches this thread.')
            state = 'APPROVED_NOT_DISPATCHED' if payload['decision'] == 'approve' else 'DENIED'
            db.execute('UPDATE cc_proposals SET state=? WHERE proposal_id=?', (state, proposal['proposal_id']))
            db.execute('UPDATE cc_messages SET state=? WHERE run_id=? AND kind=?',
                       (state, proposal['proposal_id'], 'delegate'))
            self._append(db, project, agent, 'system', 'approval',
                         'Approval recorded. No agent was created or assigned; delegation execution is unavailable.'
                         if state == 'APPROVED_NOT_DISPATCHED' else 'Delegation declined. Nothing dispatched.', 'RECORDED')
            return {'state': state}

    def control(self, agent, action):
        with self.db() as db:
            rows = db.execute("SELECT * FROM cc_work WHERE agent_id=? AND state IN ('RUNNING','QUEUED','PAUSED','AWAITING_REPLY','INTERRUPTED')", (agent,)).fetchall()
            for row in rows:
                if action in {'start', 'resume'} and row['state'] not in {'PAUSED', 'INTERRUPTED'}:
                    continue
                awaiting = row['checkpoint'] == 2 and not db.execute(
                    "SELECT 1 FROM cc_messages WHERE run_id=? AND kind='reply' AND state='APPLIED'", (row['run_id'],)).fetchone()
                state = 'CANCELLED' if action == 'stop' else 'PAUSED' if action == 'pause' else 'AWAITING_REPLY' if awaiting else 'QUEUED'
                db.execute('UPDATE cc_work SET state=?,updated_at=? WHERE run_id=?', (state, time.time(), row['run_id']))
                self._append(db, row['project_id'], agent, 'system', 'status',
                             f'{state.title()} at simulator checkpoint {row["checkpoint"]}.', 'RECORDED', row['run_id'])
            if action == 'stop':
                db.execute("UPDATE cc_messages SET state='CANCELLED' WHERE agent_id=? AND state IN ('QUEUED','DELIVERED')", (agent,))

    def tick(self, agents, capacity):
        """One atomic simulator checkpoint; delivery is visible before application."""
        # Queued records were admitted only to the offline simulator. Changing a
        # future model binding neither upgrades them to cloud calls nor discards them.
        allowed = {a['agent_id']: a for a in agents if a['lifecycle'] == 'RUNNING'}
        with self.db() as db:
            messages = db.execute("SELECT * FROM cc_messages WHERE state IN ('QUEUED','DELIVERED') ORDER BY sequence").fetchall()
            for message in messages:
                aid, project = message['agent_id'], message['project_id']
                if aid not in allowed:
                    continue
                work = db.execute('SELECT * FROM cc_work WHERE run_id=?', (message['run_id'],)).fetchone()
                if work and work['state'] in {'PAUSED', 'INTERRUPTED'}:
                    continue
                if work and work['state'] in {'COMPLETED', 'CANCELLED'}:
                    db.execute("UPDATE cc_messages SET state='NOT_APPLIED' WHERE message_id=?", (message['message_id'],))
                    continue
                if message['state'] == 'QUEUED':
                    db.execute("UPDATE cc_messages SET state='DELIVERED',delivered_at=? WHERE message_id=?", (time.time(), message['message_id']))
                    continue
                kind = message['kind']
                if kind == 'work':
                    response = 'Simulation: work is admitted to the checkpoint queue. No model or tools are running.'
                elif kind in {'steer', 'reply'}:
                    db.execute("UPDATE cc_work SET steering=steering || ?, state=CASE WHEN state='AWAITING_REPLY' THEN 'QUEUED' ELSE state END,updated_at=? WHERE run_id=?",
                               ('\n' + message['body'], time.time(), work['run_id']))
                    response = f'Simulation: {kind} applied at checkpoint {work["checkpoint"]}; existing progress retained.'
                else:
                    response = ('Simulation: discussion received in this thread. It has not created or changed work. '
                                'This scripted adapter cannot answer real questions; live chat is gated.')
                db.execute("UPDATE cc_messages SET state='APPLIED',applied_at=? WHERE message_id=?", (time.time(), message['message_id']))
                self._append(db, project, aid, 'simulator', 'acknowledgement', response, 'RECORDED', message['run_id'])
            busy = set()
            rows = db.execute("SELECT * FROM cc_work WHERE state IN ('QUEUED','RUNNING','AWAITING_REPLY','PAUSED','INTERRUPTED') ORDER BY created_at,run_id").fetchall()
            slots = 0
            for work in rows:
                aid = work['agent_id']
                if aid not in allowed or aid in busy:
                    continue
                busy.add(aid)
                if work['state'] in {'AWAITING_REPLY', 'PAUSED', 'INTERRUPTED'} or slots >= capacity:
                    continue
                pending = db.execute("SELECT 1 FROM cc_messages WHERE run_id=? AND state IN ('QUEUED','DELIVERED')", (work['run_id'],)).fetchone()
                if pending:
                    continue
                slots += 1
                checkpoint = work['checkpoint'] + 1
                state = 'AWAITING_REPLY' if checkpoint == 2 else 'COMPLETED' if checkpoint >= 20 else 'RUNNING'
                db.execute('UPDATE cc_work SET state=?,checkpoint=?,updated_at=? WHERE run_id=?', (state, checkpoint, time.time(), work['run_id']))
                if state == 'AWAITING_REPLY':
                    self._append(db, work['project_id'], aid, 'simulator', 'clarification',
                                 'Simulation checkpoint: what should the example result emphasize? Choose “Reply to clarification” and answer here.', 'RECORDED', work['run_id'])
                if state == 'COMPLETED':
                    body = 'SIMULATED ARTIFACT — no model, files or tools were used.\n\nOriginal request:\n' + work['instruction'] + '\n\nApplied follow-ups:\n' + work['steering']
                    db.execute('INSERT INTO cc_artifacts VALUES (?,?,?,?,?,?,?)',
                               (identifier('artifact'), work['project_id'], aid, work['run_id'], 'Simulation result.txt', body, time.time()))
                    self._append(db, work['project_id'], aid, 'simulator', 'result',
                                 'Simulation completed. The example artifact includes the original request and acknowledged follow-ups.', 'RECORDED', work['run_id'])

    def snapshot(self, project, agent):
        with self.db() as db:
            messages = db.execute('SELECT * FROM cc_messages WHERE project_id=? AND agent_id=? ORDER BY sequence DESC LIMIT 300', (project, agent)).fetchall()
            work = db.execute('SELECT * FROM cc_work WHERE project_id=? AND agent_id=? ORDER BY created_at DESC LIMIT 100', (project, agent)).fetchall()
            proposals = db.execute('SELECT * FROM cc_proposals WHERE project_id=? AND agent_id=? ORDER BY created_at', (project, agent)).fetchall()
            artifacts = db.execute('SELECT * FROM cc_artifacts WHERE project_id=? ORDER BY created_at DESC LIMIT 100', (project,)).fetchall()
            attachments = db.execute('SELECT attachment_id,name,length(body) AS characters,message_id FROM cc_attachments WHERE scope=? AND agent_id=? ORDER BY created_at',(project,agent)).fetchall()
            return {'messages': [dict(r) for r in reversed(messages)], 'work': [dict(r) for r in work],
                    'proposals': [dict(r) for r in proposals], 'artifacts': [dict(r) for r in artifacts],
                    'attachments':[dict(r) for r in attachments],
                    'usage':{'tokens':None,'capacity':None,'status':'UNAVAILABLE: no verified tokenizer or provider capacity.',
                             'visible_characters':sum(len(r['body']) for r in messages)},
                    'history_window': 300, 'changes': [], 'changes_status': 'No file-writing adapter is enabled.'}

    def files(self, project, relative=''):
        root = self.roots.get(project)
        if root is None:
            raise PermissionError('No filesystem workspace is authorized for this project.')
        if not isinstance(relative, str) or '\\' in relative or ':' in relative or '\x00' in relative:
            raise PermissionError('Invalid workspace path.')
        parts = PurePosixPath(relative).parts
        if relative.startswith('/') or '..' in parts:
            raise PermissionError('Path is outside this workspace.')
        blocked = {'.git', '.ssh', '.aws', '.azure', '.env', '.kube', '.gnupg', 'credentials', 'secrets', 'node_modules', '__pycache__'}
        def safe_part(part):
            stem = part.casefold().split('.')[0]
            devices = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1,10)), *(f'lpt{i}' for i in range(1,10))}
            return (not part.startswith('.') and part.casefold() not in blocked
                    and stem not in blocked | devices | {'id_rsa','id_ed25519','tokens','cookies','credentials'}
                    and not any(ord(c) < 32 or c in '<>"|?*' for c in part)
                    and part == part.rstrip(' .'))
        def linked(path):
            info = path.lstat()
            return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))
        current = root
        for parent in (root, *root.parents):
            if linked(parent):
                raise PermissionError('Linked workspace roots are not supported.')
        for part in parts:
            if not safe_part(part):
                raise PermissionError('Protected path.')
            current = current / part
            if linked(current):
                raise PermissionError('Links and reparse points are not permitted.')
        if current.is_dir():
            entries = []
            for child in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
                if safe_part(child.name) and not linked(child):
                    entries.append({'name': child.name, 'path': child.relative_to(root).as_posix(), 'directory': child.is_dir()})
                if len(entries) >= 200:
                    break
            return {'path': relative, 'entries': entries, 'limit': 200}
        if current.suffix.lower() not in {'.md', '.txt', '.py', '.js', '.css', '.html', '.toml'} or current.stat().st_size > 100000:
            raise PermissionError('Only bounded source and text files may be previewed.')
        # No write/tool adapter can mutate these roots. Concurrent external mutations
        # remain a containment gate before granting agents filesystem access.
        if current.stat().st_nlink != 1:
            raise PermissionError('Hard-linked files are not permitted.')
        body = current.read_text(encoding='utf-8')
        if contains_secret(body):
            raise PermissionError('Content withheld by secret screening.')
        return {'path': relative, 'body': body}
