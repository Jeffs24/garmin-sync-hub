// Liftosaur "Sync now": syncs one workout per request until nothing is left.
// Expects #sync-now-btn, #sync-progress, #sync-progress-text and #sync-result;
// #sync-stop-btn, #sync-progress-bar and #stat-pending are optional.
let _syncStopped = false;

function stopSync() {
    _syncStopped = true;
    const stopBtn = document.getElementById('sync-stop-btn');
    if (stopBtn) stopBtn.style.display = 'none';
}

async function syncNow() {
    _syncStopped = false;
    const btn = document.getElementById('sync-now-btn');
    const stopBtn = document.getElementById('sync-stop-btn');
    const progress = document.getElementById('sync-progress');
    const text = document.getElementById('sync-progress-text');
    const bar = document.getElementById('sync-progress-bar');
    const result = document.getElementById('sync-result');
    const setBar = (pct) => { if (bar) bar.style.width = pct + '%'; };

    btn.disabled = true;
    if (stopBtn) stopBtn.style.display = '';
    progress.style.display = 'block';
    result.innerHTML = '';
    text.textContent = 'Starting sync…';
    setBar(0);

    let total = 0;
    let synced = 0;
    let errors = 0;

    while (!_syncStopped) {
        try {
            const resp = await fetch('/api/sync-one', { method: 'POST' });
            const data = await resp.json();

            if (data.busy) {
                text.textContent = 'Another sync is already running. Wait for it to finish.';
                break;
            }

            if (data.error && !data.title) {
                errors++;
                const err = data.error;
                if (data.eu_consent) {
                    text.textContent = 'Garmin requires upload consent.';
                    result.innerHTML = '<div class="toast toast-error">Enable Device Upload in <a href="https://connect.garmin.com/modern/settings" target="_blank" rel="noopener" style="color: inherit; text-decoration: underline;">Garmin settings</a> → Data, then try again.</div>';
                } else if (err.includes('Liftosaur API key')) {
                    text.textContent = 'Your Liftosaur API key is invalid or expired.';
                    result.innerHTML = '<div class="toast toast-error">Liftosaur API key invalid. <a href="/settings#liftosaur" style="color: inherit; text-decoration: underline;">Enter a new key</a></div>';
                } else if (err.includes('Login failed') || err.includes('auth') || err.includes('OAuth') || err.includes('token')) {
                    text.textContent = 'Your Garmin connection expired.';
                    result.innerHTML = '<div class="toast toast-error">Garmin connection expired. <a href="/settings#garmin" style="color: inherit; text-decoration: underline;">Reconnect Garmin</a></div>';
                } else {
                    text.textContent = 'Sync error. ' + (synced > 0 ? synced + ' synced before the error.' : 'Try again.');
                }
                break;
            }

            if (data.done || (data.synced === 0 && data.remaining <= 0)) {
                text.textContent = synced > 0 ? 'All caught up.' : 'Everything is already synced.';
                setBar(100);
                break;
            }

            if (data.synced > 0) {
                synced++;
                if (total === 0) total = synced + (data.remaining || 0);
                const verb = data.skipped_error ? 'Skipped (error)' : data.skipped_duplicate ? 'Skipped' : 'Synced';
                text.textContent = verb + ' "' + data.title + '" (' + synced + '/' + total + ')';
                setBar(total > 0 ? Math.round((synced / total) * 100) : 0);
                const pendingEl = document.getElementById('stat-pending');
                if (pendingEl && data.remaining >= 0) pendingEl.textContent = data.remaining;
            } else if (data.remaining > 0) {
                continue;
            } else {
                break;
            }
        } catch (e) {
            errors++;
            text.textContent = 'Network error. ' + (synced > 0 ? synced + ' synced so far. ' : '') + 'Try again.';
            break;
        }
    }

    if (_syncStopped && synced > 0) {
        text.textContent = 'Paused after ' + synced + ' workouts. Click Sync now to resume.';
    }

    progress.style.display = 'none';
    if (stopBtn) stopBtn.style.display = 'none';
    btn.disabled = false;

    if (synced > 0) {
        result.innerHTML = '<div class="toast toast-success">' + synced + ' workout' + (synced > 1 ? 's' : '') + ' synced to Garmin.</div>';
        setTimeout(function () { location.reload(); }, 2000);
    } else if (errors > 0 && !result.innerHTML) {
        result.innerHTML = '<div class="toast toast-error">' + text.textContent + '</div>';
    } else if (!result.innerHTML) {
        result.innerHTML = '<div class="toast" style="background: rgba(100,116,139,0.15); border: 1px solid rgba(100,116,139,0.3);">Everything is already synced.</div>';
    }
}
