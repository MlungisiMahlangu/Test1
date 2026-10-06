"""Deterministic local Git repositories; no network or external test dependencies."""
import io
import itertools
import os
import stat
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from rat import engine
from app import create_app, safe_extract, validate_url

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / '.test-artifacts'
TEMP.mkdir(exist_ok=True)


class RepositoryFixture:
    def __init__(self, folder):
        self.repo = folder / 'source'
        self.repo.mkdir()
        self.run('init', '-b', 'main')
        self.hashes = []
        self.clock = 1700000000

    def run(self, *args, **kwargs):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE, **kwargs).decode().strip()

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)

    def commit(self, subject, name='Alice', email='alice@example.com', allow_empty=False):
        self.run('add', '-A')
        env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                   GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email,
                   GIT_AUTHOR_DATE='1600000000 +0000', GIT_COMMITTER_DATE=f'{self.clock} +0000')
        self.run('-c', 'commit.gpgsign=false', 'commit', '--allow-empty' if allow_empty else '--quiet', '-m', subject, env=env)
        self.hashes.append(self.run('rev-parse', 'HEAD'))
        self.clock += 100
        return self.hashes[-1]

    def standard(self):
        self.write('src/a.txt', 'alpha\nbeta\ngamma\n')
        self.write('src/nested/b.txt', 'one\ntwo\n')
        self.write('unchanged.txt', 'static\n')
        self.write('empty.txt', '')
        self.write('data.bin', b'\x00\x01\x02binary')
        self.commit('Initial text and binary files')
        self.write('src/a.txt', 'alpha\nBETA\ngamma\ndelta\n')
        (self.repo / 'src/nested/b.txt').unlink()
        self.write('new.txt', 'new\n')
        self.commit('Edit, delete and add', 'Bob', 'bob@example.com')
        self.run('mv', 'src/a.txt', 'src/renamed.txt')
        self.commit('Pure rename', 'Alicia', 'alias@example.com')
        self.write('src/renamed.txt', 'alpha\nBETA\ngamma\ndelta\nextra\n')
        self.write('.mailmap', 'Alice <alice@example.com> Alicia <alias@example.com>\n')
        self.commit('Alias edit and mailmap', 'Alicia', 'alias@example.com')
        return self

    def zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in self.repo.rglob('*'):
                if path.is_file():
                    archive.write(path, 'project/' + path.relative_to(self.repo).as_posix())
        buf.seek(0)
        return buf


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.fixture = RepositoryFixture(self.folder).standard()
        self.db = self.folder / 'metrics.sqlite3'
        engine.index_repository(self.fixture.repo, self.db)

    def metrics(self, **filters):
        return engine.metrics(self.db, self.fixture.repo, engine.normalize_filters(filters))

    def test_repository_formulas(self):
        summary = self.metrics()['summary']
        expected = dict(added=11, removed=3, growth=8, churn=14, modifications=3,
                        frequency=.75, churn_rate=3.5, commit_count=4, author_count=2)
        for key, value in expected.items():
            self.assertEqual(summary[key], value, key)

    def test_binary_exclusion_empty_files_and_deleted_paths(self):
        files = {f['path']: f for f in self.metrics()['files']}
        self.assertNotIn('data.bin', files)
        self.assertIn('empty.txt', files)
        self.assertEqual(files['empty.txt']['churn'], 0)
        self.assertEqual(files['src/nested/b.txt']['removed'], 2)
        self.assertEqual(files['src/nested/b.txt']['growth'], 0)
        self.assertEqual(files['src/nested/b.txt']['modifications'], 2)

    def test_directory_recursive_totals_and_distinct_commits(self):
        result = self.metrics()
        directories = {d['path']: d for d in result['directories']}
        self.assertEqual(directories['']['churn'], 14)
        self.assertEqual(directories['src']['added'], 8)
        self.assertEqual(directories['src']['removed'], 3)
        self.assertEqual(directories['src']['modifications'], 3)
        self.assertEqual(directories['src/nested']['churn'], 4)

    def test_pure_rename_has_no_modification(self):
        sha = self.fixture.hashes[2]
        result = self.metrics(commits=sha)
        self.assertEqual(result['summary']['churn'], 0)
        self.assertEqual(result['summary']['modifications'], 0)
        files = {f['path'] for f in result['files']}
        self.assertIn('src/a.txt', files)
        self.assertIn('src/renamed.txt', files)
        self.assertIn('unchanged.txt', files)
        self.assertNotIn('data.bin', files)
        self.assertNotIn('.mailmap', files)

    def test_mailmap_and_ownership(self):
        authors = {a['name']: a for a in self.metrics()['authors']}
        self.assertEqual(set(authors), {'Alice', 'Bob'})
        self.assertEqual(authors['Alice']['churn'], 8)
        self.assertEqual(authors['Bob']['churn'], 6)
        self.assertAlmostEqual(authors['Alice']['ownership'], 8/14)
        alice = self.metrics(author=str(authors['Alice']['id']))
        self.assertEqual(alice['summary']['commit_count'], 3)
        self.assertEqual(alice['summary']['churn'], 8)
        self.assertAlmostEqual(alice['authors'][0]['ownership'], 8/14)

    def test_manual_merge_keeps_repository_totals(self):
        authors = self.metrics()['authors']
        engine.merge_authors(self.db, authors[0]['id'], [authors[1]['id']])
        result = self.metrics()
        self.assertEqual(len(result['authors']), 1)
        self.assertEqual(result['authors'][0]['churn'], 14)
        self.assertEqual(result['authors'][0]['modifications'], 3)
        self.assertEqual(result['authors'][0]['ownership'], 1)
        self.assertEqual(result['summary']['churn'], 14)

    def test_committer_dates_inclusive_exclusive(self):
        result = self.metrics(since='1700000100', until='1700000200')
        self.assertEqual(result['summary']['commit_count'], 1)
        self.assertEqual(result['summary']['added'], 3)
        self.assertEqual(result['summary']['removed'], 3)
        self.assertEqual(result['timeline'][0]['date'], '2023-11-14')

    def test_noncontiguous_manual_selection(self):
        result = self.metrics(commits=','.join([self.fixture.hashes[0], self.fixture.hashes[3]]))
        self.assertEqual(result['summary']['commit_count'], 2)
        self.assertEqual(result['summary']['added'], 8)
        self.assertEqual(result['summary']['removed'], 0)

    def test_empty_set_zero_denominators(self):
        result = self.metrics(since='1800000000')
        for key in ('churn', 'frequency', 'churn_rate', 'modifications', 'commit_count'):
            self.assertEqual(result['summary'][key], 0)
        self.assertTrue(all(a['ownership'] == 0 for a in result['authors']))

    def test_scope_preserves_commit_denominator(self):
        result = self.metrics(path='src/nested', kind='directory')
        self.assertEqual(result['summary']['churn'], 4)
        self.assertEqual(result['summary']['commit_count'], 4)
        self.assertEqual(result['summary']['frequency'], .5)
        result = self.metrics(path='unchanged.txt', kind='file', commits=self.fixture.hashes[1])
        self.assertEqual(result['summary']['churn'], 0)
        self.assertEqual(result['summary']['file_count'], 1)

    def test_specific_reference(self):
        db = self.folder / 'reference.sqlite3'
        engine.index_repository(self.fixture.repo, db, self.fixture.hashes[1])
        result = engine.metrics(db, self.fixture.repo, engine.normalize_filters({}))
        self.assertEqual(result['summary']['commit_count'], 2)
        self.assertEqual(result['summary']['churn'], 12)

    def test_commit_detail_pagination_search(self):
        result = engine.commits_page(self.db, engine.normalize_filters({}), page=2, per_page=2)
        self.assertEqual(result['total'], 4)
        self.assertEqual(len(result['commits']), 2)
        found = engine.commits_page(self.db, engine.normalize_filters({}), search='rename')
        self.assertEqual(found['total'], 1)
        detail = engine.commit_detail(self.db, self.fixture.hashes[2])
        self.assertEqual(detail['files'][0]['old_path'], 'src/a.txt')
        self.assertEqual(detail['files'][0]['path'], 'src/renamed.txt')

    def test_every_subset_inventory_matches_git_snapshots(self):
        for length in range(1, 5):
            for hashes in itertools.combinations(self.fixture.hashes, length):
                expected = set()
                for sha in hashes:
                    parents = engine.git(self.fixture.repo, 'rev-list', '--parents', '-n', '1', sha).decode().split()
                    for endpoint in parents:
                        raw = engine.git(self.fixture.repo, 'diff', '--numstat', '-z', '--no-renames', engine.EMPTY_TREE, endpoint)
                        stream = iter(raw.split(b'\0'))
                        for token in stream:
                            if token:
                                record = engine.numstat(token, stream)
                                if record:
                                    expected.add(record[0])
                actual = {f['path'] for f in self.metrics(commits=','.join(hashes))['files']}
                self.assertEqual(actual, expected, hashes)

    def test_invalid_filters(self):
        for filters in ({'since':'x'}, {'kind':'other'}, {'since':'20','until':'10'}, {'commits':'HEAD'}):
            with self.assertRaises(ValueError):
                engine.normalize_filters(filters)

    def test_csv_formula_neutralization(self):
        row = {'path': '=CMD()', **engine.metric(1, 0)}
        self.assertIn("'=CMD()", engine.csv_export({'files': [row]}))


class HistoryEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.fixture = RepositoryFixture(self.folder)

    def analyze(self):
        db = self.folder / 'metrics.sqlite3'
        engine.index_repository(self.fixture.repo, db)
        return engine.metrics(db, self.fixture.repo, engine.normalize_filters({}))

    def test_empty_repository(self):
        self.assertEqual(self.analyze()['summary']['commit_count'], 0)

    def test_empty_commit(self):
        self.fixture.commit('No files yet', allow_empty=True)
        result = self.analyze()
        self.assertEqual(result['summary']['commit_count'], 1)
        self.assertEqual(result['summary']['churn_rate'], 0)

    def test_tabs_newlines_unicode_paths_and_rename_edit(self):
        old, new = 'src/a\t☃\n.txt', 'src/renamed\t☃\n.txt'
        self.fixture.write(old, 'a\nb\nc\nd\ne\nf\n')
        self.fixture.commit('Weird path')
        self.fixture.run('mv', old, new)
        self.fixture.write(new, 'a\nb\nc\nd\ne\nf\ng\n')
        self.fixture.commit('Rename plus edit')
        result = self.analyze()
        files = {f['path']:f for f in result['files']}
        self.assertEqual(files[new]['added'], 1)
        self.assertEqual(files[new]['removed'], 0)
        self.assertEqual(result['summary']['added'], 7)

    def test_binary_to_text_inventory_with_zero_line_metrics(self):
        f = self.fixture
        f.write('convert.txt', b'\x00binary')
        f.commit('Binary')
        f.write('convert.txt', 'now text\n')
        sha = f.commit('Text conversion')
        result = self.analyze()
        self.assertIn('convert.txt', {r['path'] for r in result['files']})
        subset = engine.metrics(self.folder / 'metrics.sqlite3', f.repo, engine.normalize_filters({'commits': sha}))
        self.assertEqual(subset['summary']['file_count'], 1)
        self.assertEqual(subset['summary']['churn'], 0)

    def test_merge_commits_excluded_side_branch_included(self):
        f = self.fixture
        f.write('base.txt', 'base\n')
        f.commit('Base')
        f.run('checkout', '-b', 'feature')
        f.write('feature.txt', 'feature\n')
        f.commit('Feature', 'Bob', 'bob@example.com')
        f.run('checkout', 'main')
        f.write('main.txt', 'main\n')
        f.commit('Main')
        env = dict(os.environ, GIT_AUTHOR_NAME='Alice', GIT_AUTHOR_EMAIL='alice@example.com',
                   GIT_COMMITTER_NAME='Alice', GIT_COMMITTER_EMAIL='alice@example.com')
        f.run('-c', 'commit.gpgsign=false', 'merge', '--no-ff', '--no-commit', 'feature', env=env)
        f.write('merge-only.txt', 'Created only during merge\n')
        f.commit('Merge feature')
        f.write('after.txt', 'after\n')
        sha = f.commit('After merge')
        result = self.analyze()
        self.assertEqual(result['summary']['commit_count'], 4)
        self.assertEqual(result['summary']['added'], 4)
        self.assertEqual(result['summary']['file_count'], 5)
        subset = engine.metrics(self.folder / 'metrics.sqlite3', f.repo, engine.normalize_filters({'commits': sha}))
        self.assertEqual(subset['summary']['file_count'], 5)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.fixture = RepositoryFixture(self.folder).standard()
        self.app = create_app(self.folder / 'data')
        self.addCleanup(lambda: self.app.extensions['rat_executor'].shutdown(wait=True))
        self.client = self.app.test_client()

    def upload(self):
        response = self.client.post('/api/repos/upload', data={'file': (self.fixture.zip(), 'fixture.zip')})
        self.assertEqual(response.status_code, 202)
        rid = response.json['id']
        for _ in range(1500):
            repo = next(r for r in self.client.get('/api/repos').json['repos'] if r['id'] == rid)
            if repo['status'] != 'indexing':
                self.assertEqual(repo['status'], 'ready', repo.get('error'))
                return rid
            time.sleep(.02)
        self.fail('Indexing timed out')

    def test_upload_metrics_meta_export_and_persistence(self):
        rid = self.upload()
        self.assertEqual(self.client.get(f'/api/repos/{rid}/metrics').json['summary']['churn'], 14)
        self.assertEqual(self.client.get(f'/api/repos/{rid}/meta').json['commit_count'], 4)
        self.assertEqual(self.client.get(f'/api/repos/{rid}/export.csv').status_code, 200)
        self.assertEqual(self.client.get(f'/api/repos/{rid}/commits').json['total'], 4)
        app2 = create_app(self.folder / 'data')
        self.addCleanup(lambda: app2.extensions['rat_executor'].shutdown(wait=True))
        self.assertEqual(len(app2.test_client().get('/api/repos').json['repos']), 1)

    def test_cached_metrics_invalidated_after_merge(self):
        rid = self.upload()
        before = self.client.get(f'/api/repos/{rid}/metrics').json
        authors = before['authors']
        response = self.client.post(f'/api/repos/{rid}/authors/merge', json={'target':authors[0]['id'], 'sources':[authors[1]['id']]})
        self.assertEqual(response.status_code, 200)
        after = self.client.get(f'/api/repos/{rid}/metrics').json
        self.assertEqual(after['summary']['author_count'], 1)
        self.assertEqual(after['summary']['churn'], before['summary']['churn'])
        self.assertEqual(after['authors'][0]['ownership'], 1)
        self.assertEqual(self.client.post(f'/api/repos/{rid}/authors/merge', json={}).status_code, 400)

    def test_multiple_repositories_and_delete(self):
        first, second = self.upload(), self.upload()
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.client.get('/api/repos').json['repos']), 2)
        self.assertEqual(self.client.delete(f'/api/repos/{first}').status_code, 200)
        self.assertEqual(self.client.get(f'/api/repos/{first}/metrics').status_code, 404)
        self.assertEqual(self.client.get(f'/api/repos/{second}/metrics').status_code, 200)

    def test_static_and_errors(self):
        with self.client.get('/') as response:
            self.assertEqual(response.status_code, 200)
        with self.client.get('/static/app.js') as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/repos', headers={'Host': 'attacker.example'}).status_code, 400)
        self.assertEqual(self.client.post('/api/repos/upload').status_code, 400)
        self.assertEqual(self.client.post('/api/repos/clone', json=[]).status_code, 400)
        self.assertEqual(self.client.get('/api/repos/missing/meta').status_code, 404)
        self.assertEqual(self.client.post('/api/repos/clone', json={'url':'https://127.0.0.1/repo'}).status_code, 400)
        self.assertEqual(self.client.post('/api/repos/clone', json={'url':'file:///etc'}).status_code, 400)
        self.assertEqual(self.client.post('/api/repos/clone', json={}, headers={'Origin':'https://evil.example'}).status_code, 403)

    def test_zip_traversal_and_symlink_rejected(self):
        for name, mode in [('../escape.txt', 0), ('/absolute.txt', 0), ('link', (stat.S_IFLNK | 0o777) << 16)]:
            archive = self.folder / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode
                z.writestr(info, 'bad')
            with self.assertRaises(ValueError):
                safe_extract(archive, self.folder / 'extract')

    def test_clone_endpoint_worker_with_mocked_transport(self):
        # Transport alone is mocked; import/index/API all remain real.
        original = subprocess.run
        def transport(args, **kwargs):
            if isinstance(args, list) and 'clone' in args:
                target = Path(args[-1])
                result = original(['git', 'clone', '--bare', str(self.fixture.repo), str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                return result
            return original(args, **kwargs)
        with patch('app.validate_url', return_value='https://example.com/repo.git'), patch('app.subprocess.run', side_effect=transport):
            response = self.client.post('/api/repos/clone', json={'url':'https://example.com/repo.git'})
            self.assertEqual(response.status_code, 202)
            self.app.extensions['rat_executor'].shutdown(wait=True)
        rid = response.json['id']
        self.assertEqual(self.client.get(f'/api/repos/{rid}/metrics').json['summary']['churn'], 14)


if __name__ == '__main__':
    unittest.main()
