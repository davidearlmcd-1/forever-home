# Forever Home — Setup (all in your browser, ~10 minutes, one time)

This puts your Forever Home tool on GitHub, where it runs itself **free, every Monday morning**, and publishes a live report + an auto-updating Google Earth map. You never have to run anything yourself after setup.

You'll do six short steps. No coding, no command line.

---

## Step 1 — Make a free GitHub account
Go to **github.com** → Sign up. (Free plan is all you need.)

## Step 2 — Create a new repository
- Click the **+** (top right) → **New repository**.
- Repository name: **forever-home**
- Set it to **Public** (required so the free website works).
- Click **Create repository**.

## Step 3 — Upload the project files
- On the new repo page, click **"uploading an existing file"** (a link in the quick-setup box).
- Drag in **all the files from the bundle**, keeping the folder structure. The important ones:
  - `forever_home_live.py`
  - `SETUP.md`
  - the folder **`.github`** (which contains `workflows/forever_home.yml`)
- If the `.github` folder is awkward to drag, instead click **Add file → Create new file**, type this exact path in the name box:
  `.github/workflows/forever_home.yml`
  then paste the contents of that file from the bundle, and **Commit**.
- Commit the uploads (green **Commit changes** button).

## Step 4 — Add your RentCast key as a secret
This keeps your key private even though the repo is public.
- In the repo: **Settings** → (left sidebar) **Secrets and variables** → **Actions**.
- Click **New repository secret**.
- Name: **`RENTCAST_API_KEY`**  (exactly this)
- Secret: **paste your RentCast key**  *(the one you created at developers.rentcast.io)*
- **Add secret**.

## Step 5 — Turn on the free website (GitHub Pages)
- **Settings** → **Pages**.
- Under **Build and deployment → Source**, choose **GitHub Actions**.
- (Nothing else to set — the workflow handles publishing.)

## Step 6 — Run it once to confirm
- Click the **Actions** tab → if prompted, click **"I understand… enable workflows"**.
- Choose **"Forever Home weekly update"** on the left → **Run workflow** → **Run workflow**.
- Wait ~1 minute for the green check.
- Your live report is now at:
  **`https://YOUR-USERNAME.github.io/forever-home/`**
  (replace YOUR-USERNAME). Bookmark it.

---

## What you get, automatically, every Monday
- **The report page** (the URL above) — top candidates, ranked, with the summary tables.
- **Download map (.kmz)** button — opens in Google Earth with pins grouped into price-band folders you can toggle; pin color = ocean drive-time tier (green = under 1 hour).
- **Auto-refresh map** button — download that one file, open it in Google Earth **once**, and it re-pulls the latest listings on its own. That's your living map.

## Good to know
- **Cost:** $0. The job makes ~10 requests/run, well under RentCast's free 50/month, and the code hard-stops at 12 so it can never run up a bill.
- **Change the schedule / states / price cap:** edit the top of `forever_home_live.py` (the CONFIG block) or the `cron:` line in the workflow file, right in GitHub's web editor.
- **Email + Google Drive delivery:** the report URL + auto-refresh map cover the core need without extra setup. Emailing the report or copying the KMZ to Google Drive each week is a straightforward add-on we can wire in next if you want it — it just needs a couple of extra credentials.
