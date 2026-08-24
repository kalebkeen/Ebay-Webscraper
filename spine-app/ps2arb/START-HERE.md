# Get the app on your phone

No Android Studio. No SDK. No command line. GitHub builds the APK for you
and gives you a download link.

Roughly 15 minutes, most of it waiting.

---

## 1. Make a GitHub account

<https://github.com/signup> — free.

## 2. Make an empty repository

<https://github.com/new>

- Name it anything (`spine`)
- **Private** is fine
- Do **not** tick "Add a README"
- Click **Create repository**

## 3. Upload this folder

On the new repo's page click **uploading an existing file**.

Unzip `spine.zip`, then drag **everything inside it** into the browser
window — all the files and folders at once. GitHub keeps the folder
structure.

Wait for the file list to finish appearing, then click **Commit changes**.

> The `.github` folder is the important one and starts with a dot, so some
> file managers hide it. On Windows tick **Hidden items** in Explorer's View
> tab; on Mac press `Cmd + Shift + .` in Finder. Without it, nothing builds.

## 4. Wait for the build

Click the **Actions** tab. A job called **Build APK** starts on its own.

First run takes about 5-10 minutes. A green tick means it worked.

If it fails, open the failed run and read the red step — the error is
usually one line, and you can paste it back into the chat.

## 5. Download it on your phone

Open the repo on your phone's browser and go to **Releases** (right-hand
side, or add `/releases` to the URL).

Tap the `.apk` file. Android will ask whether to allow installs from your
browser — say yes. Then open it and install.

That's it.

---

## What you'll see

A working app. Barcode scanning, title search, max bids, the local-pickup
toggle.

**The prices are synthetic**, and the app shows a banner saying so. The
machinery is finished; the data is not. Two things separate this from a tool
you would act on:

1. **A real price source.** PriceCharting API access works immediately and
   costs money. The built-in harvester is free but needs weeks of polling
   before it can price anything.

2. **A bigger catalog.** It knows 30 games. The PS2 library is about 1,800.
   This is the one you will hit first in an actual shop — scan something it
   does not know and nothing happens.

## Making changes later

Edit a file on github.com, commit, and a new build starts automatically.
Every build appears under Releases. You never need a local setup.
