-- CineCalendar SQLite schema v4
-- Generated from the same migrations used by the application.

CREATE TABLE calendar_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL,
  event_date TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE(event_key,event_date)
);

CREATE TABLE feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  weight REAL NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE import_files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path_name TEXT,
  size_bytes INTEGER NOT NULL,
  mtime_ns INTEGER,
  sha256 TEXT NOT NULL UNIQUE,
  row_count INTEGER NOT NULL DEFAULT 0,
  imported_at TEXT NOT NULL
);

CREATE TABLE metadata_cache(
  provider TEXT NOT NULL,
  cache_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  expires_at TEXT,
  PRIMARY KEY(provider, cache_key)
);

CREATE TABLE movies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  imdb_id TEXT UNIQUE,
  identity_key TEXT NOT NULL,
  title TEXT NOT NULL,
  original_title TEXT,
  year INTEGER,
  title_type TEXT,
  runtime_min INTEGER,
  genres_json TEXT NOT NULL DEFAULT '[]',
  directors_json TEXT NOT NULL DEFAULT '[]',
  countries_json TEXT NOT NULL DEFAULT '[]',
  overview TEXT NOT NULL DEFAULT '',
  keywords_json TEXT NOT NULL DEFAULT '[]',
  semantic_json TEXT NOT NULL DEFAULT '{}',
  imdb_rating REAL,
  num_votes INTEGER,
  release_date TEXT,
  poster_url TEXT,
  source TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
, tmdb_id INTEGER, title_norm TEXT, original_title_norm TEXT);

CREATE TABLE ratings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  movie_id INTEGER NOT NULL UNIQUE REFERENCES movies(id) ON DELETE CASCADE,
  rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 10),
  date_rated TEXT,
  source TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE recommendation_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
  recommended_at TEXT NOT NULL,
  context_date TEXT NOT NULL,
  slot TEXT NOT NULL DEFAULT 'today',
  final_score REAL,
  ignored INTEGER NOT NULL DEFAULT 0,
  action TEXT
);

CREATE TABLE recommendation_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  context_date TEXT NOT NULL,
  slot TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  candidate_count INTEGER NOT NULL,
  result_count INTEGER NOT NULL,
  engine_version TEXT NOT NULL
);

CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

CREATE TABLE settings(
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE user_profile(
  profile_key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE watchlist(
  movie_id INTEGER PRIMARY KEY REFERENCES movies(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'want_to_watch',
  added_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX ix_feedback_movie ON feedback(movie_id);

CREATE INDEX ix_movies_identity ON movies(identity_key);

CREATE INDEX ix_movies_original_norm_year_type ON movies(original_title_norm,year,title_type);

CREATE INDEX ix_movies_title_norm_year_type ON movies(title_norm,year,title_type);

CREATE INDEX ix_movies_tmdb ON movies(tmdb_id);

CREATE INDEX ix_movies_votes ON movies(num_votes);

CREATE INDEX ix_movies_year ON movies(year);

CREATE INDEX ix_rec_hist_movie_date ON recommendation_history(movie_id,recommended_at);
