import os

os.environ["CINECALENDAR_V5_ALPHA"] = "1"

from cinecalendar.app import main

if __name__ == "__main__":
    raise SystemExit(main())
