// Shared-backend config for the distributed app.
//
// If you ship this app to other people and want every install to silently use
// YOUR Railway as its backend (code runs, files, saved API keys), put your
// Railway URL here before building the APK:
//
//   window.FORGE_SHARED_URL = 'https://your-app.up.railway.app';
//
// Then deploy this source on Railway with FORGE_SHARED=1, a FORGE_TOKEN, and a
// volume at /data. Leave the value empty to keep the normal per-user setup.
window.FORGE_SHARED_URL = 'https://forge-backend-production-4425.up.railway.app';
