"""Connect module-owned AI integration tests to repository-wide discovery."""

from pathlib import Path

from taip.ai_integration import testing


def load_tests(loader, tests, pattern):
    testing_directory = Path(testing.__file__).parent
    return loader.discover(
        start_dir=str(testing_directory),
        pattern=pattern or "test_*.py",
        top_level_dir=str(testing_directory.parents[2]),
    )
