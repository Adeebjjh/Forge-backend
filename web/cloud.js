'use strict';
/* Cloud page: on-device runner card + Railway card. Called from app.js show('cloud'). */
async function refreshDevice() {
  const badge = $('device-badge'), status = $('device-status'), workspace = $('device-workspace');
  if (!native) {
    badge.textContent = 'APP ONLY'; status.textContent = 'Install the Forge Android app to run tools on your phone, or deploy the runner on Railway and connect it below.';
    $('device-start').hidden = true; return;
  }
  try {
    const r = await nativeCall('deviceStatus');
    badge.textContent = r.running ? 'RUNNING' : 'STOPPED';
    status.textContent = r.running
      ? 'Your phone is the computer. Tools run right here.'
      : 'Start the runner to use your phone as the computer.';
    workspace.textContent = r.workspace ? 'Workspace: ' + r.workspace : '';
    $('device-start').hidden = !!r.running; $('device-stop').hidden = !r.running;
  } catch (e) { badge.textContent = 'UNAVAILABLE'; status.textContent = e.message; }
}
async function refreshCloud() {
  await refreshDevice();
  if (typeof refreshGitHub === 'function') refreshGitHub();
}
$('device-start').onclick = async () => {
  if (!native) return;
  $('device-start').disabled = true;
  try { await nativeCall('deviceAction', 'start'); await connect(); toast('On-device runner started.'); }
  catch (e) { toast(e.message); }
  finally { $('device-start').disabled = false; refreshDevice(); }
};
$('device-stop').onclick = async () => {
  if (!native) return;
  try { await nativeCall('deviceAction', 'stop'); toast('On-device runner stopped.'); }
  catch (e) { toast(e.message); }
  finally { refreshDevice(); }
};
$('railway-connect').onclick = () => pair();
$('railway-deploy').onclick = () => {
  const url = 'https://railway.app/new';
  if (native && window.ForgeNative.openExternal) window.ForgeNative.openExternal(url);
  else window.open(url, '_blank', 'noopener');
};
