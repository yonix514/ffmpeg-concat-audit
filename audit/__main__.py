"""`python -m audit` -> junction audit CLI."""
import sys
from .junctions import main

if __name__ == "__main__":
    raise SystemExit(main(["junctions"] + sys.argv[1:]))
