"""Create a small offline demo ZIP with known metrics; run from the project root."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_metrics import RepositoryFixture


def main():
    output = ROOT / '.test-artifacts'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        fixture = RepositoryFixture(Path(temporary)).standard()
        destination = output / 'demo-repository.zip'
        destination.write_bytes(fixture.zip().getvalue())
    print(destination)
    print('Expected: 4 commits, +11 / -3, growth 8, churn 14, modifications 3.')
    print('Alice (mailmapped): churn 8; Bob: churn 6. Frequency 0.75; churn rate 3.5.')


if __name__ == '__main__':
    main()
