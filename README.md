# RAT — Repository Analysis Tool

A local, multi-repository Git analytics dashboard for COMS3011A. Import full Git history, explore code churn and ownership, and filter down to a directory, author, date interval, or hand-picked commit set.

## Run the submission

Requirements: **Python 3.10+**, **Git 2.30+**, and a modern browser. No Node.js, database server, PAT, or SSH key is required. Internet access is needed for the first dependency install and remote imports; ZIP imports work offline afterward.

```bash
git clone https://github.com/MlungisiMahlangu/Test1.git
cd Test1
./start.sh
```

Open **http://localhost:8000**. The script creates `.venv`, installs the pinned Flask dependency, and starts the server. Stop with Ctrl+C. On Ubuntu, if virtual-environment support is missing, install `python3-venv` first. Alternatively, run `bash start.sh`.

Manual startup:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Use `PORT=8080 ./start.sh` if port 8000 is busy. State persists under `.rat-data/` (ignored by Git); `RAT_DATA_DIR=/path/to/data` overrides the location. This is a single-user local application, not a public hosting service. It binds to loopback by default. For an explicitly trusted LAN deployment, set `HOST=0.0.0.0` and `RAT_TRUSTED_HOSTS=localhost,127.0.0.1,your-hostname`.

## Marker walkthrough

1. Select **Import repository**. Paste `https://github.com/DaveGamble/cJSON.git` (also available as a quick-fill button). Redis and Git are supported too. Optionally enter the supplied reference commit hash, branch, or tag; the default is `HEAD`.
2. Wait for indexing; progress updates automatically. Imports are full bare clones, never shallow clones. Repositories are not checked out or executed.
3. **Overview** shows all repository/commit-set metrics, a timeline, author ownership, and file hotspots. **Files & directories** provides all metrics at either granularity; select a path to drill down.
4. Filter by author, exact file/directory, and start/end date-time, then press **Apply**. The end instant is exclusive. Reset restores the whole history. File table search and commit search narrow their respective lists only.
5. In **Commits**, select any commits across pages and choose **Apply selection**. Commit details show the original diff statistics and rename source. Applied selections intersect the other filters.
6. **Authors → Merge authors** combines multiple identities into one canonical identity, without changing Git history. `.mailmap` from the chosen reference is applied automatically. Manual merges persist; re-import to reset them.
7. Import another repository and switch using the sidebar. Each repository has independent analysis and filters. **Export CSV** downloads the currently filtered file metrics.

### ZIP imports

The archive must include its **complete `.git` directory**, not just source files. GitHub's “Download ZIP” does not contain history. For example, from the directory containing a local clone:

```bash
python3 -m zipfile -c repository.zip your-repository-directory
```

Select **Import repository → Upload ZIP**. A bare Git repository also works. A `.git` pointer is accepted only if its referenced Git data is inside the archive. Shallow uploads, external object alternates, traversal paths, symlinks, encrypted entries, and incomplete metadata are rejected. Limits: 512 MiB compressed, 2 GiB expanded, 200,000 entries.

## Metric definitions and decisions

- History is the set of **non-merge commits reachable from the specified reference**. Side-branch commits are included; merge commits contribute no line metrics. Dates are **committer dates**, never author dates.
- Each commit is compared with its sole parent, or the empty tree for an initial commit. Git's `--numstat -z --find-renames=50%` provides line statistics and binary detection. External diff/textconv drivers are disabled.
- Growth = `added − removed`; churn = `added + removed`.
- A pure rename contributes zero churn and zero modifications. Rename-and-edit statistics are attributed to the **destination path**; prior changes remain on the paths used at those commits. Deleted files retain their removed-line statistics. Paths are not retroactively rewritten through later renames.
- Directory statistics recursively sum all descendant files exactly once. Repository statistics are the root directory's statistics.
- Modifications count **distinct commits with churn > 0** in a scope, not the number of file events. Frequency = modifications / selected commit count. Churn rate = churn / selected commit count. Empty denominators yield 0.
- Author modifications count that author's modifying commits. Author ownership = author churn / **all-author churn in the same path/time/manual-commit scope**. Selecting an author changes the shown commit set but preserves the ownership denominator. API ownership/frequency are fractions; the UI displays ownership as a percentage.
- Files include text objects present **before or after** the selected commits, including unchanged, empty, renamed, and deleted paths. Consequently, “Files” is not a count of changed files; zero-metric rows are intentional. Binary changes do not contribute line metrics, including text/binary conversion diffs reported by Git as binary.
- Timestamps entered in the UI use the browser's local time zone; timeline buckets use UTC calendar dates. Manual author merges and `.mailmap` mappings are scoped per repository.
- A reference is an immutable analyzed snapshot. To analyze newer remote commits or another reference, import it again. No credentials or private repositories are supported.

## Architecture

- `rat/engine.py`: a streaming, NUL-safe Git parser; indexed SQLite commit/change tables; cached snapshot inventories for boundary cases; SQL file aggregation and single-pass ancestor/author aggregation. History is indexed once and persists across restarts. No subprocess per commit during primary indexing.
- `app.py`: Flask JSON API, background import workers, isolated repository storage, upload/URL validation, and CSV export. Two imports can run concurrently.
- `static/`: dependency-free HTML/CSS/JavaScript, responsive navigation, SVG charts, accessible dialogs, loading/error states, table sorting, commit pagination, and no external analytics/CDNs.
- `.rat-data/<id>/`: sanitized bare Git data and metrics database. Credentials, runtime data, environments, and test fixtures are excluded from the submission.

Boundary inventories may require extra Git snapshot reads for merge parents, binary conversions, or disconnected commit selections. Very large repositories require more disk/time for their initial full clone and indexing; subsequent queries reuse the index. No historical data is silently truncated.

## Tests

After startup has installed dependencies:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests generate deterministic local repositories and check initial commits, additions/deletions, recursive directory sums, denominator rules, empty sets, binary exclusions/conversions, rename-only and rename-plus-edit changes, tabs/newlines/Unicode paths, merge exclusion, files created during merges, date boundaries, reference selection, `.mailmap`, manual merging, CSV safety, ingestion, persistence, multirepo isolation, ZIP safety, and HTTP errors. The clone transport unit test is mocked; the real cJSON import is also verified through the browser.

Optional offline demo: `.venv/bin/python tests/demo_fixture.py` creates `.test-artifacts/demo-repository.zip` with 4 commits, 11 additions, 3 removals, and churn 14.

Optional scale test: `.venv/bin/python tests/benchmark.py --commits 100000 --author-query`. A synthetic 100,000-commit/200-file run measured approximately 3.1 seconds to index, 0.7 seconds for root metrics, and 0.12 seconds for an author query (local machine; excludes clone time). Real repositories with large diffs and many merge boundaries can take longer. Files and authors are paginated in the UI; common query results are cached and invalidated after merges.

## API

All endpoints are same-origin and return JSON errors as `{ "error": "..." }`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/repos` | List imports and indexing progress |
| POST | `/api/repos/clone` | JSON `{url, reference}` |
| POST | `/api/repos/upload` | Multipart `file`, optional `reference` |
| GET | `/api/repos/<id>/meta` | Authors, aliases, paths, and time bounds |
| GET | `/api/repos/<id>/metrics` | All metric categories |
| GET | `/api/repos/<id>/commits` | Searchable, paginated commits |
| GET | `/api/repos/<id>/commits/<hash>` | Individual commit and file metrics |
| POST | `/api/repos/<id>/authors/merge` | JSON `{target, sources: [...]}` |
| GET | `/api/repos/<id>/export.csv` | Filtered file statistics |
| DELETE | `/api/repos/<id>` | Remove local analysis only |

Metric/filter query parameters: `path`, `kind=file|directory`, `author=<canonical id>`, `since=<UNIX timestamp>`, `until=<exclusive UNIX timestamp>`, `commits=<comma-separated full hashes>`. Commit listing also supports `q`, `page`, and `per_page` (maximum 200).

## Development disclosure

Developed with AI coding assistance. Automated fixture tests and live browser checks were used to verify behavior; generated code was reviewed and corrected during implementation.
