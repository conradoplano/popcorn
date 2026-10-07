# Popcorn

Small Django app to keep lists of the movies and TV shows you want to watch and have watched, rate them, and
share the lists with friends. Everyone who registers has their own lists. Runs on the NAS as a Docker container,
data in SQLite. Works on the phone and can be added to the home screen. Built the same way as
[Chef](https://github.com/conradoplano/chef).

The name is set with `SITE_NAME`, so renaming the app only takes one environment variable.

## What it does

- **🏠 Home** (where the app opens): friend requests to answer, imported titles waiting to be checked, and
  **new from your friends**: per friend, the titles they added or watched since your last visit (opening the
  app after more than an hour away), with **+ My list** on each. Below, how many movies and shows you want to
  watch and the shows you're watching.
  - **Recommended for you**: 5 movies and 5 TV shows a day, each with why, where it streams and the group it
    suits. **+ To watch** or **✓ Seen it** adds it (in that group); **Not for me** hides it for good.
    Candidates come from TMDB (similar to what you liked or want to watch, popular on your services); AI
    picks from them knowing your lists, ratings, groups and services, and the ones on your services come
    first. A new set is made the first time you open the app on a day, only if your lists, groups or services
    changed since the last one. **↻ New recommendations** makes one any time. AI approval and the daily AI
    limits apply to both (see TMDB and AI).
- **🎬 Movies** and **📺 TV shows**: one list each. Filter by *Want to watch*, *Watching* (shows only),
  *Watched* or *All*, and search as you type. Add a title at the top: typing searches TMDB, and picking a
  result adds it with its poster, genres and where to stream it (or press Enter to add it as typed). It goes
  into the list you're looking at. A TV show can be added as the whole show or as one season (each its own
  title). **Group by** genre or where to watch (your own streaming services first); each title is under one
  heading. **My groups** (e.g. "With the kids", one per title) filter both lists. **✓ Watched** moves a title to what you've seen; tap the stars to rate it from 1 to 5 (tap the
  same star again to clear it). Rating something you haven't watched yet marks it as watched. A title's page
  has its overview, then status, rating and group, each saved as soon as it's picked. Below, the details are
  saved with a button: title, year, season, where to watch (TMDB's are kept up to date unless switched off for
  that title; services you add are kept), genres and notes. It also finds a title typed by hand on TMDB, and
  removes it.
- **👥 Friends**: friends see each other's movie and TV show lists, and nothing else: there are no comments
  or likes, and lists are never public. The Friends tab shows a badge for requests waiting for you. Each
  friend shows how many titles are new since your last visit and when they were last active; on their list
  the new ones are marked. **+ My list** puts a title on your own list as one to watch.
  - **By email**: the person gets an email and becomes your friend once they accept on their Friends page.
    If the address has no account yet, the email invites them, and the request waits until they register.
    If they had already asked you, you're friends straight away.
  - **By invite link**: everyone has a link (Friends → 🔗 Your invite link). Whoever opens it and signs in or
    registers becomes your friend. *Make a new link* stops the old one working; existing friends stay.
  - **Remove** ends the friendship for both people.
- **⚙️ Manage**:
  - **Import a list**: paste titles (one per line, with headings like "Movies:" or "TV shows", years like
    "(2021)", "S2" for shows) or a message with recommendations. It's read in the background: each line is
    looked up on TMDB, and AI only helps where that isn't enough (typos, other languages, text that isn't a
    list). What's found waits under **Check and add**: pick another match, add it as typed, choose *want to
    watch* or *watched*, then add or discard each one (or all). Titles already on your lists are skipped.
  - **Where you watch**: your country and your streaming services. Changing the country updates where to watch
    for all your titles; it's also checked again weekly when you open a list. *Update all my titles* also
    looks up titles typed in by hand where TMDB has a certain match.

Not built yet: recommendations.

## TMDB and AI

- **TMDB** ([The Movie Database](https://www.themoviedb.org/settings/api), free for non-commercial use): set
  `TMDB_API_KEY` (the API key or the read access token). Where-to-watch data on TMDB comes from JustWatch.
  `WATCH_REGION` is the default country, `TMDB_LANGUAGE` the language of titles and overviews. Without a key
  everything works, but titles are kept as typed.
- **AI** for imports, the same way as Chef: `OPENAI_API_KEY`, `AI_MODEL`, `AI_EFFORT`. Every call is recorded
  with its cost (admin → AI usage). Someone can use AI once an admin ticks *AI approved* on their user (staff
  always can); `AI_DAILY_LIMIT_USD` per person (or their own limit) and `AI_GLOBAL_DAILY_LIMIT_USD` for
  everyone keep it affordable. Without AI, imports still work line by line.

## Login

There are no passwords. A user enters their email and gets a six-digit code (valid 10 minutes).
Only users that already exist (registered or added with `adduser`) can log in. At most 5 codes are sent per
address every 15 minutes. `REGISTRATION_OPEN=false` closes registration. From the command line:

```sh
python manage.py adduser you@example.com --name "You" --admin
```

If `EMAIL_HOST` is not set, emails are printed to the console / container logs.

## Local development

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:DEBUG = "true"
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py adduser you@example.com --admin
.\.venv\Scripts\python manage.py runserver
```

Open http://127.0.0.1:8000, enter your email, and copy the code from the terminal output
(or set `$env:DEV_LOGIN = "true"` to sign in with just the email).

Run tests with `python manage.py test` (with `DEBUG=true`).

## Docker / NAS

```sh
cp .env.example .env   # set SECRET_KEY, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, SMTP...
docker compose up -d --build
```

- To try the image locally over http://localhost:8062 (separate database in `./data-local`, `DEV_LOGIN` on):
  `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build`
- The SQLite database lives in `./data` (mounted at `/data`); back up that whole folder. The container runs
  as UID 1000. Migrations run automatically on container start.
- `INITIAL_ADMIN_EMAIL` in `.env` creates the first admin user on startup.
- Health check: `GET /health/`.
- Every push to `main` runs the tests and publishes `ghcr.io/<owner>/<repo>:latest`
  (`.github/workflows/docker.yml`), as for Chef. On the NAS, serve it behind DSM's reverse proxy on port 5062.
- On the Synology NAS: Container Manager → Project → Create, with `deploy/docker-compose.nas.yml` (fill in its
  values there). To update, rebuild the project: it pulls the newest image and migrates on start.
