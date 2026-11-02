"""Allow ``python -m sclm``."""

import sys

from sclm.app import main

if __name__ == "__main__":
    sys.exit(main())
