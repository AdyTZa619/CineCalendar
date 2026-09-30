import os

# Stable must never inherit Alpha mode from a parent shell or an old test environment.
for _key in (
    "CINECALENDAR_V5_ALPHA",
    "CINECALENDAR_V5_ALPHA_ALLOW_EMPTY",
    "CINECALENDAR_V5_ALPHA_SOURCE_DB",
):
    os.environ.pop(_key, None)

from cinecalendar.app import main

if __name__ == "__main__":
    raise SystemExit(main())
