# Getting the app onto your phone

No Android Studio. No SDK. No command line. GitHub compiles it for you on
its own machines, and you download the finished app.

Total: about ten minutes of your attention, plus ten minutes of waiting.

---

## 1. Unzip this folder

Keep the structure exactly as it is. `android/` and `static/` must stay
where they are relative to the `.py` files.

## 2. Make a GitHub repository

- Sign in at github.com, or make a free account
- Click **+** (top right) → **New repository**
- Name it anything. **Private is fine.**
- Do **not** tick "Add a README"
- **Create repository**

## 3. Upload the files

On the empty repository page, click **uploading an existing file**.

Drag in *everything* from the unzipped folder. Wait for the list to finish
populating — it is about 50 files — then click **Commit changes**.

> If your computer hides dotfiles, the `.github` folder may not upload.
> Step 4 handles that, so do it either way.

## 4. Add the build recipe

- Click the **Actions** tab
- Click **set up a workflow yourself**
- Delete everything in the editor
- Open `.github/workflows/build-apk.yml` from the unzipped folder in any
  text editor, copy all of it, paste it in
- **Commit changes**

The build starts by itself.

## 5. Wait

**Actions** tab → click the running job to watch it. First build takes
roughly 8-12 minutes, mostly downloading the Android toolchain and
unpacking CPython.

A green tick means it worked.

## 6. Install on your phone

On your **phone**, open your repository and tap **Releases** (right-hand
side, or under the ⋯ menu on mobile).

Tap the newest release, then tap **app-debug.apk**.

Android will ask whether to allow installs from your browser. Allow it, then
tap **Install**.

Done. It is a normal app with an icon.

---

## If the build fails

Open the failed run in the Actions tab and read the red step.

**"Resource not accessible by integration"** on the Release step — the
repository is not allowed to publish releases. Fix: **Settings → Actions →
General → Workflow permissions → Read and write permissions → Save**, then
re-run the job.

**Anything mentioning NDK** — add this line inside the
`Set up Android SDK` step's `with:` block:

```yaml
          ndk-version: '26.1.10909125'
```

**Anything else** — the last twenty lines of the failed step usually name
the problem outright.

## Rebuilding later

Edit any file on GitHub and commit; a new build starts and a new release
appears. Or **Actions → Build APK → Run workflow** to rebuild unchanged.

---

## What you will actually see

A working app. Barcode scanning, title search, max-bid calculation, the
local-pickup toggle.

**The prices are synthetic**, and the app says so in a banner. Every piece
of machinery is real and tested; the data behind it is not yet. Two things
change that:

1. **A real price source.** PriceCharting API access is the fast path.
   Failing that, `store.py` harvests sold prices by watching listings
   disappear — free, but it needs weeks of polling before it can price
   anything.

2. **A bigger catalog.** `catalog.py` holds 30 titles; the NTSC-U PS2
   library is around 1,800. This is the one you will notice first in a
   shop, because a title that is not in the catalog produces nothing at
   all. It is unglamorous data entry and it is worth more than any further
   code.
