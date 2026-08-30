"""支持 `python -m geo_engine` 直接调用 CLI。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
