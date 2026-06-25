import os
import sys


def is_running_with_pytest() -> bool:
    if 'PYTEST_CURRENT_TEST' in os.environ:
        return True

    if os.path.basename(sys.argv[0]).startswith('pytest'):  # noqa: PTH119
        return True

    if 'pytest' in sys.modules:
        return True

    return False
