"""Streaming Git history indexing and SQL-backed metrics. No repository code is run."""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from collections import OrderedDict
from pathlib import Path

EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
FORMAT = '%x00RAT_COMMIT%x00%H%x00%P%x00%an%x00%ae%x00%ct%x00%s%x00'
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
 canonical_id INTEGER, UNIQUE(name,email));
CREATE TABLE commits(id INTEGER PRIMARY KEY, hash TEXT UNIQUE, parent TEXT,
 author_id INTEGER, timestamp INTEGER, subject TEXT);
CREATE TABLE paths(id INTEGER PRIMARY KEY, path TEXT UNIQUE);
CREATE TABLE binary_commits(commit_id INTEGER PRIMARY KEY);
CREATE TABLE inventory(path TEXT PRIMARY KEY);
CREATE TABLE changes(commit_id INTEGER, path_id INTEGER, old_path TEXT,
 added INTEGER NOT NULL, removed INTEGER NOT NULL, PRIMARY KEY(commit_id,path_id));
CREATE INDEX changes_path ON changes(path_id,commit_id);
CREATE INDEX commits_date ON commits(timestamp);
CREATE INDEX commits_author ON commits(author_id);
CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
"""


def environment():
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', LC_ALL='C.UTF-8')
    return env


def command(repo, *args):
    return ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.pager=cat',
            '-c', 'core.quotePath=false', '-c', 'diff.renames=true',
            '-c', 'diff.renameLimit=0', '-c', 'protocol.file.allow=never',
            '-c', 'protocol.ext.allow=never', '-C', str(repo), *args]


def git(repo, *args, input=None, timeout=180):
    result = subprocess.run(command(repo, *args), input=input, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=environment(), timeout=timeout)
    if result.returncode:
        error = result.stderr.decode('utf-8', 'replace').strip()
        raise ValueError(error[-1800:] or 'Git could not read this repository.')
    return result.stdout


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback a context and release its file descriptors immediately."""
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(database):
    db = sqlite3.connect(database, timeout=60, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA temp_store=MEMORY')
    return db


def tokens(stream):
    """Yield NUL-delimited records without retaining the history in memory."""
    pending = b''
    while chunk := stream.read(65536):
        chunks = (pending + chunk).split(b'\0')
        pending = chunks.pop()
        yield from chunks
    if pending:
        yield pending


def numstat(token, iterator):
    fields = token.lstrip(b'\n').split(b'\t', 2)
    if len(fields) != 3:
        raise ValueError('Unexpected Git numstat record.')
    added, removed, path = fields
    old_path = None
    if not path:
        old_path = next(iterator).decode('utf-8', 'replace')
        path = next(iterator)
    path = path.decode('utf-8', 'replace')
    if added == b'-' or removed == b'-':
        return None
    return path, old_path, int(added), int(removed)


def index_repository(repo, database, reference='HEAD', progress=lambda *_: None):
    """One streaming Git traversal; merge commits never enter the indexed set."""
    if not reference or reference.startswith('-') or len(reference) > 240:
        raise ValueError('Enter a valid reference, branch, tag, or full commit hash.')
    try:
        head = git(repo, 'rev-parse', '--verify', '--end-of-options', reference + '^{commit}').decode().strip()
    except ValueError:
        if reference == 'HEAD' and not git(repo, 'for-each-ref').strip():
            with connect(database) as db:
                db.executescript(SCHEMA)
                db.execute('INSERT INTO settings VALUES (?,?)', ('head', ''))
                db.execute('INSERT INTO settings VALUES (?,?)', ('count', '0'))
            return '', 0
        raise ValueError('Reference not found. Check the branch, tag, or commit hash.')
    if git(repo, 'rev-parse', '--is-shallow-repository').strip() == b'true':
        raise ValueError('This repository is shallow. Fetch its complete history before uploading.')
    count = int(git(repo, 'rev-list', '--count', '--no-merges', head).strip())
    progress(12, 'Reading non-merge commits')
    db = connect(database)
    db.executescript(SCHEMA)
    authors, paths = {}, {}
    pending = []
    proc = subprocess.Popen(command(repo, 'log', '--no-merges', '--reverse', '--topo-order',
                                    '--format=' + FORMAT, '--numstat', '-z', '--find-renames=50%',
                                    '--no-ext-diff', '--no-textconv', head, '--'),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment())
    commit_id = 0
    try:
        stream = iter(tokens(proc.stdout))
        for token in stream:
            if not token.strip(b'\n'):
                continue
            if token.lstrip(b'\n') == b'RAT_COMMIT':
                sha, parents, name, email, timestamp, subject = [next(stream).decode('utf-8', 'replace') for _ in range(6)]
                commit_id += 1
                key = name, email
                if key not in authors:
                    aid = len(authors) + 1
                    authors[key] = aid
                    db.execute('INSERT INTO authors VALUES (?,?,?,?)', (aid, name, email, aid))
                db.execute('INSERT INTO commits VALUES (?,?,?,?,?,?)',
                           (commit_id, sha, parents.split()[0] if parents else '', authors[key], int(timestamp), subject))
                if commit_id % 1000 == 0:
                    db.executemany('INSERT INTO changes VALUES (?,?,?,?,?)', pending)
                    pending.clear()
                    db.commit()
                    progress(12 + int(80 * commit_id / max(count, 1)), f'Indexed {commit_id:,} / {count:,} commits')
            else:
                change = numstat(token, stream)
                if change is None:
                    db.execute('INSERT OR IGNORE INTO binary_commits VALUES (?)', (commit_id,))
                    continue
                path, old_path, added, removed = change
                for candidate in (path, old_path):
                    if candidate is not None and candidate not in paths:
                        pid = len(paths) + 1
                        paths[candidate] = pid
                        db.execute('INSERT INTO paths VALUES (?,?)', (pid, candidate))
                pending.append((commit_id, paths[path], old_path, added, removed))
        stderr = proc.stderr.read().decode('utf-8', 'replace')
        if proc.wait() != 0:
            raise ValueError(stderr[-1800:] or 'Git history traversal failed.')
        db.executemany('INSERT INTO changes VALUES (?,?,?,?,?)', pending)
        progress(94, 'Applying .mailmap author identities')
        apply_mailmap(repo, head, db)
        progress(96, 'Resolving text-file inventory across merge and binary boundaries')
        all_commits = db.execute('SELECT * FROM commits').fetchall()
        db.execute('CREATE TEMP TABLE selected(id INTEGER PRIMARY KEY)')
        db.executemany('INSERT INTO selected VALUES (?)', ((r['id'],) for r in all_commits))
        inventory = object_paths(db, repo, all_commits, -1)
        db.executemany('INSERT INTO inventory VALUES (?)', ((p,) for p in inventory))
        db.executemany('INSERT INTO settings VALUES (?,?)', [('head', head), ('count', str(count))])
        db.commit()
        db.execute('PRAGMA optimize')
        return head, count
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()
        db.close()


def apply_mailmap(repo, head, db):
    rows = db.execute('SELECT * FROM authors ORDER BY id').fetchall()
    if not rows:
        return
    contacts = ''.join(f"{r['name']} <{r['email']}>\n" for r in rows).encode()
    mapped = git(repo, '-c', 'mailmap.file=/dev/null', '-c', f'mailmap.blob={head}:.mailmap',
                 'check-mailmap', '--stdin', input=contacts).decode('utf-8', 'replace').splitlines()
    canonical = {}
    for row, contact in zip(rows, mapped):
        match = re.fullmatch(r'(.*) <([^<>]*)>', contact)
        if not match:
            continue
        name, email = match.groups()
        key = name, email
        if key not in canonical:
            existing = db.execute('SELECT id FROM authors WHERE name=? AND email=?', key).fetchone()
            if existing:
                canonical[key] = existing['id']
            else:
                cursor = db.execute('INSERT INTO authors(name,email) VALUES (?,?)', key)
                canonical[key] = cursor.lastrowid
                db.execute('UPDATE authors SET canonical_id=id WHERE id=?', (cursor.lastrowid,))
        db.execute('UPDATE authors SET canonical_id=? WHERE id=?', (canonical[key], row['id']))
    # Resolve chains if a canonical identity was itself remapped later.
    mapping = {r['id']: r['canonical_id'] for r in db.execute('SELECT * FROM authors')}
    for aid in mapping:
        target, seen = aid, set()
        while mapping[target] != target and target not in seen:
            seen.add(target)
            target = mapping[target]
        db.execute('UPDATE authors SET canonical_id=? WHERE id=?', (target, aid))


def author_list(db):
    result = {}
    for row in db.execute('''SELECT a.*,c.name AS canonical_name,c.email AS canonical_email
                            FROM authors a JOIN authors c ON c.id=a.canonical_id ORDER BY c.name,a.name'''):
        target = row['canonical_id']
        result.setdefault(target, {'id': target, 'name': row['canonical_name'],
                                  'email': row['canonical_email'], 'aliases': []})
        result[target]['aliases'].append({'name': row['name'], 'email': row['email']})
    return list(result.values())


def directories(path):
    yield ''
    parts = path.split('/')[:-1]
    for i in range(1, len(parts) + 1):
        yield '/'.join(parts[:i])


def normalize_filters(args):
    result = {'path': args.get('path', ''), 'kind': args.get('kind', 'directory')}
    if result['kind'] not in ('file', 'directory'):
        raise ValueError('Path kind must be file or directory.')
    if result['kind'] == 'directory':
        result['path'] = result['path'].rstrip('/')
    for key in ('since', 'until', 'author'):
        value = args.get(key)
        if value not in (None, ''):
            try:
                result[key] = int(value)
            except (ValueError, TypeError):
                raise ValueError(f'{key} must be an integer.')
            if abs(result[key]) > 253402300799:
                raise ValueError(f'{key} is outside the supported range.')
    if 'since' in result and 'until' in result and result['since'] >= result['until']:
        raise ValueError('Start must be earlier than the exclusive end date.')
    raw = args.get('commits')
    if raw is not None:
        selected = list(dict.fromkeys(filter(None, raw.split(','))))
        if len(selected) > 10000 or any(not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', c) for c in selected):
            raise ValueError('Select at most 10,000 complete commit hashes.')
        result['commits'] = selected
    return result


def select_commits(db, filters, include_author=True):
    where, values = [], []
    if 'since' in filters:
        where.append('c.timestamp>=?')
        values.append(filters['since'])
    if 'until' in filters:
        where.append('c.timestamp<?')
        values.append(filters['until'])
    if include_author and 'author' in filters:
        where.append('a.canonical_id=?')
        values.append(filters['author'])
    if 'commits' in filters:
        db.execute('CREATE TEMP TABLE IF NOT EXISTS requested(hash TEXT PRIMARY KEY)')
        db.execute('DELETE FROM requested')
        db.executemany('INSERT INTO requested VALUES (?)', ((c,) for c in filters['commits']))
        where.append('c.hash IN (SELECT hash FROM requested)')
    condition = ' AND '.join(where) or '1'
    return f'SELECT c.*,a.canonical_id FROM commits c JOIN authors a ON a.id=c.author_id WHERE {condition}', values


def in_scope(path, filters):
    scope = filters.get('path', '')
    return path == scope if filters.get('kind') == 'file' else not scope or path.startswith(scope + '/')


def scope_sql(filters, prefix='p'):
    path = filters.get('path', '')
    if filters.get('kind') == 'file':
        return f'{prefix}.path=?', [path]
    if path:
        return f'substr({prefix}.path,1,?)=?', [len(path) + 1, path + '/']
    return '1', []


_inventory_cache = OrderedDict()
_classification_cache = OrderedDict()
_inventory_lock = threading.Lock()


def extend_snapshot_paths(repo, sha, paths):
    """Extend a union using tree metadata first, diffing only unclassified blobs.

    Known union members need no blob reads. This is important for merge-heavy
    histories: repeatedly diffing every complete tree would rescan all its lines.
    """
    key = str(repo), sha
    initially_empty = not paths
    with _inventory_lock:
        cached = _inventory_cache.get(key)
        if cached is not None:
            _inventory_cache.move_to_end(key)
            paths.update(cached)
            return
    output = git(repo, 'ls-tree', '-r', '-z', sha)
    unknown = []
    for record in output.split(b'\0'):
        if not record:
            continue
        metadata, raw_path = record.split(b'\t', 1)
        path = raw_path.decode('utf-8', 'replace')
        if path in paths:
            continue
        object_id = metadata.split()[2].decode()
        cache_key = str(repo), path, object_id
        with _inventory_lock:
            known = _classification_cache.get(cache_key)
        if known is True:
            paths.add(path)
        elif known is None:
            unknown.append((path, cache_key))
    for offset in range(0, len(unknown), 128):
        batch = unknown[offset:offset + 128]
        output = git(repo, 'diff', '--numstat', '-z', '--no-renames', '--no-ext-diff',
                     '--no-textconv', EMPTY_TREE, sha, '--', *(':(literal)' + p for p, _ in batch))
        stream = iter(output.split(b'\0'))
        text_paths = set()
        for token in stream:
            if token:
                change = numstat(token, stream)
                if change:
                    text_paths.add(change[0])
        paths.update(text_paths)
        with _inventory_lock:
            for path, cache_key in batch:
                _classification_cache[cache_key] = path in text_paths
            while len(_classification_cache) > 50000:
                _classification_cache.popitem(last=False)
    if initially_empty:
        with _inventory_lock:
            _inventory_cache[key] = paths.copy()
            while len(_inventory_cache) > 48:
                _inventory_cache.popitem(last=False)


def snapshot_paths(repo, sha):
    paths = set()
    extend_snapshot_paths(repo, sha, paths)
    return paths


def object_paths(db, repo, selected, total):
    if not selected:
        return set()
    if len(selected) == total:
        return {r[0] for r in db.execute('SELECT path FROM inventory')}
    # For each component of selected history, its parent's tree plus all changes
    # exactly covers the union of before/after paths. Deleted/renamed paths stay.
    hashes = {r['hash'] for r in selected}
    boundaries = {r['parent'] for r in selected if r['parent'] and r['parent'] not in hashes}
    # A binary numstat record can be a text↔binary conversion: it has no line
    # metric but a text endpoint must still appear in the object inventory.
    binary = {r[0] for r in db.execute('SELECT b.commit_id FROM binary_commits b JOIN selected s ON s.id=b.commit_id')}
    boundaries.update(r['hash'] for r in selected if r['id'] in binary)
    boundaries.update(r['parent'] for r in selected if r['id'] in binary and r['parent'])
    result = set()
    for row in db.execute('''SELECT p.path,d.old_path FROM changes d JOIN paths p ON p.id=d.path_id
                             JOIN selected s ON s.id=d.commit_id'''):
        result.add(row['path'])
        if row['old_path']:
            result.add(row['old_path'])
    universe = {r[0] for r in db.execute('SELECT path FROM inventory')}
    # A subset cannot contain objects outside the full history's inventory.
    # Stop once it is covered: avoids thousands of identical path inventories
    # for interleaved-author histories while preserving exact object membership.
    for sha in boundaries:
        if universe and universe.issubset(result):
            break
        extend_snapshot_paths(repo, sha, result)
    return result


def metric(added=0, removed=0, modifications=0, count=0):
    churn = added + removed
    return dict(added=added, removed=removed, growth=added - removed, churn=churn,
                modifications=modifications, frequency=modifications / count if count else 0,
                churn_rate=churn / count if count else 0)


def metrics(database, repo, filters):
    with connect(database) as db:
        total = db.execute('SELECT COUNT(*) FROM commits').fetchone()[0]
        query, values = select_commits(db, filters)
        selected = db.execute(query, values).fetchall()
        count = len(selected)
        db.execute('CREATE TEMP TABLE selected(id INTEGER PRIMARY KEY)')
        db.executemany('INSERT INTO selected VALUES (?)', ((r['id'],) for r in selected))
        scope, scope_values = scope_sql(filters)
        files = []
        paths = {p for p in object_paths(db, repo, selected, total) if in_scope(p, filters)}
        aggregates = {r['path']: r for r in db.execute(f'''
            SELECT p.path,SUM(d.added) added,SUM(d.removed) removed,
                   SUM(CASE WHEN d.added+d.removed>0 THEN 1 ELSE 0 END) modifications
            FROM changes d JOIN selected s ON s.id=d.commit_id JOIN paths p ON p.id=d.path_id
            WHERE {scope} GROUP BY p.id''', scope_values)}
        paths.update(aggregates)
        for path in paths:
            row = aggregates.get(path)
            files.append({'path': path, **(metric(row['added'], row['removed'], row['modifications'], count) if row else metric(count=count))})
        files.sort(key=lambda r: (-r['churn'], r['path']))
        # One row per changed file; each ancestor gets a commit counted once.
        directory_totals = {}
        for row in files:
            for directory in directories(row['path']):
                if filters['kind'] == 'directory' and filters['path'] and directory != filters['path'] and not directory.startswith(filters['path'] + '/'):
                    continue
                entry = directory_totals.setdefault(directory, [0, 0, set()])
                entry[0] += row['added']
                entry[1] += row['removed']
        directory_totals.setdefault(filters['path'] if filters['kind'] == 'directory' else '', [0, 0, set()])
        author_changes = {}
        timeline = {}
        days = {r['id']: datetime.fromtimestamp(r['timestamp'], timezone.utc).strftime('%Y-%m-%d') for r in selected}
        changed_commits = set()
        for row in db.execute(f'''SELECT d.*,p.path,a.canonical_id,c.timestamp FROM changes d
            JOIN selected s ON s.id=d.commit_id JOIN paths p ON p.id=d.path_id
            JOIN commits c ON c.id=d.commit_id JOIN authors a ON a.id=c.author_id WHERE {scope}''', scope_values):
            if row['added'] + row['removed'] > 0:
                changed_commits.add(row['commit_id'])
                for directory in directories(row['path']):
                    if directory in directory_totals:
                        directory_totals[directory][2].add(row['commit_id'])
            entry = author_changes.setdefault(row['canonical_id'], [0, 0, set()])
            entry[0] += row['added']
            entry[1] += row['removed']
            if row['added'] + row['removed']:
                entry[2].add(row['commit_id'])
            day = days[row['commit_id']]
            entry = timeline.setdefault(day, [0, 0, set()])
            entry[0] += row['added']
            entry[1] += row['removed']
            entry[2].add(row['commit_id'])
        author_counts = {}
        for row in selected:
            author_counts[row['canonical_id']] = author_counts.get(row['canonical_id'], 0) + 1
            day = days[row['id']]
            timeline.setdefault(day, [0, 0, set()])[2].add(row['id'])
        added = sum(f['added'] for f in files)
        removed = sum(f['removed'] for f in files)
        summary = metric(added, removed, len(changed_commits), count)
        summary.update(commit_count=count, file_count=len(files), author_count=len(author_counts))
        # Ownership always divides by all authors' churn in the same time/commit
        # set and path scope, so selecting an author does not falsely make 100%.
        base_query, base_values = select_commits(db, filters, include_author=False)
        denominator = db.execute(f'''SELECT COALESCE(SUM(d.added+d.removed),0) FROM changes d
            JOIN ({base_query}) s ON s.id=d.commit_id JOIN paths p ON p.id=d.path_id WHERE {scope}''', base_values + scope_values).fetchone()[0]
        authors = []
        for author in author_list(db):
            if 'author' in filters and author['id'] != filters['author']:
                continue
            plus, minus, modifications = author_changes.get(author['id'], [0, 0, set()])
            authors.append({**author, **metric(plus, minus, len(modifications), count),
                            'ownership': (plus + minus) / denominator if denominator else 0,
                            'commit_count': author_counts.get(author['id'], 0)})
        authors.sort(key=lambda a: (-a['churn'], a['name']))
        dirs = [{'path': path, **metric(a, r, len(m), count)} for path, (a, r, m) in directory_totals.items()]
        dirs.sort(key=lambda r: (-r['churn'], r['path']))
        return {'summary': summary, 'files': files, 'directories': dirs, 'authors': authors,
                'timeline': [{'date': day, 'added': a, 'removed': r, 'growth': a-r, 'churn': a+r, 'commits': len(c)}
                             for day, (a, r, c) in sorted(timeline.items())],
                'filters': filters, 'total_commits': total}


def commits_page(database, filters, page=1, per_page=50, search=''):
    with connect(database) as db:
        query, values = select_commits(db, filters)
        conditions, extra = [], []
        if search:
            conditions.append('(instr(lower(s.subject),lower(?))>0 OR instr(s.hash,?)>0 OR instr(lower(a.name),lower(?))>0)')
            extra.extend([search, search, search])
        scope, scope_values = scope_sql(filters)
        if filters.get('path') or filters.get('kind') == 'file':
            conditions.append(f'EXISTS (SELECT 1 FROM changes d JOIN paths p ON p.id=d.path_id WHERE d.commit_id=s.id AND {scope})')
            extra.extend(scope_values)
        where = ' AND '.join(conditions) or '1'
        base = f'FROM ({query}) s JOIN authors a ON a.id=s.canonical_id WHERE {where}'
        total = db.execute('SELECT COUNT(*) ' + base, values + extra).fetchone()[0]
        rows = db.execute(f'''SELECT s.*,a.name author_name,a.email author_email,
            (SELECT COALESCE(SUM(added),0) FROM changes WHERE commit_id=s.id) added,
            (SELECT COALESCE(SUM(removed),0) FROM changes WHERE commit_id=s.id) removed
            {base} ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?''', values + extra + [per_page, (page-1)*per_page]).fetchall()
        commits = []
        for row in rows:
            item = dict(row)
            item.update(short_hash=item['hash'][:8], author_id=item['canonical_id'], churn=item['added'] + item['removed'])
            commits.append(item)
        return dict(commits=commits, total=total, page=page, per_page=per_page)


def commit_detail(database, sha):
    with connect(database) as db:
        row = db.execute('''SELECT c.*,a.name author_name,a.email author_email,a.id canonical_id
            FROM commits c JOIN authors raw ON raw.id=c.author_id JOIN authors a ON a.id=raw.canonical_id
            WHERE c.hash=?''', (sha,)).fetchone()
        if not row:
            raise LookupError('Commit not found in the indexed non-merge history.')
        files = [{'path': r['path'], 'old_path': r['old_path'], **metric(r['added'], r['removed'])}
                 for r in db.execute('SELECT d.*,p.path FROM changes d JOIN paths p ON p.id=d.path_id WHERE commit_id=? ORDER BY added+removed DESC,p.path', (row['id'],))]
        commit = dict(row)
        commit.update(short_hash=sha[:8], author_id=row['canonical_id'], added=sum(f['added'] for f in files), removed=sum(f['removed'] for f in files))
        commit['churn'] = commit['added'] + commit['removed']
        return {'commit': commit, 'files': files}


def merge_authors(database, target, sources):
    with connect(database) as db:
        valid = {a['id'] for a in author_list(db)}
        if target not in valid or not sources or any(s not in valid for s in sources):
            raise ValueError('Choose existing canonical authors and at least one source.')
        for source in set(sources) - {target}:
            db.execute('UPDATE authors SET canonical_id=? WHERE canonical_id=?', (target, source))


def csv_export(result):
    output = io.StringIO()
    columns = ['path', 'added', 'removed', 'growth', 'churn', 'modifications', 'frequency', 'churn_rate']
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for file in result['files']:
        row = {k: file[k] for k in columns}
        if row['path'].startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
            row['path'] = "'" + row['path']
        writer.writerow(row)
    return output.getvalue()
