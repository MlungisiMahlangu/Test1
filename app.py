"""RAT local web server. Run via ./start.sh; imported repositories are never executed."""
from __future__ import annotations

import io
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from rat import engine

ROOT = Path(__file__).resolve().parent
MAX_EXPANDED = 2 * 1024 ** 3
MAX_FILES = 200000


def validate_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError('Enter a public HTTPS Git repository URL.')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Only public HTTPS URLs without credentials, queries, or fragments are accepted.')
    if parsed.port not in (None, 443):
        raise ValueError('Only the standard HTTPS port is supported.')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError('The repository hostname could not be resolved.')
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('Local, private, and reserved network addresses cannot be cloned.')
    return url


def safe_extract(archive_path, destination):
    """Reject links, traversal, encrypted archives, and decompression bombs."""
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES or sum(e.file_size for e in entries) > MAX_EXPANDED:
            raise ValueError('ZIP exceeds the 2 GiB expanded size or 200,000 entry limit.')
        for entry in entries:
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                    or '\x00' in entry.filename or stat.S_ISLNK(mode) or entry.flag_bits & 1):
                raise ValueError('ZIP contains unsafe paths, symbolic links, or encrypted entries.')
            if mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError('ZIP contains unsupported special files.')
            target = destination.joinpath(*path.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError('ZIP path escapes the extraction directory.')
        actual = 0
        for entry in entries:
            target = destination.joinpath(*PurePosixPath(entry.filename).parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as src, target.open('wb') as dst:
                while chunk := src.read(1024 * 1024):
                    actual += len(chunk)
                    if actual > MAX_EXPANDED:
                        raise ValueError('ZIP expanded data exceeds 2 GiB.')
                    dst.write(chunk)


def find_git_directory(root):
    candidates = []
    for current, dirs, files in os.walk(root):
        here = Path(current)
        if here.name == '.git' or ('HEAD' in files and 'objects' in dirs):
            candidates.append(here)
            dirs[:] = []
            continue
        if '.git' in files:
            contents = (here / '.git').read_text(errors='replace').strip()
            if not contents.startswith('gitdir:'):
                raise ValueError('Invalid .git pointer file.')
            target = (here / contents[7:].strip()).resolve()
            if not target.is_relative_to(root.resolve()) or not target.is_dir():
                raise ValueError('The .git pointer must reference a directory included in the ZIP.')
            candidates.append(target)
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) != 1:
        raise ValueError('ZIP must contain exactly one repository, including its .git directory (not a GitHub source ZIP).')
    return candidates[0]


def sanitize_git(source, destination):
    """Build a clean bare repository from inert objects/refs, never uploaded config."""
    common = source
    if (source / 'commondir').is_file():
        common = (source / (source / 'commondir').read_text().strip()).resolve()
        if not common.is_relative_to(source.parents[0].resolve()):
            raise ValueError('Linked worktree common Git data must be inside the uploaded repository.')
    if not (common / 'objects').is_dir() or not (source / 'HEAD').is_file():
        raise ValueError('The uploaded Git metadata is incomplete.')
    if (common / 'objects/info/alternates').exists():
        raise ValueError('Repositories using external object alternates must be made self-contained before upload.')
    destination.mkdir(parents=True)
    (destination / 'objects').mkdir()
    (destination / 'refs').mkdir()
    (destination / 'config').write_text('[core]\n\tbare = true\n\trepositoryformatversion = 0\n')
    for name in ('HEAD', 'packed-refs', 'shallow'):
        src = source / name if name == 'HEAD' else common / name
        if src.is_file():
            shutil.copyfile(src, destination / name)
    refs = common / 'refs'
    if refs.is_dir():
        shutil.copytree(refs, destination / 'refs', dirs_exist_ok=True)
    for child in (common / 'objects').iterdir():
        if child.is_dir() and re.fullmatch('[0-9a-f]{2}', child.name):
            for blob in child.iterdir():
                if blob.is_file() and re.fullmatch('[0-9a-f]{38}|[0-9a-f]{62}', blob.name):
                    target = destination / 'objects' / child.name / blob.name
                    target.parent.mkdir(exist_ok=True)
                    shutil.copyfile(blob, target)
        elif child.name == 'pack' and child.is_dir():
            target = destination / 'objects/pack'
            target.mkdir()
            for pack in child.iterdir():
                if pack.is_file() and re.fullmatch(r'pack-[0-9a-f]+\.(pack|idx|rev)', pack.name):
                    shutil.copyfile(pack, target / pack.name)


def create_app(data_dir=None):
    app = Flask(__name__, static_folder=str(ROOT / 'static'), static_url_path='/static')
    app.config.update(MAX_CONTENT_LENGTH=512 * 1024 ** 2, JSON_SORT_KEYS=False,
                          TRUSTED_HOSTS=os.environ.get('RAT_TRUSTED_HOSTS', 'localhost,127.0.0.1,[::1]').split(','))
    data = Path(data_dir or os.environ.get('RAT_DATA_DIR', ROOT / '.rat-data')).resolve()
    data.mkdir(parents=True, exist_ok=True)
    registry = data / 'registry.sqlite3'
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='rat-index')
    app.extensions['rat_executor'] = executor
    app.config['RAT_DATA_DIR'] = data
    result_cache = OrderedDict()
    cache_lock = threading.Lock()
    revisions = {}

    def registry_db():
        db = sqlite3.connect(registry, timeout=30, factory=engine.ClosingConnection)
        db.row_factory = sqlite3.Row
        return db

    with registry_db() as db:
        db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS repos(id TEXT PRIMARY KEY,name TEXT,source TEXT,status TEXT,
              progress INTEGER,error TEXT,commit_count INTEGER,created_at INTEGER,reference TEXT,head TEXT);
            UPDATE repos SET status='error',error='Indexing was interrupted. Remove and import this repository again.'
              WHERE status='indexing';''')

    def update(rid, **fields):
        with registry_db() as db:
            db.execute('UPDATE repos SET ' + ','.join(f'{key}=?' for key in fields) + ' WHERE id=?', [*fields.values(), rid])

    def get_repo(rid, ready=False):
        with registry_db() as db:
            row = db.execute('SELECT * FROM repos WHERE id=?', (rid,)).fetchone()
        if not row:
            raise LookupError('Repository not found.')
        if ready and row['status'] != 'ready':
            raise ValueError('Wait until repository indexing has completed.')
        return dict(row)

    def new_repo(name, source, reference):
        if not isinstance(reference, str) or not reference or reference.startswith('-') or len(reference) > 240:
            raise ValueError('Enter a valid Git reference (HEAD, branch, tag, or commit hash).')
        rid = uuid.uuid4().hex
        with registry_db() as db:
            db.execute('INSERT INTO repos VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (rid, name[:200], source, 'indexing', 0, None, 0, int(time.time()), reference, None))
        (data / rid).mkdir()
        return get_repo(rid)

    def import_job(repo, archive=None):
        rid = repo['id']
        folder = data / rid
        git_dir = folder / 'repo.git'
        try:
            update(rid, progress=3)
            if archive:
                extracted = folder / 'upload'
                extracted.mkdir()
                safe_extract(archive, extracted)
                sanitize_git(find_git_directory(extracted), git_dir)
            else:
                # No shallow/partial clone, credentials, redirects, submodules or checkout.
                result = subprocess.run(['git', '-c', 'credential.helper=', '-c', 'http.followRedirects=false',
                    '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
                    'clone', '--bare', '--no-local', '--', repo['source'], str(git_dir)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=engine.environment(), timeout=1200)
                if result.returncode:
                    raise ValueError('Clone failed. Use a public HTTPS repository URL. ' + result.stderr.decode('utf-8', 'replace')[-1200:])
            update(rid, progress=10)
            head, count = engine.index_repository(git_dir, folder / 'metrics.sqlite3', repo['reference'],
                                                  lambda pct, message: update(rid, progress=pct))
            update(rid, head=head, commit_count=count, progress=100, status='ready')
        except (ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile, sqlite3.Error) as exc:
            app.logger.warning('Import %s failed: %s', rid, exc)
            update(rid, status='error', error=str(exc)[:1800])
        except Exception:
            app.logger.exception('Unexpected indexing failure')
            update(rid, status='error', error='Unexpected indexing error. Check the server log, then re-import.')
        finally:
            if archive:
                Path(archive).unlink(missing_ok=True)
                shutil.rmtree(folder / 'upload', ignore_errors=True)

    @app.before_request
    def protect_local_api():
        # No cross-origin mutation; app is a single-user localhost tool.
        if request.method in ('POST', 'DELETE', 'PUT', 'PATCH'):
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                return jsonify(error='Cross-origin requests are not allowed.'), 403
            if request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return jsonify(error='Cross-site requests are not allowed.'), 403

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(Exception)
    def errors(error):
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        if isinstance(error, LookupError):
            return jsonify(error=str(error)), 404
        if isinstance(error, (ValueError, TypeError)):
            return jsonify(error=str(error)), 400
        app.logger.exception('Request failed')
        return jsonify(error='The request could not be completed. Check the server log.'), 500

    @app.get('/')
    def home():
        return send_from_directory(ROOT / 'static', 'index.html')

    @app.get('/api/health')
    def health():
        return jsonify(status='ok')

    @app.get('/api/repos')
    def repos():
        with registry_db() as db:
            return jsonify(repos=[dict(r) for r in db.execute('SELECT * FROM repos ORDER BY created_at DESC,id')])

    @app.post('/api/repos/clone')
    def clone():
        body = request.get_json()
        if not isinstance(body, dict):
            raise ValueError('Send a JSON object containing url and optional reference.')
        url = validate_url(body.get('url'))
        name = urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1].removesuffix('.git') or 'Repository'
        repo = new_repo(name, url, body.get('reference') or 'HEAD')
        executor.submit(import_job, repo)
        return jsonify(repo), 202

    @app.post('/api/repos/upload')
    def upload():
        file = request.files.get('file')
        if not file or not file.filename or not file.filename.lower().endswith('.zip'):
            raise ValueError('Choose a .zip file that includes the .git directory.')
        repo = new_repo(Path(file.filename).name[:-4], 'ZIP upload', request.form.get('reference') or 'HEAD')
        archive = data / repo['id'] / 'upload.zip'
        try:
            file.save(archive)
            executor.submit(import_job, repo, archive)
        except Exception:
            update(repo['id'], status='error', error='Upload could not be saved.')
            raise
        return jsonify(repo), 202

    @app.get('/api/repos/<rid>/meta')
    def meta(rid):
        repo = get_repo(rid, ready=True)
        with engine.connect(data / rid / 'metrics.sqlite3') as db:
            paths = [r[0] for r in db.execute('SELECT path FROM inventory ORDER BY path')]
            dirs = {''}
            for path in paths:
                dirs.update(engine.directories(path))
            bounds = db.execute('SELECT MIN(timestamp),MAX(timestamp) FROM commits').fetchone()
            return jsonify(repo=repo, authors=engine.author_list(db), commit_count=repo['commit_count'],
                           min_timestamp=bounds[0], max_timestamp=bounds[1],
                           paths=[{'path': p, 'kind': 'directory'} for p in sorted(dirs)] + [{'path': p, 'kind': 'file'} for p in paths])

    def cached_metrics(rid):
        filters = engine.normalize_filters(request.args)
        with cache_lock:
            revision = revisions.get(rid, 0)
            key = (rid, revision, json.dumps(filters, sort_keys=True))
            if key in result_cache:
                result_cache.move_to_end(key)
                return result_cache[key]
        result = engine.metrics(data / rid / 'metrics.sqlite3', data / rid / 'repo.git', filters)
        # At most four modest results: large exports remain streamed by the index.
        if len(result['files']) + len(result['authors']) + len(result['directories']) < 15000:
            with cache_lock:
                if revision == revisions.get(rid, 0):
                    result_cache[key] = result
                    while len(result_cache) > 4:
                        result_cache.popitem(last=False)
        return result

    @app.get('/api/repos/<rid>/metrics')
    def metrics(rid):
        get_repo(rid, ready=True)
        return jsonify(cached_metrics(rid))

    @app.get('/api/repos/<rid>/commits')
    def commits(rid):
        get_repo(rid, ready=True)
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(200, max(1, int(request.args.get('per_page', 50))))
        return jsonify(engine.commits_page(data / rid / 'metrics.sqlite3', engine.normalize_filters(request.args),
                                           page, per_page, request.args.get('q', '')[:500]))

    @app.get('/api/repos/<rid>/commits/<sha>')
    def detail(rid, sha):
        get_repo(rid, ready=True)
        return jsonify(engine.commit_detail(data / rid / 'metrics.sqlite3', sha))

    @app.post('/api/repos/<rid>/authors/merge')
    def merge(rid):
        get_repo(rid, ready=True)
        body = request.get_json()
        if not isinstance(body, dict) or 'target' not in body or not isinstance(body.get('sources'), list):
            raise ValueError('Provide a target author ID and a list of source author IDs.')
        target = int(body['target'])
        sources = [int(s) for s in body['sources']]
        engine.merge_authors(data / rid / 'metrics.sqlite3', target, sources)
        with cache_lock:
            revisions[rid] = revisions.get(rid, 0) + 1
            for key in list(result_cache):
                if key[0] == rid:
                    del result_cache[key]
        return jsonify(ok=True)

    @app.get('/api/repos/<rid>/export.csv')
    def export(rid):
        get_repo(rid, ready=True)
        result = cached_metrics(rid)
        return Response(engine.csv_export(result), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename="rat-file-metrics.csv"'})

    @app.delete('/api/repos/<rid>')
    def remove(rid):
        repo = get_repo(rid)
        if repo['status'] == 'indexing':
            raise ValueError('Wait until indexing has finished before removing this repository.')
        with registry_db() as db:
            db.execute('DELETE FROM repos WHERE id=?', (rid,))
        shutil.rmtree(data / rid, ignore_errors=True)
        with cache_lock:
            revisions[rid] = revisions.get(rid, 0) + 1
            for key in list(result_cache):
                if key[0] == rid:
                    del result_cache[key]
        return jsonify(ok=True)

    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    create_app().run(host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', '8000')), threaded=True, debug=False)
