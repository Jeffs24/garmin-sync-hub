// Shared Garmin login (setup and settings pages). Expects #garmin_email,
// #garmin_password, #garmin-login-result, #garmin-mfa-box and #garmin_mfa_code.
let pendingGarminLoginId = '';

async function startGarminLogin() {
    const email = document.getElementById('garmin_email').value.trim();
    const password = document.getElementById('garmin_password').value;
    const result = document.getElementById('garmin-login-result');
    const mfaBox = document.getElementById('garmin-mfa-box');
    if (!email || !password) {
        result.innerHTML = '<span style="font-size: 12px; color: var(--red);">Enter your Garmin email and password first.</span>';
        return;
    }
    result.innerHTML = '<span style="font-size: 12px; color: var(--text-muted);">Connecting to Garmin… this can take up to a minute.</span>';
    mfaBox.style.display = 'none';
    try {
        const response = await fetch('/api/garmin/login/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password })
        });
        const data = await response.json();
        if (!response.ok) {
            result.innerHTML = '<span style="font-size: 12px; color: var(--red);">' + (data.error || 'Garmin login failed') + '</span>';
            return;
        }
        if (data.status === 'needs_mfa') {
            pendingGarminLoginId = data.login_id;
            result.innerHTML = '<span style="font-size: 12px; color: var(--text-muted);">Garmin sent a verification code. Enter it below.</span>';
            mfaBox.style.display = 'block';
            return;
        }
        result.innerHTML = '<span style="font-size: 12px; color: var(--accent);">&#10003; Garmin connected.</span>';
    } catch (e) {
        result.innerHTML = '<span style="font-size: 12px; color: var(--red);">&#10007; Network error, try again.</span>';
    }
}

async function finishGarminLogin() {
    const result = document.getElementById('garmin-login-result');
    const code = document.getElementById('garmin_mfa_code').value.trim();
    if (!pendingGarminLoginId || !code) {
        result.innerHTML = '<span style="font-size: 12px; color: var(--red);">Enter the verification code first.</span>';
        return;
    }
    result.innerHTML = '<span style="font-size: 12px; color: var(--text-muted);">Verifying code...</span>';
    try {
        const response = await fetch('/api/garmin/login/finish', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ login_id: pendingGarminLoginId, mfa_code: code })
        });
        const data = await response.json();
        if (!response.ok) {
            result.innerHTML = '<span style="font-size: 12px; color: var(--red);">' + (data.error || 'Verification failed') + '</span>';
            return;
        }
        pendingGarminLoginId = '';
        document.getElementById('garmin-mfa-box').style.display = 'none';
        result.innerHTML = '<span style="font-size: 12px; color: var(--accent);">&#10003; Garmin connected.</span>';
    } catch (e) {
        result.innerHTML = '<span style="font-size: 12px; color: var(--red);">&#10007; Network error, try again.</span>';
    }
}
