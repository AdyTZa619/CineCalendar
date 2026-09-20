-- CineCalendar SQLite schema v9
-- Canonical reference kept in sync with migrations in cinecalendar/db.py.

PRAGMA foreign_keys=ON;

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
  updated_at TEXT NOT NULL,
  tmdb_id INTEGER,
  title_norm TEXT,
  original_title_norm TEXT
);

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
  action TEXT,
  exposure_history_id INTEGER REFERENCES recommendation_history(id) ON DELETE SET NULL,
  predicted_rating REAL,
  confidence REAL
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

CREATE TABLE recommendation_trust_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  history_id INTEGER NOT NULL UNIQUE REFERENCES recommendation_history(id) ON DELETE CASCADE,
  run_id INTEGER REFERENCES recommendation_runs(id) ON DELETE SET NULL,
  movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
  context_date TEXT NOT NULL,
  slot TEXT NOT NULL,
  rank_position INTEGER NOT NULL,
  engine_version TEXT NOT NULL,
  trust_status TEXT NOT NULL,
  trust_score REAL,
  gate_score REAL,
  support_count INTEGER NOT NULL DEFAULT 0,
  support_labels TEXT NOT NULL DEFAULT '[]',
  red_flag INTEGER NOT NULL DEFAULT 0,
  red_reason TEXT,
  score_gap REAL,
  als_score REAL,
  public_bayes REAL,
  created_at TEXT NOT NULL
);

CREATE TABLE metadata_provenance(
  movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
  field TEXT NOT NULL,
  provider TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(movie_id,field)
);

CREATE TABLE recommendation_outcomes(
  exposure_history_id INTEGER PRIMARY KEY REFERENCES recommendation_history(id) ON DELETE CASCADE,
  movie_id INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
  rank_position INTEGER,
  context_date TEXT NOT NULL,
  slot TEXT NOT NULL,
  chosen_at TEXT,
  playback_at TEXT,
  watched_at TEXT,
  rating_id INTEGER REFERENCES ratings(id) ON DELETE SET NULL,
  actual_rating INTEGER,
  rating_date TEXT,
  predicted_rating REAL,
  confidence REAL,
  final_score REAL,
  engine_version TEXT,
  absolute_error REAL,
  resolved_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE recommendation_explanations(
  history_id INTEGER PRIMARY KEY REFERENCES recommendation_history(id) ON DELETE CASCADE,
  personal_reason TEXT NOT NULL DEFAULT '',
  why_not TEXT NOT NULL DEFAULT '',
  score_factors_json TEXT NOT NULL DEFAULT '{}',
  contributions_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE TABLE schema_migrations(
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

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
CREATE INDEX ix_rec_hist_exposure ON recommendation_history(exposure_history_id);
CREATE INDEX ix_rec_hist_context_action ON recommendation_history(context_date,action);
CREATE INDEX ix_rec_hist_root_context_id
  ON recommendation_history(context_date,id DESC) WHERE action IS NULL;
CREATE INDEX ix_rec_hist_exposure_action_id
  ON recommendation_history(exposure_history_id,action,id DESC);
CREATE INDEX ix_rec_trust_date_status ON recommendation_trust_audit(context_date,trust_status);
CREATE INDEX ix_rec_trust_movie_date ON recommendation_trust_audit(movie_id,context_date);
CREATE INDEX ix_rec_trust_engine_history ON recommendation_trust_audit(engine_version,history_id);
CREATE INDEX ix_metadata_provenance_provider ON metadata_provenance(provider);
CREATE INDEX ix_rec_outcome_movie ON recommendation_outcomes(movie_id);
CREATE INDEX ix_rec_outcome_rating_date ON recommendation_outcomes(rating_date);
CREATE INDEX ix_rec_outcome_engine_context ON recommendation_outcomes(engine_version,context_date);
CREATE INDEX ix_rec_outcome_context ON recommendation_outcomes(context_date);
