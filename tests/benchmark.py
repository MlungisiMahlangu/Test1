"""Optional reproducible scale benchmark: python tests/benchmark.py --commits 100000."""
import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rat import engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commits', type=int, default=10000)
    parser.add_argument('--author-query', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.commits <= 200000:
        parser.error('Use 1 through 200,000 commits.')
    workspace = ROOT / '.test-artifacts'
    workspace.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workspace) as temp:
        root = Path(temp)
        repo = root / 'benchmark.git'
        subprocess.run(['git', 'init', '--bare', str(repo)], check=True, stdout=subprocess.DEVNULL)
        start = time.perf_counter()
        process = subprocess.Popen(['git', '-C', str(repo), 'fast-import', '--quiet'], stdin=subprocess.PIPE)
        for i in range(args.commits):
            content = f'version {i % 19}\n'
            author = i % 10
            record = (f'commit refs/heads/main\ncommitter Author {author} <author{author}@example.com> {1700000000+i} +0000\n'
                      f'data 9\nchange {i%10}\nM 100644 inline src/mod{i%200//20}/file{i%200}.txt\n'
                      f'data {len(content)}\n{content}\n')
            process.stdin.write(record.encode())
        process.stdin.close()
        if process.wait():
            raise RuntimeError('Fixture generation failed')
        generation = time.perf_counter() - start
        subprocess.run(['git', '-C', str(repo), 'symbolic-ref', 'HEAD', 'refs/heads/main'], check=True)
        start = time.perf_counter()
        database = root / 'metrics.sqlite3'
        engine.index_repository(repo, database)
        indexing = time.perf_counter() - start
        start = time.perf_counter()
        result = engine.metrics(database, repo, engine.normalize_filters({}))
        query = time.perf_counter() - start
        expected_removed = max(0, args.commits - 200)
        assert result['summary']['added'] == args.commits, result['summary']
        assert result['summary']['removed'] == expected_removed, result['summary']
        assert result['summary']['commit_count'] == args.commits, result['summary']
        output = {'commits': args.commits, 'fixture_seconds': round(generation, 3),
                  'index_seconds': round(indexing, 3), 'root_query_seconds': round(query, 3),
                  'peak_rss_MiB': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                  'database_MiB': round(database.stat().st_size / 1024**2, 1),
                  'summary': result['summary']}
        print(json.dumps(output, indent=2), flush=True)
        if args.author_query:
            start = time.perf_counter()
            filtered = engine.metrics(database, repo, engine.normalize_filters({'author': '1'}))
            print(json.dumps({'author_query_seconds': round(time.perf_counter() - start, 3), 'summary': filtered['summary']}, indent=2))


if __name__ == '__main__':
    main()
