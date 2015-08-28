import json
import re
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from multiprocessing import Pool
from multiprocessing.dummy import Pool as ThreadPool

import requests

from . import imap_utf7, parser, log
from .helpers import Timer, with_lock
from .imap import Client

# Only these folders contain unique emails
FOLDERS = ('\\All', '\\Spam', '\\Trash')
ALIASES = {
    'INBOX': '\\Inbox',
    '\\Junk': '\\Spam',
    '\\Starred': '\\Pinned'
}
THRID = 'mlr/thrid'


def locked_sync_gmail(env, email, *a, **kw):
    func = sync_gmail
    target = ':'.join([func.__name__, email])
    with with_lock(target, timeout=30):
        return Timer(target)(func)(env, email, *a, **kw)


def sync_gmail(env, email, bodies=False, only=None, labels=None):
    imap = Client(env, email)
    folders = imap.folders()
    if not only:
        only = FOLDERS

    labels_ = labels or {}
    for attrs, delim, name in folders:
        label = set(only) & set(ALIASES.get(l, l) for l in (attrs + (name,)))
        label = label and label.pop()
        if not label:
            continue

        imap.select(name, env('readonly'))
        if not labels:
            uids = imap.search(name)
            labels_[name] = get_msgids(env, imap, uids)
        else:
            imap.status(name)

        uids = labels_[name] or {}
        log.info('"%s" has %i messages', imap_utf7.decode(name), len(uids))
        if bodies:
            fetch_bodies(env, imap, uids)
        else:
            fetch_headers(env, imap, uids)
            fetch_labels(env, imap, uids, label, label in FOLDERS)
            if label in FOLDERS:
                sync_marks(env, imap, uids)
                update_thrids(env, label)
    return labels_


def search(env, email, query):
    imap = Client(env, email)
    folder = [n for a, d, n in imap.folders() if '\\All' in a][0]
    imap.select(folder, True)
    _, data = imap.uid('SEARCH', None, 'X-GM-RAW %s' % json.dumps(query))
    if not data[0]:
        return []

    uids = data[0].decode().split(' ')
    mids = get_msgids(env, imap, uids)
    ids = get_ids(env, list(mids.values()))
    return list(ids.values())


def get_msgids(env, imap, uids):
    if not uids:
        return None

    q = 'BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]'
    data = imap.fetch(uids, [q])
    uids = OrderedDict(
        (k, parser.parse(env, v[q])['message-id']) for k, v in data
    )
    return uids


def get_gids(env, gids, where=None):
    sql = 'SELECT msgid FROM emails WHERE msgid = ANY(%(gids)s)'
    if where:
        sql += ' AND %s' % where

    return [r[0] for r in env.sql(sql, {'gids': list(gids)})]


def get_ids(env, msgids):
    sql = 'SELECT msgid, id FROM emails WHERE msgid = ANY(%(msgids)s)'
    return dict(r for r in env.sql(sql, {'msgids': list(msgids)}))


def get_parsed(env, data, msgid=None):
    def format_addr(v):
        if not v[0]:
            v = (v[1].split('@')[0], v[1])
        return '"{}" <{}>'.format(*v)

    def clean(key, value):
        if not value:
            return value
        elif key in ('to', 'fr', 'cc', 'bcc', 'reply_to', 'sender'):
            return [format_addr(v) for v in value]
        elif key in ('msgid', 'in_reply_to'):
            return value.strip()
        elif key in ('attachments',):
            return json.dumps(value)
        elif key in ('refs',):
            return ['<%s>' % v for v in re.split('[<>\s]+', value) if v]
        else:
            return value

    pairs = (
        ('subject', 'subj'),
        ('from', 'fr'),
        ('to', 'to'),
        ('cc', 'cc'),
        ('bcc', 'bcc'),
        ('reply-to', 'reply_to'),
        ('sender', 'sender'),
        ('date', 'sender_time'),
        ('message-id', 'msgid'),
        ('in-reply-to', 'in_reply_to'),
        ('references', 'refs'),
        ('html', 'html'),
        ('text', 'text'),
        ('attachments', 'attachments'),
        ('embedded', 'embedded'),
    )
    msg = parser.parse(env, data, msgid)
    return ((field, clean(field, msg[key])) for key, field in pairs)


def fetch_headers(env, imap, map_uids):
    gids = get_gids(env, map_uids.values())
    uids = [uid for uid, gid in map_uids.items() if gid not in gids]
    if not uids:
        log.info('  * No headers to fetch')
        return

    q = ['INTERNALDATE', 'RFC822.SIZE', 'RFC822.HEADER', 'X-GM-MSGID']
    for data in imap.fetch_batch(uids, q, 'add emails with headers'):
        emails = []
        for uid, row in data:
            gm_uid = '%s\r%s' % (imap.email, row['X-GM-MSGID'])
            fields = {
                'id': uuid.uuid5(uuid.NAMESPACE_URL, gm_uid),
                'header': row['RFC822.HEADER'],
                'size': row['RFC822.SIZE'],
                'time': row['INTERNALDATE'],
                'extra': {'X-GM-MSGID': row['X-GM-MSGID']},
            }
            fields.update(get_parsed(env, fields['header'], str(fields['id'])))
            emails.append(fields)
        env.emails.insert(emails)
        env.db.commit()


@contextmanager
def async_runner(count=0, threads=True):
    if count:
        pool = ThreadPool(count) if threads else Pool(count)
        results = []

        def run(func, *a, **kw):
            results.append(pool.apply_async(func, a, kw))

        yield run

        pool.close()
        pool.join()

        [r.get() for r in results]
    else:
        def run(func, *a, **kw):
            func(*a, **kw)

        yield run


def fetch_bodies(env, imap, map_uids):
    i = env.sql('''
    SELECT msgid, size FROM emails
    WHERE msgid = ANY(%(ids)s) AND raw IS NULL
    ''', {'ids': list(map_uids.values())})
    pairs = dict(i)

    uids = [(uid, pairs[mid]) for uid, mid in map_uids.items() if mid in pairs]
    if not uids:
        log.info('  * No bodies to fetch')
        return

    results = []

    def update(env, items):
        map_ids = get_ids(env, [v[1] for v in items])

        ids = []
        for data, msgid in items:
            data_ = dict(get_parsed(env, data, map_ids[msgid]), raw=data)
            ids += env.emails.update(data_, 'msgid=%s', [msgid])
        env.db.commit()
        notify(env, ids)
        results.append(len(ids))

    q = 'BODY.PEEK[]'
    with async_runner(env('async_pool')) as run:
        for data in imap.fetch_batch(uids, q, 'add bodies'):
            items = [(row[q], map_uids[uid]) for uid, row in data]
            run(update, env, items)

    log.info('  * Done %s bodies', sum(results))
    if results:
        refresh_search(env)


def refresh_search(env):
    log.info('Refresh search index')
    env.sql('REFRESH MATERIALIZED VIEW emails_search')
    env.db.commit()


def fetch_labels(env, imap, map_uids, folder, clean=True):
    updated, glabels = [], set()

    gids = get_gids(env, map_uids.values())
    updated += update_label(env, gids, folder)
    if folder not in FOLDERS:
        updated += update_label(env, gids, '\\All', folder)

    uids = [uid for uid, gid in map_uids.items() if gid in gids]
    if uids:
        data = tuple(imap.fetch(uids, 'X-GM-LABELS FLAGS'))
        glabels, gflags = set(), set()
        for _, row in data:
            glabels |= set(row['X-GM-LABELS'])
            gflags |= set(row['FLAGS'])
        log.info('  * Unique labels %r', glabels)
        log.info('  * Unique flags %r', gflags)

        labels = [
            (imap_utf7.decode(l), [l], lambda row, l: l in row['X-GM-LABELS'])
            for l in glabels
        ] + [
            ('\\Answered', [], (lambda row: '\\Answered' in row['FLAGS'])),
            ('\\Unread', [], (lambda row: '\\Seen' not in row['FLAGS'])),
        ]
        for label, args, func in labels:
            gids = [map_uids[uid] for uid, row in data if func(row, *args)]
            label = ALIASES.get(label, label)
            updated += update_label(env, gids, label, folder)

    if clean:
        glabels_ = {ALIASES.get(l, l) for l in glabels}
        updated += clean_emails(env, glabels_, folder)
    updated += process_tasks(env)

    env.db.commit()
    notify(env, updated)


def clean_emails(env, labels, folder):
    labels |= {'\\Answered', '\\Unread', folder}
    labels = [imap_utf7.decode(l) for l in labels]

    # Sorted array intersection
    new_labels = env.mogrify('''
    SELECT ARRAY(
      SELECT i FROM (
        SELECT unnest(labels)
        INTERSECT
        SELECT unnest(%s)
      ) AS dt(i)
      ORDER BY 1
    )
    ''', [labels])
    sql = '''
    UPDATE emails SET labels=({0}), thrid=NULL
    WHERE (SELECT ARRAY(SELECT unnest(labels) ORDER BY 1)) != ({0})
    AND %s = ANY(labels)
    RETURNING id
    '''.format(new_labels)
    i = env.sql(sql, [folder])
    log.info('  * Clean %d emails', i.rowcount)
    return tuple(r[0] for r in i)


def process_tasks(env):
    updated = []
    tasks = env.sql('''
    SELECT value FROM storage
    WHERE key LIKE 'task:mark:%'
    ORDER BY created
    ''').fetchall()
    log.info('  * Process %s tasks', len(tasks))
    for row in tasks:
        data = row[0]
        updated += mark(env, data['action'], data['name'], data['ids'])
        log.info('  - done %s', data)
    return updated


def mark(env, action, name, ids, new=False, inner=False):
    if not name or not ids:
        return []
    if not isinstance(name, str):
        return sum((mark(env, action, n, ids, new, inner) for n in name), [])

    ids = tuple(ids)
    actions = {
        '-': (
            '''
            UPDATE emails SET labels = array_remove(labels, %(name)s)
            WHERE id IN %(ids)s AND %(name)s=ANY(labels)
            RETURNING id
            '''
        ),
        '+': (
            '''
            UPDATE emails SET labels = (labels || %(name)s::varchar)
            WHERE id IN %(ids)s AND NOT(%(name)s=ANY(labels))
            RETURNING id
            '''
        ),
    }
    clean = {
        ('+', '\\Trash'): [('-', ['\\All', '\\Inbox', '\\Spam'])],
        ('+', '\\Spam'): [('-', ['\\All', '\\Inbox', '\\Trash'])],
        ('+', '\\Inbox'): [
            ('-', ['\\Trash', '\\Spam']),
            ('+', '\\All')
        ],
        ('-', THRID): [clean_thrid]
    }
    instead = {
        ('-', '\\Trash'): ('+', '\\Inbox'),
        ('-', '\\Spam'): ('+', '\\Inbox'),
    }

    if not inner:
        action, name = instead.get((action, name), (action, name))
        extra = clean.get((action, name), [])
        for row in extra:
            if callable(row):
                row(env, ids)
                continue
            mark(env, *row, ids=ids, inner=True)

    i = env.sql(actions[action], {'name': name, 'ids': ids})
    updated = [r[0] for r in i]
    if new:
        env.emails.update({'thrid': None}, 'id IN %s', [ids])
        updated += update_thrids(env, commit=False)

        env.add_tasks([{'action': action, 'name': name, 'ids': ids}])
        env.db.commit()
        notify(env, updated)
    return updated


def sync_marks(env, imap, map_uids):
    if not map_uids:
        return

    log.info('  * Sync marks')
    store = {
        ('+', '\\Unread'): ('-FLAGS', '\\Seen'),
        ('-', '\\Unread'): ('+FLAGS', '\\Seen'),
        '\\Pinned': ('FLAGS', '\\Flagged'),
        '\\Spam': ('X-GM-LABELS', '\\Spam'),
    }
    tasks = env.sql('''
    SELECT key, value FROM storage
    WHERE key LIKE 'task:mark:%'
    ORDER BY created
    ''').fetchall()
    msgids = tuple(map_uids.values())
    for task_id, t in tasks:
        emails = env.sql('''
        SELECT id, msgid FROM emails WHERE msgid IN %s AND id IN %s
        ''', [msgids, tuple(t['ids'])]).fetchall()
        msgids_ = [r['msgid'] for r in emails]
        uids = [uid for uid, gid in map_uids.items() if gid in msgids_]
        if not uids:
            return

        default = ('X-GM-LABELS', t['name'])
        key, value = store.get(t['name'], default)
        key = t['action'] + key
        key, value = store.get((t['action'], t['name']), (key, value))
        value = [value] if isinstance(value, str) else value
        value = (imap_utf7.encode(v) for v in value)
        value = (
            '"%s"' % v.replace('\\', '\\\\').replace('"', '\\"')
            for v in value
        )
        value = ' '.join(value)
        log.info('  - store (%s %s) for %s ones', key, value, len(uids))
        try:
            imap.uid('STORE', ','.join(uids), key, value)
        except imap.Error as e:
            log.warn('  ! %r', e)
            return

        env.sql('DELETE FROM storage WHERE key = %s', [task_id])


def notify(env, ids):
    if not ids:
        return

    url = 'http://localhost:9000/notify/'
    d = {'ids': set(ids)}
    try:
        requests.post(url, data=d, timeout=5, auth=(env.username, env.token))
    except IOError as e:
        log.error(e)


def update_label(env, gids, label, folder=None):
    def step(action, sql):
        t = Timer()
        sql += (
            ('  AND %(folder)s = ANY(labels)' if folder else '') +
            'RETURNING id'
        )
        i = env.sql(sql, {'label': label, 'gids': gids, 'folder': folder})
        log.info('  - %s %d emails for %.2fs', action, i.rowcount, t.time())
        step.ids += tuple(r[0] for r in i)
    step.ids = ()

    log.info('  * Process %r...', label)
    step('remove from', '''
    UPDATE emails SET thrid=NULL, labels=array_remove(labels, %(label)s)
    WHERE NOT (msgid = ANY(%(gids)s)) AND %(label)s = ANY(labels)
    ''')

    step('add to', '''
    UPDATE emails SET thrid=NULL, labels=(labels || %(label)s::varchar)
    WHERE msgid = ANY(%(gids)s) AND NOT (%(label)s = ANY(labels))
    ''')
    return step.ids


def update_thrids(env, folder=None, manual=True, commit=True):
    updated = []

    def step(label, sql, args=None, log_ids=False):
        ids = tuple(r[0] for r in env.sql(sql, args))
        log.info('  - for %s emails (%s)', len(ids), label)

        if log_ids and ids:
            log.info('  - ids: %s', ids)

        updated.extend(ids)
        return ids

    if not folder:
        for label in FOLDERS:
            updated += update_thrids(env, label, manual, commit)

        step('clean deleted: thrid=null and labels={}', '''
        UPDATE emails set thrid = NULL, labels='{}'
        WHERE NOT (labels && %s::varchar[]) AND thrid != id AND labels != '{}'
        RETURNING id
        ''', [list(FOLDERS)])
        return updated

    log.info('  * Update thread ids %r', folder)

    step('Clean thrid from other folders', '''
    UPDATE emails SET thrid = NULL
    WHERE %(folder)s = ANY(labels) AND thrid IS NOT NULL
      AND thrid NOT IN (
        SELECT thrid FROM emails WHERE %(folder)s = ANY(labels)
      )
    RETURNING id
    ''', {'folder': folder})

    step('no "in_reply_to" and no "references"', '''
    UPDATE emails SET thrid = id
    WHERE %s = ANY(labels) AND thrid IS NULL
      AND (in_reply_to IS NULL OR in_reply_to != ALL(SELECT msgid FROM emails))
      AND (refs IS NULL OR NOT (refs && (SELECT array_agg(msgid) FROM emails)))
    RETURNING id
    ''', [folder])

    step('flat query by "in_reply_to"', '''
    UPDATE emails e SET thrid=t.thrid
      FROM emails t
      WHERE e.in_reply_to = t.msgid
        AND %(folder)s = ANY(e.labels) AND %(folder)s = ANY(t.labels)
        AND e.thrid IS NULL AND t.thrid IS NOT NULL
      RETURNING e.id;
    ''', {'folder': folder})

    step('flat query by "references"', '''
    UPDATE emails e SET thrid=t.thrid
      FROM emails t
      WHERE t.msgid = ANY(e.refs)
        AND %(folder)s = ANY(e.labels) AND %(folder)s = ANY(t.labels)
        AND e.thrid IS NULL AND t.thrid IS NOT NULL
      RETURNING e.id;
    ''', {'folder': folder})

    step('reqursive query by "in_reply_to"', '''
    WITH RECURSIVE thrids(id, msgid, thrid, labels) AS (
      SELECT id, msgid, thrid, labels
      FROM emails WHERE thrid IS NOT NULL
    UNION
      SELECT e.id, e.msgid, t.thrid, e.labels
      FROM emails e, thrids t
      WHERE e.in_reply_to = t.msgid
        AND %(folder)s = ANY(e.labels) AND %(folder)s = ANY(t.labels)
        AND e.thrid IS NULL AND t.thrid IS NOT NULL
    )
    UPDATE emails e SET thrid=t.thrid
    FROM thrids t WHERE e.id = t.id AND e.thrid IS NULL
    RETURNING e.id
    ''', {'folder': folder})

    step('reqursive query by "references"', '''
    WITH RECURSIVE thrids(id, msgid, thrid, labels) AS (
      SELECT id, msgid, thrid, labels
      FROM emails WHERE thrid IS NOT NULL
    UNION
      SELECT e.id, e.msgid, t.thrid, e.labels
      FROM emails e, thrids t
      WHERE t.msgid = ANY(e.refs)
        AND %(folder)s = ANY(e.labels) AND %(folder)s = ANY(t.labels)
        AND e.thrid IS NULL AND t.thrid IS NOT NULL
    )
    UPDATE emails e SET thrid=t.thrid
    FROM thrids t WHERE e.id = t.id AND e.thrid IS NULL
    RETURNING e.id
    ''', {'folder': folder})

    step('other: thrid=id', '''
    UPDATE emails SET thrid = id
    WHERE thrid IS NULL AND %s = ANY(labels)
    RETURNING id
    ''', [folder], log_ids=True)

    updated += failed_delivery(env, folder)
    if manual:
        updated += manual_threads(env, folder)

    if commit:
        env.db.commit()
        notify(env, updated)
    return updated


def failed_delivery(env, folder):
    emails = env.sql('''
    SELECT id, text FROM emails
    WHERE fr[1] LIKE '%%<mailer-daemon@googlemail.com>' AND %s = ANY(labels)
    ORDER BY time
    ''', [folder])
    ids = []
    for msg in emails:
        msgid = re.search('(?m)^Message-ID:(.*)$', msg['text'])
        if not msgid:
            continue

        msgid = msgid.group(1).strip()
        thrid = env.sql('''
        SELECT thrid FROM emails WHERE msgid=%s
        ''', [msgid]).fetchall()
        if not thrid:
            continue

        i = env.sql('''
        UPDATE emails SET thrid=%(thrid)s
        WHERE (id=%(id)s OR thrid=%(id)s) AND thrid!=%(thrid)s
        RETURNING id
        ''', {'thrid': thrid[0][0], 'id': msg['id']})
        ids += [r['id'] for r in i]

    log.info('  - merge threads by failed delivery: %s', ids)
    return ids


def clean_thrid(env, ids):
    i = env.sql('''
    SELECT unnest(labels), array_agg(id)::text[] FROM emails
    WHERE id = ANY(%s::uuid[])
    GROUP BY 1
    ''', [list(ids)])

    for row in i:
        label = row[0]
        if not label.startswith('%s/' % THRID):
            continue
        mark(env, '-', label, row[1], new=True)


def mark_thread(env, thrid, ids):
    clean_thrid(env, ids)
    mark(env, '+', [THRID, '%s/%s' % (THRID, thrid)], ids, new=True)


def new_thread(env, id):
    thrid = env.sql('''
    SELECT thrid FROM emails WHERE id=%s LIMIT 1
    ''', [id]).fetchone()[0]

    env.sql('''
    UPDATE emails SET thrid = NULL WHERE thrid = %(thrid)s;
    UPDATE emails SET thrid = id WHERE id = %(id)s;
    ''', {'thrid': thrid, 'id': id})
    update_thrids(env, manual=False, commit=False)

    i = env.sql('SELECT id FROM emails WHERE thrid=%s', [id])
    ids = [r[0] for r in i]
    mark_thread(env, id, ids)


def merge_threads(env, ids):
    thrid = env.sql('''
    SELECT thrid FROM emails WHERE thrid = ANY(%s::uuid[])
    ORDER BY time LIMIT 1
    ''', [ids]).fetchone()[0]

    i = env.sql('SELECT id FROM emails WHERE thrid = ANY(%s::uuid[])', [ids])
    ids = [r[0] for r in i]

    mark_thread(env, thrid, ids)
    return thrid


def manual_threads(env, folder):
    i = env.sql(r'''
    WITH il(id, label) as (
        SELECT id, unnest(labels) FROM emails
        WHERE %s = ANY(labels)
    )
    SELECT label, array_agg(id) ids FROM il
    WHERE label LIKE '{}/%%'
    GROUP BY label
    '''.format(re.escape(THRID)), [folder])
    updated = []
    for row in i:
        thrid = row['label'].replace('%s/' % THRID, '')
        i = env.sql('''
        UPDATE emails SET thrid=%(thrid)s
        WHERE id = ANY(%(ids)s) AND thrid!=%(thrid)s
        RETURNING id
        ''', {'thrid': thrid, 'ids': row['ids']})
        updated += [r[0] for r in i]

    log.info('  - update %s emails with manual threads', len(updated))
    return updated
