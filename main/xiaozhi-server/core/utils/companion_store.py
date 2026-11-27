"""Role affect outbox/state on the existing managed-memory SQLite connection.

An event sequence is allocated before generation, not when a slow observer ends.
Role state never enters the factual ledger or PowerMem vector index.
"""
from contextlib import contextmanager
import hashlib
import json
import time


class CompanionStore:
    def __init__(self, ledger, initialize_affect=True):
        self.ledger = ledger
        if not initialize_affect:
            return
        with ledger.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS companion_affect_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    scope TEXT NOT NULL, companion_id TEXT NOT NULL, mode TEXT NOT NULL,
                    policy TEXT NOT NULL, epoch INTEGER NOT NULL, session_id TEXT NOT NULL,
                    source_turn TEXT NOT NULL, observed_at REAL NOT NULL, deadline REAL NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
                    expected_revision INTEGER, error_class TEXT);
                CREATE INDEX IF NOT EXISTS affect_ready ON companion_affect_events(scope,companion_id,mode,status,seq);
                CREATE TABLE IF NOT EXISTS companion_affect(
                    scope TEXT NOT NULL, companion_id TEXT NOT NULL, mode TEXT NOT NULL,
                    epoch INTEGER NOT NULL, policy TEXT NOT NULL, revision INTEGER NOT NULL,
                    last_seq INTEGER NOT NULL, source_event TEXT NOT NULL, source_turn TEXT NOT NULL,
                    observed_at REAL NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY(scope,companion_id,mode));
            ''')

    @contextmanager
    def db(self):
        with self.ledger.db() as db:
            # This optional feature yields promptly to factual-ledger writers.
            db.execute('PRAGMA busy_timeout=50')
            yield db

    def reserve(self, scope, companion_id, mode, policy, session_id, turn, ttl, capacity, stamp=None):
        stamp = time.time() if stamp is None else stamp
        identifier = hashlib.sha256(json.dumps([scope, companion_id, mode, session_id, turn]).encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            epoch = self.ledger._epoch(db, scope)
            db.execute("UPDATE companion_affect_events SET status='expired',payload='{}' "
                       "WHERE deadline<? AND status IN ('reserved','queued','processing')", (stamp,))
            old = db.execute('SELECT * FROM companion_affect_events WHERE id=?', (identifier,)).fetchone()
            if old:
                return dict(old) if old['epoch'] == epoch and old['status'] == 'reserved' else None
            count = db.execute("SELECT COUNT(*) FROM companion_affect_events WHERE scope=? AND companion_id=? "
                               "AND status IN ('reserved','queued','processing')", (scope, companion_id)).fetchone()[0]
            if count >= capacity:
                return None
            db.execute("INSERT INTO companion_affect_events(id,scope,companion_id,mode,policy,epoch,session_id,"
                       "source_turn,observed_at,deadline,status) VALUES(?,?,?,?,?,?,?,?,?,?,'reserved')",
                       (identifier, scope, companion_id, mode, policy, epoch, session_id, turn, stamp, stamp + ttl))
            # Keep completed metadata bounded; payloads are cleared on completion.
            db.execute("DELETE FROM companion_affect_events WHERE scope=? AND status NOT IN ('reserved','queued','processing') "
                       "AND seq NOT IN (SELECT seq FROM companion_affect_events WHERE scope=? ORDER BY seq DESC LIMIT 512)",
                       (scope, scope))
            return dict(db.execute('SELECT * FROM companion_affect_events WHERE id=?', (identifier,)).fetchone())

    def enqueue(self, ticket, payload):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.ledger._epoch(db, ticket['scope']) != ticket['epoch']:
                return False
            cursor = db.execute("UPDATE companion_affect_events SET status='queued',payload=? "
                                "WHERE id=? AND status='reserved' AND deadline>=?",
                                (json.dumps(payload, ensure_ascii=False), ticket['id'], time.time()))
            return cursor.rowcount == 1

    def read(self, scope, companion_id, mode, policy):
        with self.db() as db:
            db.execute('BEGIN')
            epoch = db.execute('SELECT epoch FROM scopes WHERE scope=?', (scope,)).fetchone()
            row = db.execute('SELECT * FROM companion_affect WHERE scope=? AND companion_id=? AND mode=?',
                             (scope, companion_id, mode)).fetchone()
            if not row or not epoch or row['epoch'] != epoch[0] or row['policy'] != policy:
                return None
            if row['expires_at'] <= time.time():
                db.execute('DELETE FROM companion_affect WHERE scope=? AND companion_id=? AND mode=? AND expires_at<=?',
                           (scope, companion_id, mode, time.time()))
                return None
            value = dict(row)
            value['state'] = json.loads(value['state'])
            return value

    def claim(self, scope, companion_id, mode, policy):
        stamp = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM companion_affect WHERE scope=? AND companion_id=? AND mode=? AND expires_at<=?',
                       (scope, companion_id, mode, stamp))
            db.execute("UPDATE companion_affect_events SET status='expired',payload='{}' "
                       "WHERE deadline<? AND status IN ('reserved','queued','processing')", (stamp,))
            # A second worker cannot observe the same subject concurrently.
            if db.execute("SELECT 1 FROM companion_affect_events WHERE scope=? AND companion_id=? AND mode=? "
                          "AND status='processing' LIMIT 1", (scope, companion_id, mode)).fetchone():
                return None
            epoch = self.ledger._epoch(db, scope)
            row = db.execute("SELECT * FROM companion_affect_events WHERE scope=? AND companion_id=? AND mode=? "
                             "AND policy=? AND epoch=? AND status='queued' ORDER BY seq LIMIT 1",
                             (scope, companion_id, mode, policy, epoch)).fetchone()
            if not row:
                return None
            current = db.execute('SELECT revision FROM companion_affect WHERE scope=? AND companion_id=? AND mode=?',
                                 (scope, companion_id, mode)).fetchone()
            revision = current[0] if current else 0
            db.execute("UPDATE companion_affect_events SET status='processing',expected_revision=? WHERE id=?",
                       (revision, row['id']))
            result = dict(row)
            result['expected_revision'] = revision
            result['payload'] = json.loads(result['payload'])
            return result

    def apply(self, job, state, expires_at):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            event = db.execute('SELECT status,deadline FROM companion_affect_events WHERE id=?', (job['id'],)).fetchone()
            if not event or event['status'] != 'processing':
                return False
            old = db.execute('SELECT * FROM companion_affect WHERE scope=? AND companion_id=? AND mode=?',
                             (job['scope'], job['companion_id'], job['mode'])).fetchone()
            valid = (event['deadline'] >= time.time() and self.ledger._epoch(db, job['scope']) == job['epoch']
                     and (old['revision'] if old else 0) == job['expected_revision']
                     and (not old or old['last_seq'] < job['seq']))
            if not valid:
                db.execute("UPDATE companion_affect_events SET status='stale',payload='{}' WHERE id=?", (job['id'],))
                return False
            db.execute('INSERT INTO companion_affect VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(scope,companion_id,mode) DO UPDATE SET epoch=excluded.epoch,policy=excluded.policy,'
                       'revision=excluded.revision,last_seq=excluded.last_seq,source_event=excluded.source_event,'
                       'source_turn=excluded.source_turn,observed_at=excluded.observed_at,expires_at=excluded.expires_at,state=excluded.state',
                       (job['scope'], job['companion_id'], job['mode'], job['epoch'], job['policy'], job['expected_revision'] + 1,
                        job['seq'], job['id'], state.get('source_turn', job['source_turn']), state['observed_at'], expires_at,
                        json.dumps(state, ensure_ascii=False)))
            db.execute("UPDATE companion_affect_events SET status='applied',payload='{}' WHERE id=?", (job['id'],))
            return True

    def fail(self, identifier, error_class):
        with self.db() as db:
            db.execute("UPDATE companion_affect_events SET status='failed',payload='{}',error_class=? "
                       "WHERE id=? AND status='processing'", (error_class, identifier))

    def cancel(self, identifier, withdraw=False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE companion_affect_events SET status='cancelled',payload='{}' "
                       "WHERE id=? AND status IN ('reserved','queued','processing')", (identifier,))
            if withdraw:
                db.execute('DELETE FROM companion_affect WHERE source_event=?', (identifier,))


class EpisodeStore(CompanionStore):
    """Durable sourced episodes and an explicitly separate extraction outbox."""

    def __init__(self, ledger):
        super().__init__(ledger, initialize_affect=False)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS companion_episode_sources(
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, companion_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL, session_id TEXT NOT NULL, source_turn TEXT NOT NULL,
                    observed_at REAL NOT NULL, user_text TEXT NOT NULL, assistant_text TEXT NOT NULL DEFAULT '',
                    delivery_status TEXT NOT NULL DEFAULT 'unanswered', semantic_revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS companion_episode_jobs(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, source_id TEXT NOT NULL,
                    scope TEXT NOT NULL, companion_id TEXT NOT NULL, epoch INTEGER NOT NULL, policy TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
                    deadline REAL NOT NULL, error_class TEXT);
                CREATE INDEX IF NOT EXISTS episode_ready ON companion_episode_jobs(scope,companion_id,status,due,seq);
                CREATE TABLE IF NOT EXISTS companion_episodes(
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, companion_id TEXT NOT NULL, epoch INTEGER NOT NULL,
                    source_id TEXT NOT NULL UNIQUE, source_seq INTEGER NOT NULL, observed_at REAL NOT NULL,
                    expires_at REAL NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS episode_scope ON companion_episodes(scope,companion_id,epoch,status,observed_at);
                CREATE TABLE IF NOT EXISTS companion_episode_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, companion_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL, action TEXT NOT NULL, revision INTEGER NOT NULL,
                    observed_at REAL NOT NULL, note TEXT NOT NULL DEFAULT '');
            ''')

    def reserve_episode(self, scope, companion, policy, session, turn, user_text, ttl=60, capacity=32):
        stamp = time.time()
        identifier = hashlib.sha256(json.dumps([scope, companion, session, turn]).encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            epoch = self.ledger._epoch(db, scope)
            identifier = hashlib.sha256((identifier + ':' + str(epoch)).encode()).hexdigest()
            self._expire(db, scope, companion, stamp)
            old = db.execute('SELECT * FROM companion_episode_jobs WHERE id=?', (identifier,)).fetchone()
            if old:
                return dict(old) if old['epoch'] == epoch and old['status'] == 'reserved' else None
            count = db.execute("SELECT COUNT(*) FROM companion_episode_jobs WHERE scope=? AND companion_id=? "
                               "AND status IN ('reserved','queued','processing')", (scope, companion)).fetchone()[0]
            if count >= capacity:
                return None
            db.execute('INSERT INTO companion_episode_sources(id,scope,companion_id,epoch,session_id,source_turn,observed_at,user_text) '
                       'VALUES(?,?,?,?,?,?,?,?)', (identifier, scope, companion, epoch, session, turn, stamp, user_text))
            db.execute("INSERT INTO companion_episode_jobs(id,source_id,scope,companion_id,epoch,policy,status,deadline) "
                       "VALUES(?,?,?,?,?,?,'reserved',?)", (identifier, identifier, scope, companion, epoch, policy, stamp + ttl))
            return dict(db.execute('SELECT * FROM companion_episode_jobs WHERE id=?', (identifier,)).fetchone())

    @staticmethod
    def _expire(db, scope, companion, stamp):
        db.execute("UPDATE companion_episode_jobs SET status='expired' WHERE scope=? AND companion_id=? "
                   "AND deadline<? AND status IN ('reserved','queued','processing')", (scope, companion, stamp))
        db.execute('DELETE FROM companion_episodes WHERE scope=? AND companion_id=? AND expires_at<=?', (scope, companion, stamp))
        db.execute("DELETE FROM companion_episode_sources WHERE scope=? AND companion_id=? "
                   "AND id NOT IN (SELECT source_id FROM companion_episodes) AND id NOT IN "
                   "(SELECT source_id FROM companion_episode_jobs WHERE status IN ('reserved','queued','processing'))",
                   (scope, companion))
        db.execute('DELETE FROM companion_episode_audit WHERE scope=? AND companion_id=? AND episode_id NOT IN '
                   '(SELECT id FROM companion_episodes)', (scope, companion))
        db.execute("DELETE FROM companion_episode_jobs WHERE scope=? AND companion_id=? AND status NOT IN ('reserved','queued','processing') "
                   "AND seq NOT IN (SELECT seq FROM companion_episode_jobs WHERE scope=? AND companion_id=? ORDER BY seq DESC LIMIT 512)",
                   (scope, companion, scope, companion))

    def finish_episode(self, ticket, assistant='', status='generated'):
        if status not in ('generated', 'failed', 'interrupted', 'unanswered'):
            raise ValueError('Invalid episode source status')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.ledger._epoch(db, ticket['scope']) != ticket['epoch']:
                return False
            row = db.execute('SELECT * FROM companion_episode_sources WHERE id=?', (ticket['source_id'],)).fetchone()
            if not row:
                return False
            if row['delivery_status'] == 'unanswered':
                db.execute('UPDATE companion_episode_sources SET assistant_text=?,delivery_status=?,semantic_revision=semantic_revision+1 WHERE id=?',
                           (assistant, status, ticket['source_id']))
            cursor = db.execute("UPDATE companion_episode_jobs SET status='queued' WHERE id=? AND status='reserved' AND deadline>=?",
                                (ticket['id'], time.time()))
            return cursor.rowcount == 1

    def episode_delivery(self, ticket, status):
        if status not in ('audio_sent', 'interrupted'):
            raise ValueError('Invalid episode delivery status')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.ledger._epoch(db, ticket['scope']) != ticket['epoch']:
                return False
            if status == 'interrupted':
                db.execute("UPDATE companion_episode_sources SET delivery_status='interrupted',semantic_revision=semantic_revision+1 "
                           "WHERE id=? AND delivery_status<>'interrupted'", (ticket['source_id'],))
                db.execute("UPDATE companion_episode_jobs SET status='queued' WHERE id=? AND status='reserved'", (ticket['id'],))
            else:
                db.execute("UPDATE companion_episode_sources SET delivery_status='audio_sent' WHERE id=? AND delivery_status='generated'",
                           (ticket['source_id'],))
            return True

    def claim_episode(self, scope, companion, policy):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._expire(db, scope, companion, time.time())
            epoch = self.ledger._epoch(db, scope)
            if db.execute("SELECT 1 FROM companion_episode_jobs WHERE scope=? AND companion_id=? AND status='processing'", (scope, companion)).fetchone():
                return None
            row = db.execute("SELECT * FROM companion_episode_jobs WHERE scope=? AND companion_id=? AND epoch=? AND policy=? "
                             "AND status='queued' AND due<=? ORDER BY seq LIMIT 1", (scope, companion, epoch, policy, time.time())).fetchone()
            if not row:
                return None
            db.execute("UPDATE companion_episode_jobs SET status='processing',attempts=attempts+1 WHERE id=?", (row['id'],))
            return dict(row, attempts=row['attempts'] + 1)

    def episode_source(self, ticket):
        with self.db() as db:
            row = db.execute('SELECT s.* FROM companion_episode_sources s JOIN scopes p ON p.scope=s.scope AND p.epoch=s.epoch '
                             'WHERE s.id=? AND s.epoch=?', (ticket['source_id'], ticket['epoch'])).fetchone()
            return dict(row) if row else None

    def newer_episode_sources(self, job):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT s.user_text FROM companion_episode_sources s '
                'JOIN companion_episode_jobs j ON j.source_id=s.id WHERE j.scope=? AND j.companion_id=? '
                'AND j.epoch=? AND j.seq>? ORDER BY j.seq DESC LIMIT 16',
                (job['scope'],job['companion_id'],job['epoch'],job['seq']))]

    def stale_episode(self, job):
        with self.db() as db:
            db.execute("UPDATE companion_episode_jobs SET status='stale' WHERE id=? AND epoch=? AND status='processing'",
                       (job['id'],job['epoch']))

    def cancel_episode(self, ticket):
        with self.db() as db:
            db.execute("UPDATE companion_episode_jobs SET status='cancelled' WHERE id=? AND epoch=? "
                       "AND status IN ('reserved','queued','processing')",(ticket['id'],ticket['epoch']))

    def episode_records(self, scope, companion, limit=256, include_withdrawn=False):
        with self.db() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT epoch FROM scopes WHERE scope=?', (scope,)).fetchone()
            epoch = row[0] if row else 0
            rows = db.execute('SELECT e.*,s.user_text,s.assistant_text,s.delivery_status,s.semantic_revision,s.session_id,s.source_turn '
                              'FROM companion_episodes e LEFT JOIN companion_episode_sources s ON s.id=e.source_id '
                              'WHERE e.scope=? AND e.companion_id=? AND e.epoch=? AND e.expires_at>? '
                              + ('' if include_withdrawn else "AND e.status IN ('active','resolved') ")
                              + 'ORDER BY e.source_seq DESC LIMIT ?', (scope, companion, epoch, time.time(), max(1, min(512, limit)))).fetchall()
            result=[]
            for row in rows:
                value=dict(row);value['payload']=json.loads(value['payload']);result.append(value)
            return epoch, result

    def episodes_current(self, scope, companion, token):
        with self.db() as db:
            row = db.execute('SELECT epoch FROM scopes WHERE scope=?', (scope,)).fetchone()
            if (row[0] if row else 0) != token[0]:
                return False
            for identifier, revision, source_revision in token[1]:
                found = db.execute("SELECT e.revision,s.semantic_revision FROM companion_episodes e JOIN companion_episode_sources s ON s.id=e.source_id "
                                   "WHERE e.id=? AND e.scope=? AND e.companion_id=? AND e.status IN ('active','resolved') AND e.expires_at>?",
                                   (identifier, scope, companion, time.time())).fetchone()
                if not found or tuple(found) != (revision, source_revision):
                    return False
            return True

    def apply_episode(self, job, result, references, retention_days):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT status,deadline FROM companion_episode_jobs WHERE id=?', (job['id'],)).fetchone()
            source = db.execute('SELECT * FROM companion_episode_sources WHERE id=?', (job['source_id'],)).fetchone()
            valid = (current and current['status']=='processing' and current['deadline']>=time.time() and source
                     and self.ledger._epoch(db,job['scope'])==job['epoch'])
            targets={}
            for ref in references:
                row=db.execute('SELECT * FROM companion_episodes WHERE id=? AND scope=? AND companion_id=?',
                               (ref['id'],job['scope'],job['companion_id'])).fetchone()
                if not row or row['revision']!=ref['revision'] or row['status'] not in ('active','resolved'):
                    valid=False
                else:
                    original=db.execute('SELECT semantic_revision FROM companion_episode_sources WHERE id=?',(row['source_id'],)).fetchone()
                    if not original or original[0]!=ref['source_revision']:valid=False
                    targets[row['id']]=row
            if not valid:
                if current:db.execute("UPDATE companion_episode_jobs SET status='stale' WHERE id=?",(job['id'],))
                return False
            if not result['keep']:
                db.execute("UPDATE companion_episode_jobs SET status='skipped' WHERE id=?",(job['id'],))
                db.execute('DELETE FROM companion_episode_sources WHERE id=?',(job['source_id'],))
                return True
            for key,status in (('supersedes','superseded'),('resolves','resolved')):
                for identifier in result[key]:
                    target=targets.get(identifier)
                    if not target or target['source_seq']>=job['seq']:
                        raise ValueError('Invalid episode target sequence')
                    db.execute('UPDATE companion_episodes SET status=?,revision=revision+1 WHERE id=?',(status,identifier))
                    db.execute('INSERT INTO companion_episode_audit(scope,companion_id,episode_id,action,revision,observed_at) VALUES(?,?,?,?,?,?)',
                               (job['scope'],job['companion_id'],identifier,status,target['revision']+1,time.time()))
            identifier=hashlib.sha256((job['source_id']+':episode').encode()).hexdigest()
            payload=dict(result)
            if source['delivery_status'] in ('interrupted','failed','unanswered'):
                payload.update(role_reaction='',interpretation='')
            db.execute('INSERT OR IGNORE INTO companion_episodes VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier,job['scope'],job['companion_id'],job['epoch'],job['source_id'],job['seq'],source['observed_at'],
                        source['observed_at']+retention_days*86400,'active',1,json.dumps(payload,ensure_ascii=False)))
            db.execute("UPDATE companion_episode_jobs SET status='done' WHERE id=?",(job['id'],))
            return True

    def fail_episode(self, job, error_class):
        with self.db() as db:
            db.execute("UPDATE companion_episode_jobs SET status=?,error_class=?,due=? WHERE id=? AND status='processing'",
                       ('queued' if job['attempts']<2 else 'failed',error_class,time.time()+1,job['id']))

    def maintain_episode(self, scope, companion, identifier, revision, changes=None, delete=False, note=''):
        allowed={'topic','summary','role_reaction','interpretation','unfinished'}
        if changes is not None and (not isinstance(changes,dict) or set(changes)-allowed):
            raise ValueError('Cannot alter episode provenance')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT e.* FROM companion_episodes e JOIN scopes s ON s.scope=e.scope AND s.epoch=e.epoch '
                           'WHERE e.id=? AND e.scope=? AND e.companion_id=?',(identifier,scope,companion)).fetchone()
            if not row or row['revision']!=revision or row['status']=='deleted':
                return False
            payload=json.loads(row['payload']);payload.update(changes or {})
            if any(not isinstance(v,str) or len(v)>500 for k,v in (changes or {}).items() if k!='unfinished'):
                raise ValueError('Invalid episode edit')
            if 'unfinished' in (changes or {}) and type(changes['unfinished']) is not bool:
                raise ValueError('Invalid unfinished status')
            if delete:
                payload={};db.execute('DELETE FROM companion_episode_sources WHERE id=?',(row['source_id'],))
                db.execute('DELETE FROM companion_episode_audit WHERE episode_id=?',(identifier,))
            db.execute('UPDATE companion_episodes SET payload=?,status=?,revision=revision+1 WHERE id=?',
                       (json.dumps(payload,ensure_ascii=False),'deleted' if delete else row['status'],identifier))
            db.execute('INSERT INTO companion_episode_audit(scope,companion_id,episode_id,action,revision,observed_at,note) VALUES(?,?,?,?,?,?,?)',
                       (scope,companion,identifier,'delete' if delete else 'edit',revision+1,time.time(),str(note)[:160] if not delete else ''))
            return True


class TopicStore(CompanionStore):
    """Small scoped conversational state, never a second fact ledger."""
    def __init__(self,ledger):
        super().__init__(ledger,initialize_affect=False)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS companion_topics(
                id TEXT PRIMARY KEY,scope TEXT NOT NULL,companion_id TEXT NOT NULL,
                epoch INTEGER NOT NULL,topic TEXT NOT NULL,pending_point TEXT NOT NULL,
                user_preference TEXT NOT NULL DEFAULT '{}',source_refs TEXT NOT NULL DEFAULT '[]',
                session_id TEXT NOT NULL,source_turn TEXT NOT NULL,last_mentioned REAL NOT NULL,
                status TEXT NOT NULL,expires_at REAL NOT NULL,revision INTEGER NOT NULL,
                is_current INTEGER NOT NULL DEFAULT 0,seen_turns TEXT NOT NULL DEFAULT '[]',
                source_episode TEXT,episode_revision INTEGER);
                CREATE INDEX IF NOT EXISTS topics_subject ON companion_topics(scope,companion_id,epoch,last_mentioned);''')

    def catalog(self,scope,companion,limit=5):
        with self.db() as db:
            epoch=self.ledger._epoch(db,scope)
            rows=db.execute('SELECT * FROM companion_topics WHERE scope=? AND companion_id=? AND epoch=? '
                'AND expires_at>? ORDER BY is_current DESC,last_mentioned DESC LIMIT ?',
                (scope,companion,epoch,time.time(),limit)).fetchall()
            return epoch,[dict(r) for r in rows]

    def apply_topic(self,scope,companion,epoch,session,turn,update,ttl):
        """Apply only current user evidence; same turn retry cannot reactivate a topic."""
        stamp=time.time();event=session+':'+turn
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.ledger._epoch(db,scope)!=epoch:return False
            db.execute("UPDATE companion_topics SET status='expired',is_current=0,revision=revision+1 "
                "WHERE scope=? AND companion_id=? AND expires_at<=? AND status!='expired'",(scope,companion,stamp))
            rows=[dict(r) for r in db.execute('SELECT * FROM companion_topics WHERE scope=? AND companion_id=? AND epoch=? AND expires_at>?',
                (scope,companion,epoch,stamp))]
            if any(event in json.loads(r['seen_turns']) for r in rows):return True
            action=update['topic_action'];target=update['topic_id']
            row=next((r for r in rows if r['id']==target),None) if target else next((r for r in rows if r['is_current']),None)
            if target and row is None:return False
            if action=='none' and not update['preferences']:return True
            if action=='new' or row is None:
                identifier=hashlib.sha256(json.dumps([scope,companion,epoch,event]).encode()).hexdigest()
                row=dict(id=identifier,scope=scope,companion_id=companion,epoch=epoch,
                    topic=update['topic_label'] or '此刻的交流',pending_point=update['pending_point'],
                    user_preference='{}',source_refs='[]',session_id=session,source_turn=turn,
                    last_mentioned=stamp,status='pending',expires_at=stamp+ttl,revision=0,is_current=1,seen_turns='[]',
                    source_episode=None,episode_revision=None)
            if action in ('new','continue') or not rows:
                db.execute('UPDATE companion_topics SET is_current=0 WHERE scope=? AND companion_id=? AND is_current=1 AND id!=?',
                    (scope,companion,row['id']))
                row['is_current']=1
            if action=='continue':row['status']='pending'
            if action=='continue' and update['topic_label'] and update['evidence_quote']:
                row['topic']=update['topic_label']
            if action in ('decline','resolve'):
                row['status']='declined' if action=='decline' else 'resolved'
                row['pending_point']='';row['user_preference']='{}'
            if update['pending_point'] and action not in ('decline','resolve'):row['pending_point']=update['pending_point']
            preferences=json.loads(row['user_preference'])
            for p in update['preferences']:
                preferences[p['key']]=dict(value=p['value'],quote=p['quote'],source_ref=event+':user')
            row['user_preference']=json.dumps(preferences,ensure_ascii=False)
            refs=json.loads(row['source_refs'])
            if update['evidence_quote']:refs.append(dict(ref=event+':user',quote=update['evidence_quote']))
            refs.extend(dict(ref=event+':user',quote=p['quote']) for p in update['preferences'])
            row.update(source_refs=json.dumps(refs[-8:],ensure_ascii=False),source_turn=turn,session_id=session,
                last_mentioned=stamp,expires_at=stamp+ttl,revision=row['revision']+1,
                seen_turns=json.dumps((json.loads(row['seen_turns'])+[event])[-32:]))
            columns=list(row)
            db.execute('INSERT OR REPLACE INTO companion_topics('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(row[k] for k in columns))
            # Only small topical state survives. Prune old, non-current rows.
            db.execute('DELETE FROM companion_topics WHERE scope=? AND companion_id=? AND is_current=0 AND '
                '(expires_at<=? OR id NOT IN (SELECT id FROM companion_topics WHERE scope=? AND companion_id=? ORDER BY last_mentioned DESC LIMIT 64))',
                (scope,companion,stamp,scope,companion))
            return True

    def sync_episode_topics(self,scope,companion,epoch,records,ttl):
        """Only already retrieved unfinished Q3 episodes, with original revisions."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.ledger._epoch(db,scope)!=epoch:return
            for r in records:
                if not r.get('unfinished') or r.get('episode_status')!='active':continue
                identifier='episode:'+r['id']
                old=db.execute('SELECT revision,status,episode_revision FROM companion_topics WHERE id=? AND scope=? AND companion_id=?',
                    (identifier,scope,companion)).fetchone()
                if old and (old['status']=='declined' or old['episode_revision']==r['revision']):continue
                # Q3 is optional. Never manufacture its table just to sync a topic.
                valid=db.execute('SELECT revision FROM companion_episodes WHERE id=? AND scope=? AND companion_id=? AND epoch=? AND status=?',
                    (r['id'],scope,companion,epoch,'active')).fetchone()
                if not valid or valid[0]!=r['revision']:continue
                db.execute('INSERT OR REPLACE INTO companion_topics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identifier,scope,companion,epoch,r['topic'],r['event_summary'],'{}',
                     json.dumps([r['user_quote']],ensure_ascii=False),r['source_session'],r['source_turn'],time.time(),
                     'pending',time.time()+ttl,(old['revision']+1) if old else 1,0,'[]',r['id'],r['revision']))

    def topic_context(self,scope,companion,epoch,related_ids,maximum):
        with self.db() as db:
            db.execute('BEGIN')
            if self.ledger._epoch(db,scope)!=epoch:return None,None
            rows=[dict(r) for r in db.execute('SELECT * FROM companion_topics WHERE scope=? AND companion_id=? AND epoch=? '
                'AND expires_at>? ORDER BY is_current DESC,last_mentioned DESC LIMIT 64',(scope,companion,epoch,time.time()))]
            selected=[]
            for r in rows:
                if r['source_episode']:
                    source=db.execute('SELECT revision,status FROM companion_episodes WHERE id=? AND epoch=?',(r['source_episode'],epoch)).fetchone()
                    if not source or source['revision']!=r['episode_revision'] or source['status']!='active':continue
                if r['is_current'] or (r['source_episode'] in related_ids and r['status']=='pending'):
                    selected.append(r)
                if len(selected)>=maximum:break
            current=next((r['id'] for r in rows if r['is_current']),'')
            token=(epoch,current,tuple((r['id'],r['revision'],r['source_episode'],r['episode_revision'],r['is_current']) for r in selected))
            return selected,token

    def topic_context_current(self,scope,companion,token):
        epoch,current,refs=token
        with self.db() as db:
            if self.ledger._epoch(db,scope)!=epoch:return False
            active=db.execute('SELECT id FROM companion_topics WHERE scope=? AND companion_id=? AND epoch=? AND is_current=1 AND expires_at>?',
                (scope,companion,epoch,time.time())).fetchone()
            if (active[0] if active else '')!=current:return False
            for identifier,revision,source,source_revision,is_current in refs:
                row=db.execute('SELECT revision,expires_at,is_current FROM companion_topics WHERE id=? AND scope=? AND companion_id=? AND epoch=?',
                    (identifier,scope,companion,epoch)).fetchone()
                if not row or row['revision']!=revision or row['expires_at']<=time.time() or row['is_current']!=is_current:return False
                if source:
                    old=db.execute('SELECT revision,status FROM companion_episodes WHERE id=? AND epoch=?',(source,epoch)).fetchone()
                    if not old or old['revision']!=source_revision or old['status']!='active':return False
            return True


class InitiativeStore(CompanionStore):
    """Shared subject controls, short presence leases and consumed opportunities.

    A commit consumes an opportunity before audio enqueue. A crash may lose
    that utterance; a second terminal must not replay it as if never delivered.
    """
    def __init__(self,ledger):
        super().__init__(ledger,initialize_affect=False)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS companion_initiative_subjects(
                scope TEXT NOT NULL,companion_id TEXT NOT NULL,epoch INTEGER NOT NULL,
                user_seq INTEGER NOT NULL DEFAULT 0,last_user_at REAL NOT NULL DEFAULT 0,
                last_session TEXT NOT NULL DEFAULT '',last_event TEXT NOT NULL DEFAULT '',last_text TEXT NOT NULL DEFAULT '',
                last_route TEXT NOT NULL DEFAULT '',quiet_until REAL NOT NULL DEFAULT 0,quiet_quote TEXT NOT NULL DEFAULT '',
                last_attempt_at REAL NOT NULL DEFAULT 0,last_spoken_at REAL NOT NULL DEFAULT 0,last_delivered_seq INTEGER NOT NULL DEFAULT -1,
                PRIMARY KEY(scope,companion_id));
                CREATE TABLE IF NOT EXISTS companion_initiative_presence(
                scope TEXT NOT NULL,companion_id TEXT NOT NULL,session_id TEXT NOT NULL,epoch INTEGER NOT NULL,
                device_id TEXT NOT NULL,expires_at REAL NOT NULL,busy INTEGER NOT NULL,voice_until REAL NOT NULL,
                PRIMARY KEY(scope,companion_id,session_id));
                CREATE TABLE IF NOT EXISTS companion_initiative_candidates(
                id TEXT PRIMARY KEY,scope TEXT NOT NULL,companion_id TEXT NOT NULL,epoch INTEGER NOT NULL,user_seq INTEGER NOT NULL,
                owner TEXT NOT NULL,topic_key TEXT NOT NULL,source_kind TEXT NOT NULL,source_id TEXT NOT NULL,source_revision INTEGER NOT NULL,
                payload TEXT NOT NULL,created_at REAL NOT NULL,deadline REAL NOT NULL,status TEXT NOT NULL,
                opportunity_key TEXT DEFAULT NULL,
                turn_id TEXT NOT NULL DEFAULT '',reply_text TEXT NOT NULL DEFAULT '',completed_at REAL,
                UNIQUE(scope,companion_id,epoch,opportunity_key));
                CREATE INDEX IF NOT EXISTS initiative_recent ON companion_initiative_candidates(scope,companion_id,created_at);''')
            columns={r['name'] for r in db.execute('PRAGMA table_info(companion_initiative_candidates)')}
            if 'opportunity_key' not in columns:
                self._migrate_candidates(db)
            columns={r['name'] for r in db.execute('PRAGMA table_info(companion_initiative_subjects)')}
            for name,definition in (('gate_reason',"TEXT NOT NULL DEFAULT ''"),('gate_session',"TEXT NOT NULL DEFAULT ''"),('gate_at','REAL NOT NULL DEFAULT 0')):
                if name not in columns:db.execute('ALTER TABLE companion_initiative_subjects ADD COLUMN '+name+' '+definition)
            # Remain forget-safe after a code rollback: old binaries already
            # advance scopes.epoch but do not know the new derived tables.
            for table in ('companion_affect','companion_affect_events','companion_episodes',
                    'companion_episode_sources','companion_episode_jobs','companion_episode_audit',
                    'companion_topics','companion_initiative_subjects','companion_initiative_presence',
                    'companion_initiative_candidates'):
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                    db.executescript('CREATE TRIGGER IF NOT EXISTS forget_'+table+' AFTER UPDATE OF epoch ON scopes '
                        'WHEN NEW.epoch!=OLD.epoch BEGIN DELETE FROM '+table+' WHERE scope=NEW.scope; END;')

    @staticmethod
    def opportunity_key(material):
        basis=material.get('basis_key') or ':'.join(str(material[k]) for k in ('source_kind','source_id','source_revision'))
        return hashlib.sha256(str(basis).encode()).hexdigest()

    def _migrate_candidates(self,db):
        # Atomically remove the old per-user-input UNIQUE constraint. All
        # existing rows/IDs/statuses survive; factual/source tables are untouched.
        db.execute('BEGIN IMMEDIATE')
        db.execute('DROP TRIGGER IF EXISTS forget_companion_initiative_candidates')
        schema=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='companion_initiative_candidates'").fetchone()[0]
        import re
        schema=re.sub(r'CREATE TABLE(?: IF NOT EXISTS)?\s+companion_initiative_candidates',
            'CREATE TABLE companion_initiative_candidates_next',schema,count=1,flags=re.I)
        schema=re.sub(r'UNIQUE\s*\(\s*scope\s*,\s*companion_id\s*,\s*epoch\s*,\s*user_seq\s*\)',
            "opportunity_key TEXT DEFAULT NULL, UNIQUE(scope,companion_id,epoch,opportunity_key)",schema,flags=re.I)
        db.execute(schema)
        rows=[dict(r) for r in db.execute('SELECT * FROM companion_initiative_candidates ORDER BY created_at,id')];used=set()
        for row in rows:
            material=json.loads(row['payload']);key=self.opportunity_key(dict(row,**{k:v for k,v in material.items() if k=='basis_key'}))
            marker=(row['scope'],row['companion_id'],row['epoch'],key)
            if marker in used:key='legacy:'+row['id']
            used.add(marker);row['opportunity_key']=key
            db.execute('INSERT INTO companion_initiative_candidates_next('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        db.execute('DROP TABLE companion_initiative_candidates')
        db.execute('ALTER TABLE companion_initiative_candidates_next RENAME TO companion_initiative_candidates')
        db.execute('CREATE INDEX initiative_recent ON companion_initiative_candidates(scope,companion_id,created_at)')
        db.execute('CREATE TRIGGER forget_companion_initiative_candidates AFTER UPDATE OF epoch ON scopes WHEN NEW.epoch!=OLD.epoch BEGIN DELETE FROM companion_initiative_candidates WHERE scope=NEW.scope; END;')
        db.commit()

    def record_gate(self,scope,companion,session,epoch,reason):
        allowed={'asr_pending','user_voice','normal_generation','closing','routing','playback','playback_guard','audio_queue',
                 'not_armed','idle_wait','initiative_generation','quiet','cooldown','no_material','no_unused_opportunity',
                 'queued','failed','model_silent'}
        if reason not in allowed:raise ValueError('Unknown initiative gate')
        with self.db() as db:
            db.execute('UPDATE companion_initiative_subjects SET gate_reason=?,gate_session=?,gate_at=? WHERE scope=? AND companion_id=? AND epoch=? AND last_session=?',
                (reason,session,time.time(),scope,companion,epoch,session))

    def _subject(self,db,scope,companion):
        epoch=self.ledger._epoch(db,scope)
        old=db.execute('SELECT epoch FROM companion_initiative_subjects WHERE scope=? AND companion_id=?',(scope,companion)).fetchone()
        if old and old['epoch']!=epoch:
            # An older binary may have advanced the factual epoch without
            # knowing these optional tables. Never revive its stale sources.
            for table in ('companion_initiative_candidates','companion_initiative_presence','companion_initiative_subjects'):
                db.execute('DELETE FROM '+table+' WHERE scope=? AND companion_id=?',(scope,companion))
        db.execute('INSERT OR IGNORE INTO companion_initiative_subjects(scope,companion_id,epoch) VALUES(?,?,?)',(scope,companion,epoch))
        row=db.execute('SELECT * FROM companion_initiative_subjects WHERE scope=? AND companion_id=?',(scope,companion)).fetchone()
        return dict(row)

    def read_subject(self,scope,companion):
        with self.db() as db:return self._subject(db,scope,companion)

    def user_input(self,scope,companion,session,event,text,quiet=None,quiet_ttl=3600,stamp=None):
        now=time.time() if stamp is None else stamp
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');row=self._subject(db,scope,companion)
            if row['last_event']==event:return row
            until=row['quiet_until'];quote=row['quiet_quote']
            if quiet is True:until=now+quiet_ttl;quote=text[:500]
            elif quiet is False:until=0;quote=''
            db.execute('UPDATE companion_initiative_subjects SET user_seq=user_seq+1,last_user_at=?,last_session=?,last_event=?,last_text=?,last_route=?,quiet_until=?,quiet_quote=? WHERE scope=? AND companion_id=?',
                (now,session,event,text[:1000],'pending',until,quote,scope,companion))
            db.execute("UPDATE companion_initiative_candidates SET status='cancelled' WHERE scope=? AND companion_id=? AND status IN ('leased','committed')",(scope,companion))
            return dict(db.execute('SELECT * FROM companion_initiative_subjects WHERE scope=? AND companion_id=?',(scope,companion)).fetchone())

    def route_result(self,scope,companion,event,route):
        with self.db() as db:db.execute('UPDATE companion_initiative_subjects SET last_route=? WHERE scope=? AND companion_id=? AND last_event=?',(route,scope,companion,event))

    def presence(self,scope,companion,session,device,busy,voice_until,ttl,stamp=None):
        now=time.time() if stamp is None else stamp
        with self.db() as db:
            epoch=self.ledger._epoch(db,scope)
            db.execute('INSERT OR REPLACE INTO companion_initiative_presence VALUES(?,?,?,?,?,?,?,?)',
                (scope,companion,session,epoch,device,now+ttl,int(busy),voice_until))
            db.execute('DELETE FROM companion_initiative_presence WHERE scope=? AND companion_id=? AND expires_at<=?',(scope,companion,now))

    def _ready(self,db,row,session,now,delivery=False):
        own=db.execute('SELECT * FROM companion_initiative_presence WHERE scope=? AND companion_id=? AND session_id=? AND epoch=? AND expires_at>?',
            (row['scope'],row['companion_id'],session,row['epoch'],now)).fetchone()
        if not own or own['voice_until']>now or (own['busy'] and not delivery):return False
        return not db.execute('SELECT 1 FROM companion_initiative_presence WHERE scope=? AND companion_id=? AND epoch=? AND session_id!=? AND expires_at>? AND (busy=1 OR voice_until>?) LIMIT 1',
            (row['scope'],row['companion_id'],row['epoch'],session,now,now)).fetchone()

    def _source_current(self,db,candidate):
        kind=candidate['source_kind'];identifier=candidate['source_id'];revision=candidate['source_revision']
        if kind=='user':
            s=self._subject(db,candidate['scope'],candidate['companion_id'])
            return identifier==s['last_event'] and revision==s['user_seq']
        table='companion_topics' if kind=='topic' else 'companion_episodes' if kind=='episode' else None
        if not table or not db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",(table,)).fetchone():return False
        r=db.execute('SELECT * FROM '+table+' WHERE id=? AND scope=? AND companion_id=? AND epoch=?',
            (identifier,candidate['scope'],candidate['companion_id'],candidate['epoch'])).fetchone()
        return bool(r and r['revision']==revision and r['status'] in ('pending','active')
            and r['expires_at']>time.time())

    def claim(self,scope,companion,session,material,*,silence,cooldown,ttl,source_age,max_per_session,topic_cooldown,stamp=None):
        now=time.time() if stamp is None else stamp
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');s=self._subject(db,scope,companion)
            if (s['last_session']!=session or s['user_seq']<=0 or s['last_route'] not in ('chat','grounded')
                    or now-s['last_user_at']<silence or now-s['last_user_at']>source_age or s['quiet_until']>now
                    or (s['last_attempt_at'] and now-s['last_attempt_at']<cooldown)
                    or not self._ready(db,s,session,now)):return None
            db.execute("UPDATE companion_initiative_candidates SET status='expired' WHERE scope=? AND companion_id=? AND status IN ('leased','committed') AND deadline<=?",(scope,companion,now))
            key=self.opportunity_key(material)
            if db.execute('SELECT 1 FROM companion_initiative_candidates WHERE scope=? AND companion_id=? AND epoch=? AND (opportunity_key=? OR (source_kind=? AND source_id=? AND source_revision=?))',
                (scope,companion,s['epoch'],key,material['source_kind'],material['source_id'],material['source_revision'])).fetchone():return None
            used=db.execute("SELECT count(*) FROM companion_initiative_candidates WHERE scope=? AND companion_id=? AND owner=? AND turn_id!=''",
                (scope,companion,session)).fetchone()[0]
            if used>=max_per_session:return None
            if db.execute("SELECT 1 FROM companion_initiative_candidates WHERE scope=? AND companion_id=? AND topic_key=? AND created_at>? AND turn_id!='' LIMIT 1",
                (scope,companion,material['topic_key'],now-topic_cooldown)).fetchone():return None
            identifier=hashlib.sha256(json.dumps([scope,companion,s['epoch'],key]).encode()).hexdigest()
            material=dict(material,topic_id=material['topic_key'],expires_at=now+ttl,
                pause_elapsed_seconds=round(now-s['last_user_at'],1),no_new_user_message=True,
                source_refs=[dict(kind=material['source_kind'],id=material['source_id'],revision=material['source_revision'])])
            item=dict(id=identifier,scope=scope,companion_id=companion,epoch=s['epoch'],user_seq=s['user_seq'],owner=session,
                topic_key=material['topic_key'],source_kind=material['source_kind'],source_id=material['source_id'],source_revision=material['source_revision'],
                payload=json.dumps(material,ensure_ascii=False),created_at=now,deadline=now+ttl,status='leased',opportunity_key=key)
            if not self._source_current(db,item):return None
            db.execute('INSERT INTO companion_initiative_candidates('+','.join(item)+') VALUES('+','.join('?' for _ in item)+')',tuple(item.values()))
            db.execute('UPDATE companion_initiative_subjects SET last_attempt_at=? WHERE scope=? AND companion_id=?',(now,scope,companion))
            db.execute('DELETE FROM companion_initiative_candidates WHERE scope=? AND companion_id=? AND created_at<? AND status NOT IN (?,?)',
                (scope,companion,now-86400*7,'leased','committed'))
            item['material']=material;return item

    def current(self,ticket,*,delivery=False,stamp=None):
        now=time.time() if stamp is None else stamp
        with self.db() as db:
            s=self._subject(db,ticket['scope'],ticket['companion_id'])
            row=db.execute('SELECT * FROM companion_initiative_candidates WHERE id=?',(ticket['id'],)).fetchone()
            return bool(row and row['status']==('committed' if delivery else 'leased') and row['deadline']>now
                and s['epoch']==ticket['epoch'] and s['user_seq']==ticket['user_seq'] and s['last_session']==ticket['owner']
                and s['quiet_until']<=now and self._source_current(db,ticket) and self._ready(db,s,ticket['owner'],now,delivery))

    def commit(self,ticket,turn,text,stamp=None):
        now=time.time() if stamp is None else stamp
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');s=self._subject(db,ticket['scope'],ticket['companion_id'])
            row=db.execute('SELECT * FROM companion_initiative_candidates WHERE id=?',(ticket['id'],)).fetchone()
            if not (row and row['status']=='leased' and row['deadline']>now and s['epoch']==ticket['epoch']
                    and s['user_seq']==ticket['user_seq'] and s['last_session']==ticket['owner'] and s['quiet_until']<=now
                    and self._source_current(db,ticket) and self._ready(db,s,ticket['owner'],now)):return False
            db.execute("UPDATE companion_initiative_candidates SET status='committed',turn_id=?,reply_text=? WHERE id=?",(turn,text,ticket['id']))
            db.execute('UPDATE companion_initiative_subjects SET last_spoken_at=?,last_delivered_seq=? WHERE scope=? AND companion_id=?',
                (now,ticket['user_seq'],ticket['scope'],ticket['companion_id']))
            return True

    def settle(self,ticket,status,stamp=None):
        if status not in ('skipped','cancelled','failed','audio_sent','interrupted'):raise ValueError('Invalid initiative status')
        with self.db() as db:
            allowed=('leased','committed','audio_sent') if status=='interrupted' else ('leased','committed')
            db.execute('UPDATE companion_initiative_candidates SET status=?,completed_at=? WHERE id=? AND epoch=? AND status IN ('+','.join('?' for _ in allowed)+')',
                (status,time.time() if stamp is None else stamp,ticket['id'],ticket['epoch'],*allowed))

    def disconnect(self,scope,companion,session):
        with self.db() as db:
            db.execute('DELETE FROM companion_initiative_presence WHERE scope=? AND companion_id=? AND session_id=?',(scope,companion,session))
            db.execute("UPDATE companion_initiative_candidates SET status='cancelled' WHERE scope=? AND companion_id=? AND owner=? AND status='leased'",(scope,companion,session))
