# Majlis

An English team trivia party game for Android, played by 2 to 4 teams on one shared device with a host. Each game uses 6 categories with 200 / 400 / 600 point tiles, a countdown timer, steals, and 16 draftable abilities.

## Layout

| Folder | What it is |
|---|---|
| `squareup/` | Game source and data: the app template (`majlis2.template.html`), `build.py`, 85 category banks in `q/`, picture questions in `qimg/`, and the built `index.html` |
| `majlis-android/` | Android Studio project that wraps the built game in a WebView |
| `.github/workflows/build-apk.yml` | GitHub Actions job that builds `Majlis.apk` and attaches it to a Release |

Full details (data format, abilities, architecture, open work) are in [`squareup/HANDOVER.md`](squareup/HANDOVER.md).

## Build the game

```bash
cd squareup
python3 build.py   # writes squareup/index.html and majlis-android/app/src/main/assets/index.html
```

Open `squareup/index.html` in any browser to play.

## Build the APK

Push a change under `majlis-android/` (or run the **Build APK** workflow by hand from the Actions tab). The workflow builds a release APK signed with the debug key and publishes it as a Release named `build-<n>`. Alternatively, open `majlis-android/` in Android Studio and use Build > Build APK(s).
