# Majlis (Android)

Offline Android app wrapping the Majlis trivia game (all questions bundled in `app/src/main/assets/index.html`).

## Get the APK
- **GitHub (no setup):** push this folder to a GitHub repo. The "Build APK" action builds `Majlis.apk` and attaches it to a Release. Open the Release on your phone and tap the APK to install (allow "Install unknown apps" for your browser).
- **Android Studio:** File > Open this folder, let it sync, then Build > Build App Bundle(s) / APK(s) > Build APK(s).

## Update the game
Replace `app/src/main/assets/index.html` with a new build and push again.
