'use strict';
// Preview is the default. Live mode requires an explicit, valid same-origin status.
// Live browser authentication uses the vendored PocketBase default LocalAuthStore.
// Preview choices, withdrawal capabilities and vault secrets are never persisted here.
const byId = id => document.getElementById(id);
let signedIn = false, live = null, token = '', account = null, revision = null, unsubscribeToken = '', busy = false, resetNoticeShown = false;
let linkWithdrawalToken = '';
let PocketBase, BaseAuthStore;
let pb = null, authEpoch = 0, writingAuth = false;
function saveAuth(value, record) {
  writingAuth = true;
  try { value ? pb.authStore.save(value, record) : pb.authStore.clear(); }
  catch {
    pb = new PocketBase(location.origin, new BaseAuthStore());
    if (value) pb.authStore.save(value, record);
  } finally { writingAuth = false; }
}
function clearIdentity(clearStored = true) {
  authEpoch++; token = ''; account = null; revision = null; signedIn = false;
  if (clearStored && pb) saveAuth('');
  byId('identity').hidden = true; googleButton.hidden = false; byId('signout').hidden = true;
  byId('preferences-status').hidden = true; byId('onboarding').hidden = true;
  byId('enrollment-form').reset(); byId('connect-code').textContent = connectExample;
  syncContinue();
}
async function restoreSession() {
  if (!pb.authStore.token) return;
  if (!pb.authStore.isValid || Date.now() >= Date.parse(live.resetAt)) { clearIdentity(); return; }
  const epoch = authEpoch;
  // Refresh in an isolated store: a late response must never revive a signed-out tab.
  const validator = new PocketBase(location.origin, new BaseAuthStore());
  validator.authStore.save(pb.authStore.token, pb.authStore.record);
  setBusy(true); googleButton.disabled = true; message('Checking your saved session…');
  try {
    const result = await validator.collection('users').authRefresh();
    if (epoch !== authEpoch) return;
    if (result.record?.collectionName !== 'users' || !result.record.email || !validator.authStore.isValid) throw new Error('Invalid saved session');
    token = result.token; account = result.record;
    await refreshPreferences();
    if (epoch !== authEpoch || Date.now() >= Date.parse(live.resetAt)) return;
    saveAuth(token, account); setIdentity(account.email, account.name || 'Google account');
    message('Signed in. Review the terms and your optional choices.');
  } catch {
    if (epoch === authEpoch) { clearIdentity(); message('Your saved session could not be verified. Sign in again.'); }
  } finally { setBusy(false); googleButton.disabled = false; }
}
// Fragments never reach the HTTP server. Remove the capability from visible/history
// URLs before any discovery requests; confirmation is always a separate user action.
{
  const parameters = new URLSearchParams(location.hash.slice(1));
  if (parameters.has('unsubscribe')) {
    const values = parameters.getAll('unsubscribe');
    history.replaceState(null, '', location.pathname + location.search);
    byId('link-withdrawal').hidden = false;
    byId('mode-banner').querySelector('.preview-tag').textContent = 'WITHDRAWAL LINK';
    byId('mode-banner').lastElementChild.textContent = 'Only confirmation sends a withdrawal request. No Google sign-in required.';
    if (values.length === 1 && /^[A-Za-z0-9_-]{32,128}$/.test(values[0])) {
      linkWithdrawalToken = values[0];
    } else {
      byId('confirm-withdrawal').disabled = true;
      byId('withdrawal-status').textContent = 'This withdrawal link is incomplete or invalid. Use the complete link from your message.';
    }
    parameters.delete('unsubscribe');
  }
}
const connectExample = byId('connect-code').textContent;
const installExample = byId('install-code').textContent;
// Installation is available only when the backend advertises the public skill contract.
function checkedInstallation(data) {
  const installation = data?.installation;
  if (data?.clientReady !== true || installation?.method !== 'skills' || installation.source !== 'https://github.com/pocketcontext/vaultcontext-demo/tree/main/skills/vaultcontext' || installation.skill !== 'vaultcontext' || !/^[a-f0-9]{40}$/.test(installation.clientRevision)) return null;
  return installation;
}
function clientAvailable() { return !!checkedInstallation(live); }
function renderInstallation() {
  const installation = checkedInstallation(live);
  if (!installation) {
    byId('install-code').textContent = installExample;
    byId('install-intro').textContent = 'Live setup is unavailable until this server advertises its public skill and tested client revision. Do not use production credentials.';
    byId('install-note').textContent = 'No valid installation contract is available. No commands are run for you.';
    return;
  }
  byId('install-code').closest('.codebox').querySelector('.copy').textContent = 'Copy instructions';
  byId('install-code').closest('.codebox').classList.add('agent-prompt');
  byId('install-intro').textContent = 'Copy these instructions into your coding agent. It will install and load the public VaultContext skill, then guide you through setup. Node.js/npm and uv are required.';
  byId('install-code').textContent = `Install the VaultContext skill in this workspace with:

npx skills add ${installation.source} --skill vaultcontext --yes

Use the appropriate --agent option if needed (codex or claude-code), then load the installed skill and follow its instructions. Check that Node.js/npm and uv are available first. Preserve any local skill changes and lock existing VaultContext sessions before upgrading.

Before running the client, verify that the installed skill launcher pins client revision ${installation.clientRevision}. If the branch has advanced or the pin differs, pause setup and report the mismatch; do not substitute another client revision.

Help me connect to ${location.origin}. Browser enrollment is required first: I must sign in on the website and accept the terms using the same Google account as the CLI. Use vaultcontext login for Google sign-in; on a remote host, guide me through the callback port forwarding described in the skill.

Guide me through checking access, initializing only if I have no identity, and unlocking. I will enter my vault passphrase only in my private terminal, never in chat, commands, environment variables or pipes. Do not ask for it.

Help me save, compare and restore a harmless synthetic file without inspecting its contents. The demo resets at 00:00 UTC; after a reset, repeat enrollment and sign-in and create a fresh identity. The unlocked host and coding agent are trusted.`;
  byId('install-note').textContent = 'The public skill includes a launcher pinned to a tested client commit. Installing it does not sign in or unlock your vault. GitHub must be available; no GitHub credentials are required.';
}
const googleButton = byId('google-button');
const cancelGoogleButton = byId('cancel-google');
let cancelGoogleLogin = null;
cancelGoogleButton.addEventListener('click', () => cancelGoogleLogin?.());
function canContinue() {
  return !busy && signedIn && (!live ||
    (Number.isInteger(revision) && revision >= 0 && !!token && !!account && pb.authStore.isValid && Date.now() < Date.parse(live.resetAt)));
}
function syncContinue() { document.querySelector('.continue-button').disabled = !canContinue(); }
function setBusy(value) { busy = value; byId('signout').disabled = value; syncContinue(); }
function message(value) { byId('form-status').textContent = value; }
async function api(path, body, authenticated = false) {
  const epoch = authEpoch;
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', cache:'no-store', credentials:'omit',
    headers: {'Accept':'application/json', ...(body === undefined ? {} : {'Content-Type':'application/json'}), ...(authenticated ? {'Authorization':token} : {})},
    ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  if (authenticated && epoch !== authEpoch) throw new Error('Your session changed. Sign in again.');
  if (authenticated && (response.status === 401 || response.status === 403)) {
    clearIdentity();
    const error = new Error('Your session expired. Sign in again.'); error.status = response.status; throw error;
  }
  if (authenticated && response.status === 503) { revision = null; syncContinue(); }
  if (response.status === 204) return {};
  let data; try { data = await response.json(); } catch { throw new Error('Service unavailable. Please try again later.'); }
  if (authenticated && epoch !== authEpoch) throw new Error('Your session changed. Sign in again.');
  if (authenticated && Date.now() >= Date.parse(live.resetAt)) {
    clearIdentity(); throw new Error('The daily reset is due. Refresh before continuing.');
  }
  if (!response.ok) {
    const error = new Error(response.status === 409 ? 'Preferences changed. Review the latest choices and submit again.' : response.status === 503 ? 'The demo is resetting or temporarily unavailable. Please try again later.' : response.status === 401 ? 'Your session expired. Sign in again.' : 'The request could not be completed. Please try again.');
    error.status = response.status; throw error;
  }
  return data;
}
function validStatus(data) {
  return data?.enabled === true && /^\d{4}-\d{2}-\d{2}$/.test(data.generation) && typeof data.termsVersion === 'string' && Number.isFinite(Date.parse(data.resetAt));
}
function updateCountdown() {
  if (signedIn && live && pb && !pb.authStore.isValid) { clearIdentity(); message('Your session expired. Sign in again.'); }
  syncContinue();
  const now = new Date();
  const midnight = live ? Date.parse(live.resetAt) : Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1);
  const seconds = Math.max(0, Math.floor((midnight - now.getTime()) / 1000));
  byId('countdown').textContent = [Math.floor(seconds / 3600), Math.floor(seconds % 3600 / 60), seconds % 60].map(n => String(n).padStart(2, '0')).join(' : ');
  if (live && seconds === 0) {
    if (token || pb?.authStore.token) clearIdentity();
    byId('countdown-note').textContent = 'Reset due. Refresh to start a new session.';
    document.querySelector('.continue-button').disabled = true;
    if (!resetNoticeShown) message('The daily reset is due. Refresh this page before continuing.');
    resetNoticeShown = true;
  }
}
function setIdentity(email, name) {
  signedIn = true; byId('identity').hidden = false; googleButton.hidden = true;
  byId('identity-name').textContent = name;
  byId('identity-email').textContent = email;
  byId('identity').querySelector('.avatar').textContent = Array.from((name || email).trim())[0]?.toUpperCase() || '?';
  byId('signout').hidden = !live;
  syncContinue();
}
function signout() {
  if (busy) return;
  clearIdentity(); unsubscribeToken = '';
  const withdraw = byId('withdraw'); if (withdraw) withdraw.hidden = true;
  message('Signed out of this browser. Existing CLI sessions are unchanged.');
}
async function refreshPreferences() {
  revision = null; syncContinue();
  const preferences = await api('/api/demo/preferences', undefined, true);
  if (!Number.isInteger(preferences.revision) || preferences.revision < 0 || typeof preferences.salesContact !== 'boolean' || typeof preferences.newsletter !== 'boolean') throw new Error('Preferences could not be verified. Please sign in again.');
  revision = preferences.revision; syncContinue();
  byId('commercial').checked = preferences.salesContact;
  byId('newsletter').checked = preferences.newsletter;
  byId('preferences-status').hidden = false;
  byId('preferences-status').textContent = revision ? 'Your saved preferences are shown. Review them before submitting changes.' : 'No previous preferences found. Both optional choices are off.';
}
function googleLogin(popup) {
  return new Promise(async (resolve, reject) => {
    let stream, clientId, provider, exchanging = false, ended = false;
    const redirectURL = `${location.origin}/api/oauth2-redirect`;
    const finish = (error, value) => { if (ended) return; ended = true; clearTimeout(timeout); cancelGoogleLogin = null; cancelGoogleButton.hidden = true; stream?.close(); popup.close(); error ? reject(error) : resolve(value); };
    const timeout = setTimeout(() => finish(new Error('Google sign-in timed out. Please try again.')), 180000);
    // COOP can sever the cross-origin popup reference and report closed even
    // while Google is open. PocketBase also closes its successful redirect before
    // the separate realtime callback necessarily arrives. Neither means cancel.
    cancelGoogleLogin = () => finish(new Error('Google sign-in cancelled. You can try again.'));
    cancelGoogleButton.hidden = false;
    try {
      const methods = await api('/api/collections/users/auth-methods');
      provider = methods.oauth2?.providers?.find(item => item.name === 'google');
      if (!provider || typeof provider.codeVerifier !== 'string') throw new Error('Google sign-in is not configured on this demo.');
      const authURL = new URL(provider.authURL);
      if (authURL.protocol !== 'https:' || authURL.hostname !== 'accounts.google.com' || authURL.username || authURL.password) throw new Error('Unexpected Google authorization configuration.');
      if (ended) return;
      stream = new EventSource('/api/realtime');
      stream.addEventListener('PB_CONNECT', async event => {
        try {
          if (clientId || ended) return;
          clientId = JSON.parse(event.data).clientId;
          if (typeof clientId !== 'string' || !clientId) throw new Error('Could not initialize Google sign-in.');
          await api('/api/realtime', {clientId, subscriptions:['@oauth2']});
          if (ended) return;
          authURL.searchParams.set('state', clientId); authURL.searchParams.set('redirect_uri', redirectURL);
          popup.location.href = authURL.href;
        } catch (error) { finish(error); }
      });
      stream.addEventListener('@oauth2', async event => {
        if (exchanging || ended) return;
        exchanging = true;
        try {
          const payload = JSON.parse(event.data);
          if (payload.state !== clientId || !payload.code || payload.error) throw new Error('Google sign-in was not completed. Please try again.');
          const result = await api('/api/collections/users/auth-with-oauth2', {provider:'google', code:payload.code, codeVerifier:provider.codeVerifier, redirectURL});
          if (ended) return;
          if (!result.token || !result.record?.id || result.record.collectionName !== 'users' || !result.record.email) throw new Error('The signed-in account could not be verified.');
          finish(null, {token:result.token, record:result.record});
        } catch (error) { finish(error); }
      });
      stream.onerror = () => finish(new Error('Google sign-in connection interrupted. Please try again.'));
    } catch (error) { finish(error); }
  });
}
function showOnboarding() {
  byId('onboarding').hidden = false;
  byId('onboarding').focus({preventScroll:true});
  byId('onboarding').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
}
googleButton.addEventListener('click', async () => {
  if (busy) return;
  if (!live) { setIdentity('demo@example.com · synthetic identity','Demo visitor'); message('Fictional identity selected. Review the choices above.'); byId('terms').focus(); return; }
  const popup = window.open('about:blank', '_blank', 'popup,width=520,height=720');
  if (!popup) { message('Allow the sign-in popup, then try again.'); return; }
  setBusy(true); googleButton.disabled = true; message('Complete Google sign-in in the popup, or cancel here to try again.');
  const epoch = authEpoch;
  try {
    const result = await googleLogin(popup);
    if (epoch !== authEpoch) return;
    token = result.token; account = result.record;
    await refreshPreferences();
    if (epoch !== authEpoch || Date.now() >= Date.parse(live.resetAt)) { if (epoch === authEpoch) clearIdentity(); return; }
    saveAuth(token, account); setIdentity(account.email, account.name || 'Google account');
    message('Signed in. Review the terms and your optional choices.'); byId('terms').focus();
  } catch (error) { if (epoch === authEpoch) clearIdentity(); message(error.message); }
  finally { setBusy(false); googleButton.disabled = false; }
});
byId('signout').addEventListener('click', signout);
byId('enrollment-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return;
  if (!signedIn) { message(live ? 'Sign in with Google before continuing.' : 'First simulate Google sign-in. No Google account is needed.'); googleButton.focus(); return; }
  if (!canContinue() || !byId('terms').checked) return;
  if (!live) { message('Preview opened. No account or preferences have been saved.'); showOnboarding(); return; }
  setBusy(true); const submit = document.querySelector('.continue-button'); submit.disabled = true;
  try {
    const result = await api('/api/demo/enroll', {generation:live.generation, termsVersion:live.termsVersion, acceptTerms:true,
      salesConsent:byId('commercial').checked, newsletterConsent:byId('newsletter').checked, expectedRevision:revision}, true);
    if (result.enrolled !== true || result.generation !== live.generation || !Number.isInteger(result.contact?.revision)) throw new Error('Enrollment could not be verified. Refresh your preferences before retrying.');
    revision = result.contact.revision; unsubscribeToken = result.contact.unsubscribeToken || '';
    message('Demo enrollment saved. Your optional contact preferences have been recorded. No email has been sent.');
    byId('guide-intro').textContent = clientAvailable() ? 'You are enrolled for this demo day. Use the verified demo-compatible client.' : 'Enrollment saved. Public client distribution pending — these are planned instructions only.';
    byId('guide-banner').querySelector('p').textContent = clientAvailable() ? 'Google sign-in does not unlock your files. Enter your vault passphrase only in your private terminal. The daily reset removes your demo account and vault data.' : 'The public skill installation contract is unavailable on this server. Do not run these examples against a real service or use production credentials. Your preferences are saved; vault onboarding is not yet available.';
    // Quote identity safely for shell snippets; never interpolate untrusted identity as code.
    const shellQuote = value => "'" + value.replaceAll("'", "'\\''") + "'";
    if (clientAvailable()) byId('connect-code').textContent = `export VAULTCONTEXT_URL=${shellQuote(location.origin)}\nexport VAULTCONTEXT_USER_EMAIL=${shellQuote(account.email)}\nvaultcontext login\nvaultcontext whoami\nvaultcontext check\nvaultcontext init\nvaultcontext unlock --timeout 900\nvaultcontext create 'Sample vault'`;
    byId('guide-complete').textContent = 'All three steps reviewed. Verify each operation in your terminal; the page cannot confirm CLI success.';
    if (unsubscribeToken) {
      let withdraw = byId('withdraw');
      if (!withdraw) { withdraw = document.createElement('button'); withdraw.id = 'withdraw'; withdraw.type = 'button'; withdraw.className = 'link-button'; withdraw.textContent = 'Withdraw both marketing permissions'; byId('enrollment-form').after(withdraw);
        withdraw.addEventListener('click', async () => {
          if (busy || !unsubscribeToken) return; setBusy(true); withdraw.disabled = true;
          try {
            const result = await api('/api/demo/unsubscribe', {token:unsubscribeToken});
            if (result.ok !== true) throw new Error('Withdrawal could not be confirmed. Please try again.');
            byId('commercial').checked = false; byId('newsletter').checked = false;
            message('Both marketing permissions withdrawn.');
            try { await refreshPreferences(); }
            catch {
              revision = null;
              byId('preferences-status').hidden = false;
              byId('preferences-status').textContent = 'Withdrawal succeeded. The account view could not refresh; sign in again before saving any new preferences.';
            }
          } catch (error) { message(error.message); }
          finally { setBusy(false); withdraw.disabled = false; }
        });
      }
      withdraw.hidden = false;
    }
    showOnboarding();
  } catch (error) {
    if (error.status === 401) revision = null;
    message(error.message);
    if (error.status === 409) { try { await refreshPreferences(); message('The latest preferences are shown. Review and submit again; nothing was retried automatically.'); } catch { message('Could not refresh preferences. Sign out and sign in again.'); } }
  } finally { setBusy(false); updateCountdown(); }
});
// This capability works independently of demo-day status and Google account lifetime.
byId('confirm-withdrawal').addEventListener('click', async () => {
  if (!linkWithdrawalToken || byId('confirm-withdrawal').disabled) return;
  const button = byId('confirm-withdrawal'); button.disabled = true;
  byId('withdrawal-status').textContent = 'Processing your withdrawal request…';
  try {
    const result = await api('/api/demo/unsubscribe', {token:linkWithdrawalToken});
    if (result.ok !== true) throw new Error('Withdrawal could not be confirmed. Please try again.');
    linkWithdrawalToken = '';
    byId('withdrawal-status').textContent = 'Withdrawal request processed. Any matching commercial email and newsletter permissions are now off.';
    button.textContent = 'Withdrawal processed';
  } catch {
    byId('withdrawal-status').textContent = 'Withdrawal could not be completed. Your link is still available in this tab; try again when the service is reachable.';
    button.disabled = false;
  }
});
// Status failures retain a plainly labelled preview; they never fabricate live enrollment.
async function discoverLive() {
  if (!/^https?:$/.test(location.protocol)) return;
  try {
    const data = await api('/api/demo/status'); if (!validStatus(data)) return;
    ({default: PocketBase, BaseAuthStore} = await import('./vendor/pocketbase.es.mjs?v=0.28.1'));
    if (signedIn) signout();
    live = data;
    // Default LocalAuthStore where available; private/blocked storage stays usable in memory.
    try {
      const probe = 'vaultcontext_storage_probe';
      localStorage.setItem(probe, '1'); localStorage.removeItem(probe);
      pb = new PocketBase(location.origin);
    } catch { pb = new PocketBase(location.origin, new BaseAuthStore()); }
    window.addEventListener('storage', event => {
      if (event.key === null) {
        clearIdentity(false); unsubscribeToken = '';
        const withdraw = byId('withdraw'); if (withdraw) withdraw.hidden = true;
        message('Browser storage cleared. Sign in again.');
      }
    });
    pb.authStore.onChange(() => {
      if (writingAuth) return;
      cancelGoogleLogin?.();
      clearIdentity(false); unsubscribeToken = '';
      const withdraw = byId('withdraw'); if (withdraw) withdraw.hidden = true;
      message(pb.authStore.token ? 'Your session changed in another tab. Refresh to continue.' : 'Signed out in another tab.');
    });
    document.title = 'Try VaultContext · Public demo';
    document.querySelector('meta[name="description"]').content = 'Try client-encrypted file storage with your Google account. Demo vaults reset daily at 00:00 UTC.';
    byId('audience-description').textContent = 'Try the demo with a verified Google account, including personal Gmail and Google Workspace accounts.';
    byId('terms-help').textContent = 'Required to try the demo';
    byId('privacy-label').textContent = 'privacy & retention policy';
    byId('terms-details').querySelector('div').innerHTML = '<p>Use synthetic or disposable sample files only. Do not upload real credentials, confidential information or other people’s personal data. The demo is temporary, has no storage guarantee, and vault data resets at 00:00 UTC daily.</p><p>Commercial contact and newsletter subscription are separate, optional choices. Neither is required to try the demo. <a href="terms/">Read the full terms and operator details.</a></p>';
    byId('privacy-details').querySelector('dl').innerHTML = '<dt>Vault data</dt><dd>Deleted daily at 00:00 UTC, including demo accounts, encrypted files and keys. This does not erase your local downloads.</dd><dt>Email without marketing consent</dt><dd>Retained separately for up to 30 days after your last demo use.</dd><dt>Marketing preferences and contact details</dt><dd>Retained separately from vault data, reviewed after 12 months, with an option to withdraw consent. Minimal suppression records may remain to honor withdrawal.</dd><dt>Operational logs</dt><dd>Retained for up to 30 days, excluding file content, passphrases and credentials. A documented security incident may require a limited investigation period.</dd><dt>Isolated contact and security backups</dt><dd>Recovery copies may remain for up to an additional 30 days after live retention ends. They are restricted to recovery. Withdrawals and deletion requests must be reconciled before any sending. Demo vault replicas remain subject to the daily reset.</dd>';
    byId('reset-details').querySelector('div > p').textContent = 'Demo accounts, encrypted keys and vault files reset daily at 00:00 UTC. You may have less than 24 hours until the next reset. The countdown uses the deadline supplied by the demo server. Contact details and preferences are retained separately.';
    byId('mode-banner').replaceChildren();
    const badge = document.createElement('span'); badge.className = 'preview-tag'; badge.textContent = 'PUBLIC DEMO';
    const note = document.createElement('span'); note.textContent = 'Vaults reset at 00:00 UTC · Sample files only.'; byId('mode-banner').append(badge,note);
    byId('mode-note').innerHTML = '<strong>A temporary evaluation space.</strong><p>Google sign-in creates a demo identity. Vaults reset daily; your separately stored contact preferences follow the privacy policy.</p>';
    byId('mode-pill').textContent = 'Daily reset';
    renderInstallation();
    document.querySelector('.card-intro').textContent = 'Sign in, review the terms, and choose your preferences.';
    byId('policy-intro').textContent = 'Your demo account and vault reset daily. Contact preferences have their own retention period.';
    byId('privacy-mode-note').textContent = 'Your contact preferences are saved separately from daily vault data. Use the withdrawal control after enrollment, or sign in again to review and change your choices.';
    googleButton.replaceChildren(); const g = document.createElement('span'); g.className='google-g'; g.textContent='G'; googleButton.append(g,document.createTextNode('Continue with Google'));
    byId('google-note').textContent = 'Sign in with your Google account. Your organization may restrict access.';
    document.querySelector('.continue-button').replaceChildren(document.createTextNode('Accept terms & continue'), document.querySelector('.nav-cta .ui-icon').cloneNode(true));
    byId('countdown-note').textContent = 'Reset time supplied by the demo server';
    byId('reset-description').textContent = 'Demo vault data clears every day at 00:00 UTC. Use sample files only.';
    message('Sign in with Google to continue.'); updateCountdown();
    await restoreSession();
  } catch { /* Unconnected static preview intentionally remains useful. */ }
}
updateCountdown(); setInterval(updateCountdown,1000); discoverLive();
function updateProgress() {
  const complete = document.querySelectorAll('.step-done:checked').length;
  byId('progress-label').textContent = `${complete} of 3 guide steps reviewed`;
  byId('progress-bar').style.width = `${complete / 3 * 100}%`;
  byId('guide-complete').hidden = complete !== 3;
}
document.querySelectorAll('.step-done').forEach(input => input.addEventListener('change',updateProgress));
byId('reset-progress').addEventListener('click',()=>{ document.querySelectorAll('.step-done').forEach(input=>{input.checked=false;});updateProgress(); });
document.querySelectorAll('.copy').forEach(button=>button.addEventListener('click',async()=>{
  const code=button.nextElementSibling.querySelector('code');
  try { if(!navigator.clipboard)throw new Error(); await navigator.clipboard.writeText(code.textContent);button.textContent='Copied';byId('copy-status').textContent='Instructions copied. No commands have been run.';setTimeout(()=>{button.textContent=code.id === 'install-code' ? 'Copy instructions' : 'Copy';},2000); }
  catch { const selection=window.getSelection(),range=document.createRange();range.selectNodeContents(code);selection.removeAllRanges();selection.addRange(range);button.textContent='Select & copy';byId('copy-status').textContent='Clipboard unavailable. Instructions selected; copy them manually.'; }
}));
document.querySelectorAll('a[href="#terms-details"],a[href="#privacy-details"]').forEach(link=>link.addEventListener('click',()=>{document.querySelector(link.getAttribute('href')).open=true;}));
