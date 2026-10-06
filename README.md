# Popcorn

Small Django app to keep lists of the movies and TV shows you want to watch and have watched, rate them, and
share the lists with friends. Everyone who registers has their own lists. Runs on the NAS as a Docker container,
data in SQLite. Works on the phone and can be added to the home screen. Built the same way as
[Chef](https://github.com/conradoplano/chef).

The name is set with `SITE_NAME`, so renaming the app only takes one environment variable.

## What it does

- **🎬 Movies** and **📺 TV shows**: one list each. Filter by *Want to watch*, *Watching* (shows only),
  *Watched* or *All*, and search as you type. Add a title (and year) at the top: it goes into the list you're
  looking at. **✓ Watched** moves a title to what you've seen; tap the stars to rate it from 1 to 5 (tap the
  same star again to clear it). Rating something you haven't watched yet marks it as watched. A title's page
  has its status, rating and notes, and removes it.
- **👥 Friends**: friends see each other's movie and TV show lists, and nothing else: there are no comments
  or likes. On a friend's list, **+ My list** puts a title on your own list as one to watch.
  - **By email**: the person gets an email and becomes your friend once they accept on their Friends page.
    If the address has no account yet, the email invites them, and the request waits until they register.
    If they had already asked you, you're friends straight away.
  - **By invite link**: everyone has a link (Friends → 🔗 Your invite link). Whoever opens it and signs in or
    registers becomes your friend. *Make a new link* stops the old one working; existing friends stay.
  - **Remove** ends the friendship for both people.
- **🌍 Public lists**: each list (movies, TV shows) can be made public separately. A public list is at its
  own link (`/p/<token>/movies/`), readable by anyone, also without an account. Lists start private.

Not built yet: recommendations, and looking titles up in a movie database (e.g. TMDB) for posters and to
avoid typing.

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
