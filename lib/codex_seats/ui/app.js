/* Local, cache-backed manager. Only explicit actions POST; polling only reads. */
(() => {
  'use strict';
  const root = document.getElementById('codex-seats');
  const content = document.getElementById('content');
  const sessionKey = 'codex-seats-session';
  let session = '';
  if (typeof location !== 'undefined') {
    const fragment = new URLSearchParams(location.hash.slice(1)).get('session');
    if (fragment) {
      session = fragment;
      try { sessionStorage.setItem(sessionKey, session); } catch (_) { /* memory still works */ }
      history.replaceState(history.state, '', location.pathname + location.search);
    } else {
      try { session = sessionStorage.getItem(sessionKey) || ''; } catch (_) { /* no stored session */ }
    }
  }
  // Synthetic fixtures can supply a fake token without a launch fragment.
  if (!session) session = document.querySelector('meta[name="codex-seats-session"]')?.content || '';
  const requests = new Set();
  const terminalStates = new Set(['done', 'error', 'canceled']);
  const profiles = [['unverified', 'Unverified'], ['personal-free', 'Free'], ['personal-go', 'Go'], ['personal-plus', 'Plus'], ['personal-pro-5x', 'Pro 5x'], ['personal-pro-20x', 'Pro 20x'], ['business-standard', 'Business Standard'], ['business-premium', 'Business Premium']];
  let state = null, stateError = '', page = 'inventory', selected = null;
  let settingsDraft = null, claudeDraft = null, settingsDirty = false, claudeDirty = false;
  let seatForm = null, roleChoice = null, operation = null, operationRequest = null, postPending = false;
  let confirmSent = false, operationError = '', operationPaused = false, operationReads = 0;
  let stateTimer, operationTimer, stateReading = false, operationReading = false, stopped = false;
  let stateReadAgain = false, stateGeneration = 0;
  let refreshPending = false, resetDraftAfterRead = null;
  const notifications = [];
  let notificationId = 0;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const known = value => value === null || value === undefined || value === '' ? 'Unknown' : String(value);
  const number = value => typeof value === 'number' && Number.isFinite(value);
  const list = value => Array.isArray(value) ? value : [];
  const hasWork = value => Array.isArray(value) ? value.length > 0 : !!value;
  const busy = () => postPending || (operation && !terminalStates.has(operation.status));
  const selectedSeat = () => state?.seats.find(s => selected === `codex:${s.id}`);
  const selectedLocal = () => state?.local_seats.find(s => selected === `local:${s.id}`);
  const button = (label, action, disabled = false, primary = false) => `<button type="button" id="action-${esc(action)}" class="cs-button${primary ? ' cs-primary' : ''}" data-action="${esc(action)}" ${disabled ? 'disabled' : ''}>${esc(label)}</button>`;
  const facts = pairs => `<dl class="cs-facts">${pairs.map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(known(v))}</dd>`).join('')}</dl>`;
  const notice = text => text ? `<div class="cs-notice">${esc(text)}</div>` : '';
  function date(value) {
    if (value === null || value === undefined || value === '') return 'Not reported';
    const d = new Date(number(value) ? value * 1000 : value);
    return Number.isNaN(d.getTime()) ? 'Unknown date' : d.toLocaleString();
  }
  function countdown(value) {
    if (value === null || value === undefined || value === '') return 'Reset time unknown';
    const d = new Date(number(value) ? value * 1000 : value).getTime();
    if (!Number.isFinite(d)) return 'Reset time unknown';
    const minutes = Math.ceil((d - Date.now()) / 60000);
    if (minutes <= 0) return 'Reset time passed · awaiting observation';
    return `Resets in ${minutes >= 1440 ? `${Math.floor(minutes / 1440)}d ${Math.floor(minutes % 1440 / 60)}h` : minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`}`;
  }
  function duration(window) {
    const m = window.window_minutes;
    if (!number(m) || m <= 0) return `${known(window.name)} · duration unknown`;
    return `${m % 1440 === 0 ? `${m / 1440}-day` : m % 60 === 0 ? `${m / 60}-hour` : `${m}-minute`} window`;
  }
  function quota(window, accountFresh = true) {
    const value = window.remaining_pct;
    const valid = number(value) && value >= 0 && value <= 100;
    const fresh = accountFresh && window.freshness === 'fresh';
    return `<section><h3>${esc(duration(window))}</h3><div class="cs-quotavalue"><strong>${valid ? esc(value) + '%' : 'Unknown'}</strong>${valid ? '<span class="cs-small cs-muted">remaining</span>' : ''}</div>${valid ? `<meter class="cs-meter${fresh ? '' : ' cs-stale'}" aria-label="${esc(duration(window))} remaining${fresh ? '' : ' (last observation)'}" min="0" max="100" value="${value}">${value}%</meter>` : ''}<p class="cs-small cs-muted">${esc(fresh ? 'Fresh' : window.freshness === 'fresh' ? 'Account stale / unknown' : known(window.freshness))} · ${esc(countdown(window.resets_at))}</p><p class="cs-small cs-muted">Observed ${esc(date(window.observed_at))}</p></section>`;
  }
  function terminal(seat, local = false) {
    return `<p class="cs-small cs-muted">${local ? 'Advanced local configuration is available in the terminal manager.' : 'For interactive sign-in, open the terminal manager and choose this seat. Then reactivate it here.'}</p><code class="cs-terminal-command">codex-seat dash --manage</code><p class="cs-small">Choose ${esc(seat.label || seat.id)} · ${esc(seat.id)}</p>`;
  }
  function announce(message) { document.getElementById('announcer').textContent = message; }
  function notify(message) {
    notifications.splice(0, notifications.length, {id: ++notificationId, message});
    renderNotifications(); announce(message);
  }
  function renderNotifications() {
    document.getElementById('notifications').innerHTML = notifications.map(n => `<div class="cs-notice"><p>${esc(n.message)}</p><button type="button" class="cs-button" data-dismiss="${n.id}" aria-label="Dismiss notification">Dismiss</button></div>`).join('');
  }
  async function api(path, body) {
    const controller = new AbortController(); requests.add(controller);
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      if (!session || session === '__SESSION_TOKEN__') throw new Error('Session unavailable. Reopen the launch link from your terminal.');
      const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', headers: {'X-Codex-Seats-Session': session, ...(body === undefined ? {} : {'Content-Type': 'application/json'})}, body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal});
      const data = await response.json().catch(() => { throw new Error(`Invalid JSON response (HTTP ${response.status})`); });
      if (!response.ok) throw new Error(`HTTP ${response.status}: ${data.error || 'Request failed'}`);
      return data;
    } finally { clearTimeout(timeout); requests.delete(controller); }
  }
  function preserveDOM(target, html) {
    const active = document.activeElement;
    const id = target.contains(active) ? active.id : null;
    const summaryId = target.contains(active) && active.tagName === 'SUMMARY' ? active.parentElement.id : null;
    const selection = active?.tagName === 'INPUT' && active.type === 'text' ? [active.selectionStart, active.selectionEnd] : null;
    const open = [...target.querySelectorAll('details[open]')].map(d => d.id);
    target.innerHTML = html;
    open.forEach(id => { const d = document.getElementById(id); if (d) d.open = true; });
    if (id) {
      const replacement = document.getElementById(id);
      if (replacement && !replacement.disabled) { replacement.focus({preventScroll:true}); if (selection) replacement.setSelectionRange(...selection); }
    }
    if (summaryId) document.getElementById(summaryId)?.querySelector('summary')?.focus({preventScroll:true});
  }
  function appearance() {
    const p = state?.policy || {};
    document.documentElement.style.colorScheme = ['light','dark'].includes(p['ui.theme']) ? p['ui.theme'] : 'light dark';
    root.dataset.density = p['ui.density'] === 'compact' ? 'compact' : 'comfortable';
    root.dataset.accent = p['ui.accent'] === 'indigo' ? 'indigo' : 'teal';
  }
  function claudeValues() {
    const c = state.claude;
    return {mode:c.configured_mode, subscription_tier:c.subscription_tier?.configured ?? null, active_usage_refresh:c.active_usage_refresh, small_task_min_headroom_pct:c.headroom?.small_min_pct, big_task_min_headroom_pct:c.headroom?.big_min_pct};
  }
  function differences(before, after) { return Object.fromEntries(Object.entries(after).filter(([key,value]) => value !== before[key])); }
  function delta(before, after) {
    const changes = Object.entries(differences(before,after));
    return `<h3>Change preview</h3>${changes.length ? `<ul>${changes.map(([key,value]) => `<li><span class="cs-mono">${esc(key)}</span>: ${esc(known(before[key]))} → ${esc(value === null ? 'Clear' : known(value))}</li>`).join('')}</ul>` : '<p class="cs-small cs-muted">No changes to save.</p>'}`;
  }
  async function readState() {
    if (stopped || document.hidden) return;
    if (stateReading) { stateReadAgain = true; return; }
    clearTimeout(stateTimer); stateReading = true;
    const generation = stateGeneration;
    try {
      const next = await api('/api/state');
      if (generation !== stateGeneration) { stateReadAgain = true; return; }
      if (next.version !== 1 || !Array.isArray(next.seats) || !Array.isArray(next.local_seats) || !next.policy || !next.claude) throw new Error('Invalid state response; registry cannot be determined.');
      state = next; stateError = '';
      if (!settingsDirty || resetDraftAfterRead === 'settings') { settingsDraft = {...state.policy}; settingsDirty = false; }
      if (!claudeDirty || resetDraftAfterRead === 'claude') { claudeDraft = claudeValues(); claudeDirty = false; }
      resetDraftAfterRead = null;
      if (!selectedSeat() && !selectedLocal()) selected = state.seats[0] ? `codex:${state.seats[0].id}` : state.local_seats[0] ? `local:${state.local_seats[0].id}` : null;
      appearance(); render();
    } catch (error) {
      if (!stopped) { const message = `Could not read cached state: ${error.message}`; if (message !== stateError) announce(message); stateError = message; render(); }
    } finally {
      stateReading = false;
      if (!stopped && !document.hidden) stateTimer = setTimeout(readState, stateReadAgain ? 0 : 5000);
      stateReadAgain = false;
    }
  }
  function resetReason(s) {
    if (stateError) return 'Cached state could not be read. Retry before preparing a reset.';
    if (!['active','draining'].includes(s.lifecycle)) return 'Reactivate this seat before redeeming a reset.';
    if (s.busy !== false) return 'A reset requires an idle seat; busy or unknown availability blocks redemption.';
    if (s.freshness !== 'fresh') return 'Refresh this seat: account observations are stale or unknown.';
    if (s.identity?.status !== 'verified' && s.identity?.status !== 'fresh') return 'Fresh verified account identity is required. Check sign-in in the terminal manager.';
    if (!s.identity.email) return 'Account identity is unknown. Refresh this seat before preparing a reset.';
    const bank = s.banked_resets || {};
    if (bank.status !== 'fresh') return 'Refresh this seat: reset credit observations are stale or unknown.';
    if (!number(bank.available_count) || bank.available_count < 1) return 'No known available reset credit.';
    if (!list(bank.credits).some(c => c.fingerprint && c.status === 'available' && (!c.expires_at || new Date(number(c.expires_at) ? c.expires_at * 1000 : c.expires_at).getTime() > Date.now()))) return 'No identified available credit. Refresh or inspect provider details.';
    return '';
  }
  function seatStatus(s) { return [known(s.lifecycle), s.busy === true ? 'Busy' : s.busy === false ? 'Idle' : 'Availability unknown', known(s.health), `Identity: ${known(s.identity?.status)}`, known(s.freshness)].join(' · '); }
  function inventory() {
    const seat = selectedSeat(), local = selectedLocal();
    const rows = state.seats.map((s,i) => `<button type="button" id="seat-${i}" class="cs-seatrow" data-select="${esc(`codex:${s.id}`)}" aria-pressed="${s === seat}"><span class="cs-seatname"><strong>${esc(s.label || s.id)}</strong><span class="cs-seatid">${esc(s.id)}</span></span><small>${esc(known(s.role))} · ${esc(seatStatus(s))}</small></button>`).join('');
    const localRows = state.local_seats.map((s,i) => `<button type="button" id="local-${i}" class="cs-seatrow" data-select="${esc(`local:${s.id}`)}" aria-pressed="${s === local}"><span class="cs-seatname"><strong>${esc(s.label || s.id)}</strong><span class="cs-seatid">${esc(s.id)}</span></span><small>${esc(known(s.host))} · ${esc(known(s.status))} · ${esc(known(s.lifecycle))}${s.busy ? ' · Busy' : ''}</small></button>`).join('');
    return `<div class="cs-pagehead"><div><p class="cs-overline">Workspace / accounts</p><h1>Inventory</h1></div>${button('Add Codex seat','add',busy() || !!stateError)}</div>${seatForm ? seatEditor() : ''}<div class="cs-workspace"><aside class="cs-seatlist" aria-label="Seats"><div class="cs-listlabel"><span class="cs-overline">Codex seats</span><span class="cs-small">${state.seats.length}</span></div><div class="cs-seatrows">${rows || '<p class="cs-small cs-muted">No Codex seats registered.</p>'}</div><div class="cs-listdivider cs-overline">Claude</div><button type="button" class="cs-seatrow" data-page="claude"><strong>Claude account</strong><small>${esc(known(state.claude.mode))} · ${esc(known(state.claude.status?.status))}</small></button><div class="cs-listdivider cs-overline">Local seats</div><div class="cs-seatrows">${localRows || '<p class="cs-small cs-muted">No local seats registered.</p>'}</div></aside><section class="cs-seatdetail" aria-label="Selected seat">${seat ? seatDetail(seat) : local ? localDetail(local) : '<div class="cs-empty"><h2>Add your first Codex seat</h2><p>Register an account, then sign in through the terminal manager and reactivate it.</p>' + button('Add Codex seat','add-empty',busy() || !!stateError,true) + '</div>'}</section></div>`;
  }
  function localDetail(s) {
    return `<div class="cs-detailhead"><h2>${esc(s.label || s.id)}</h2><span class="cs-status cs-off">${esc(known(s.status))}</span></div>${facts([['Seat',s.id],['Placement / host',s.host],['Provider',s.provider],['Lifecycle',s.lifecycle],['Mode',s.mode],['Availability',s.busy === true ? 'Busy' : s.busy === false ? 'Idle' : 'Unknown'],['Reason',s.reason]])}${terminal(s,true)}`;
  }
  function seatDetail(s) {
    const bank = s.banked_resets || {}, identity = s.identity || {}, capacity = s.capacity || {};
    const reason = resetReason(s), roleGuard = ['primary','deputy'].includes(s.role);
    const active = ['active','draining'].includes(s.lifecycle);
    const firstActivation = !state.seats.some(seat => ['active','draining'].includes(seat.lifecycle));
    const locked = busy() || !!stateError;
    return `<div class="cs-detailhead"><div><h2>${esc(s.label || s.id)}</h2><p class="cs-small cs-muted">${esc(identity.email || 'Account identity unknown')} · ${esc(s.id)}</p></div><span class="cs-status ${s.freshness === 'fresh' && !stateError ? '' : 'cs-stale'}">${esc(known(s.health))}</span></div><div class="cs-detailmeta cs-small"><span>${esc(known(s.role))}</span><span>${esc(seatStatus(s))}</span>${s.task ? `<span>Task: ${esc(typeof s.task === 'string' ? s.task : s.task.description || s.task.id || 'In progress')}</span>` : ''}</div>${notice(identity.reason)}<div class="cs-quotahead"><h3>Usage</h3><span class="cs-small cs-muted">Observed ${esc(date(s.observed_at))}</span></div><div class="cs-quotas">${list(s.windows).map(w => quota(w,s.freshness === 'fresh' && ['fresh','verified'].includes(identity.status) && !stateError)).join('') || '<p class="cs-muted">Usage unknown · no observed windows.</p>'}</div><div class="cs-bank"><div><h3>Banked resets</h3><p class="cs-small cs-muted">Provider credits · separate from quota and billing</p><p class="cs-small">${esc(known(bank.status))} · observed ${esc(date(bank.observed_at))}</p></div><strong class="cs-bankcount">${number(bank.available_count) ? esc(bank.available_count) : 'Unknown'}</strong></div>${notice(bank.last_error)}<div class="cs-actions">${button(refreshPending ? 'Refresh queued…' : 'Refresh selected','refresh',refreshPending || locked)}${button('Prepare reset','redeem-reset',!!reason || locked)}${active ? button(s.busy ? 'Pause / drain' : 'Pause','pause',locked || roleGuard) : button(firstActivation ? 'Reactivate as primary' : 'Reactivate',firstActivation ? 'reactivate-primary' : 'reactivate',locked || s.lifecycle === 'retired' || s.busy !== false)}</div><p id="reset-reason" class="cs-reason">${esc(reason || 'Preparing checks the account and credit again before you confirm.')}</p>${roleGuard ? '<p class="cs-reason">To pause or remove a primary / deputy seat, assign or clear its role first.</p>' : ''}${!active || !['verified','fresh'].includes(identity.status) ? `<details id="login-${esc(s.id)}"><summary>Sign-in / next steps</summary>${terminal(s)}</details>` : ''}<details id="advanced-${esc(s.id)}"><summary>More / advanced</summary>${facts([['Observed plan',identity.observed_plan_type],['Identity status',identity.status],['Configured plan',capacity.label || capacity.profile],['Configured relative capacity',capacity.relative_capacity],['Capacity source',capacity.source]])}<h3>Provider-reported credit details</h3>${list(bank.credits).map(c => facts([['Credit',c.title || c.fingerprint],['Status',c.status],['Granted',date(c.granted_at)],['Expires',c.expiry_status === 'none' ? 'No expiry reported by provider' : date(c.expires_at)],['Expiry status',c.expiry_status]])).join('') || '<p class="cs-small cs-muted">Individual credit details unavailable.</p>'}<div class="cs-actions">${button('Edit seat','edit',locked)}${button('Remove seat','remove-seat',locked || s.lifecycle !== 'inactive' || s.busy !== false || roleGuard)}</div><h3>Role</h3>${field('role','choice','Assign role',roleChoice || s.role,['primary','deputy','worker'],'Preview this role change separately from seat settings. Active seats only.')}${button('Review role change','set-role',locked || s.lifecycle !== 'active' || (roleChoice || s.role) === s.role)}<p class="cs-reason">Removal retires only an inactive, idle seat without a primary or deputy role. History and the seat home are preserved.</p>${terminal(s)}</details>`;
  }
  function field(scope,key,title,value,options = null,helper = '',type = 'text',extra = '') {
    const id = `${scope}-${key}`;
    const attr = `id="${esc(id)}" data-scope="${scope}" data-key="${esc(key)}" aria-describedby="${esc(id)}-help" ${extra}`;
    return `<div class="cs-field"><label for="${esc(id)}">${esc(title)}</label>${options ? `<select ${attr}>${value === undefined ? '<option value="">Unknown · choose a value</option>' : ''}${options.map(o => { const [v,l] = Array.isArray(o) ? o : [o,o]; return `<option value="${esc(v)}" ${String(value ?? '') === String(v) ? 'selected' : ''}>${esc(l)}</option>`; }).join('')}</select>` : type === 'checkbox' ? `<label class="cs-check"><input type="checkbox" ${attr} ${value === true ? 'checked' : ''}>Enabled${value === undefined ? ' (current value unknown)' : ''}</label>` : `<input type="${type}" ${attr} value="${esc(value)}">`}<p id="${esc(id)}-help">${esc(helper)}</p></div>`;
  }
  function settings() {
    const d = settingsDraft;
    return `<div class="cs-pagehead"><div><p class="cs-overline">Workspace / preferences</p><h1>Settings</h1></div></div><form id="settings-form" class="cs-settings"><div class="cs-settinggrid"><section class="cs-settinggroup"><h2>Reviews</h2>${field('settings','review.mode','Goal review mode',d['review.mode'],['off','advisory','blocking'],'Automatic goal reviews only: off, feedback, or require approval. Manual review remains available.')}${field('settings','review.strategy','Review strategy',d['review.strategy'],['single','cross'],'Also governs manual reviews; editable when automatic goal reviews are off.')}</section><section class="cs-settinggroup"><h2>Execution</h2>${field('settings','execution.delegation_policy','Delegation',d['execution.delegation_policy'],['auto','always','manual'])}${field('settings','execution.inline_max_minutes','Inline estimate threshold (minutes)',d['execution.inline_max_minutes'],null,'Maximum estimated task length for inline work · 1–240 minutes.','number','min="1" max="240" step="1" required')}<details id="execution-advanced"><summary>Advanced execution / access</summary>${field('settings','execution.head_permissions','Head mode',d['execution.head_permissions'],['safe','full-access'],'Filesystem access profile for the head.')}${field('settings','execution.head_worker_fallback','Head fallback',d['execution.head_worker_fallback'],null,'Allow a managed worker to use the head account when other seats are unavailable.','checkbox')}</details></section></div><section class="cs-settinggroup cs-appearance"><h2>Appearance</h2><div class="cs-settinggrid">${field('settings','ui.theme','Theme',d['ui.theme'],['system','light','dark'])}${field('settings','ui.density','Density',d['ui.density'],['comfortable','compact'])}${field('settings','ui.accent','Accent',d['ui.accent'],['teal','indigo'])}</div><p class="cs-small cs-muted">Applied after a confirmed save to the existing settings.</p></section><div class="cs-preview" id="settings-delta">${delta(state.policy,d)}</div><p id="settings-validation" class="cs-error"></p><div class="cs-actions"><button id="settings-save" class="cs-button cs-primary" type="submit" ${busy() || !!stateError ? 'disabled' : ''}>Review settings</button>${button('Discard changes','discard-settings',busy())}</div></form>`;
  }
  function claudeUsage() {
    const c = state.claude, u = c.usage || {}, tier = c.subscription_tier || {};
    const windows = Object.entries(u.windows || {}).map(([name,w]) => ({name,window_minutes: name === 'five_hour' ? 300 : name === 'seven_day' ? 10080 : null, remaining_pct:w.remaining_percentage,resets_at:w.resets_at, freshness:u.fresh === true ? 'fresh' : 'stale', observed_at:number(u.age_seconds) && number(state.observed_at) ? state.observed_at - u.age_seconds : null}));
    return `<div class="cs-detailhead"><div><h2>${esc(tier.configured || 'No configured tier')}</h2><p class="cs-small cs-muted">Configured label · observed tier: ${esc(tier.detected || 'Unknown')}</p></div><span class="cs-status ${u.fresh === true && !stateError ? '' : 'cs-stale'}">${esc(known(c.status?.status))}${c.status?.busy ? ' · Busy' : ''}</span></div><div class="cs-detailmeta cs-small">Effective mode: ${esc(known(c.mode))} · Configured mode: ${esc(known(c.configured_mode))}</div>${notice(c.mode_reason || c.status_error)}${list(c.warnings).map(notice).join('')}<div class="cs-quotahead"><h3>Observed usage</h3><span class="cs-small cs-muted">${u.fresh === true && !stateError ? 'Fresh' : 'Stale / unknown'}</span></div><div class="cs-quotas">${windows.map(w => quota(w,!stateError)).join('') || '<p class="cs-muted">Usage unknown. ' + esc(u.reason || 'No observed windows.') + '</p>'}</div>`;
  }
  function claude() {
    const d = claudeDraft;
    return `<div class="cs-pagehead"><div><p class="cs-overline">Provider / Claude</p><h1>Claude account</h1></div></div><form id="claude-form" class="cs-claude">${claudeUsage()}${field('claude','mode','Routing mode',d.mode,['off','manual_only','review_only','opportunistic'])}${field('claude','subscription_tier','Configured tier',d.subscription_tier,[['','Clear label'],'Pro','Max 5x','Max 20x','Team','Team Premium','Enterprise'],'Optional label; never substitutes for an observed entitlement.')}${field('claude','active_usage_refresh','Active /usage refresh',d.active_usage_refresh,null,'Allow the existing watcher to refresh Claude usage through /usage.','checkbox')}<details id="claude-headroom"><summary>Advanced · headroom</summary><div class="cs-settinggrid">${field('claude','small_task_min_headroom_pct','Small task headroom (%)',d.small_task_min_headroom_pct,null,'Small must not exceed big.','number','min="0" max="100" step="any" required')}${field('claude','big_task_min_headroom_pct','Big task headroom (%)',d.big_task_min_headroom_pct,null,'Minimum remaining capacity for big tasks.','number','min="0" max="100" step="any" required')}</div></details><div id="claude-delta" class="cs-preview">${delta(claudeValues(),d)}</div><p id="claude-validation" class="cs-error"></p><div class="cs-actions"><button id="claude-save" type="submit" class="cs-button cs-primary" ${busy() || !!stateError ? 'disabled' : ''}>Review Claude settings</button>${button('Discard changes','discard-claude',busy())}</div></form>`;
  }
  function seatEditor() {
    const f = seatForm, d = f.draft, add = f.kind === 'add-seat';
    return `<form id="seat-form" class="cs-settings"><h2>${add ? 'Add Codex seat' : 'Edit ' + esc(f.label)}</h2>${field('seat','label','Label',d.label,null,'A recognizable name for this seat.','text','required maxlength="200"')}${add ? field('seat','home','Home (optional)',d.home,null,'Leave blank to use the manager’s default.') : ''}${field('seat','profile','Configured plan preset',d.profile,add ? profiles : [['','Keep current'],...profiles],'Choose only a plan you have verified. Configuration is separate from the observed plan.')}${!add ? `<details id="seat-edit-advanced"><summary>Reserve, headroom, capacity and role</summary>${field('seat','reserve_pct','Reserve (%)',d.reserve_pct,null,'Blank leaves the current reserve unchanged.','number','min="0" max="100" step="any"')}${field('seat','small_task_min_headroom_pct','Small task headroom (%)',d.small_task_min_headroom_pct,null,'Blank leaves the current floor unchanged.','number','min="0" max="100" step="any"')}${field('seat','big_task_min_headroom_pct','Big task headroom (%)',d.big_task_min_headroom_pct,null,'Small must not exceed big.','number','min="0" max="100" step="any"')}${field('seat','relative_capacity','Relative capacity',d.relative_capacity,null,'Blank leaves current capacity unchanged.','number','min="0.1" max="1000" step="any"')}${field('seat','capacity_source','Capacity evidence',d.capacity_source,[['','Keep current'],'unverified','openai-help-center','openai-published-baseline','workspace-ui','contract','operator-measured'])}<p class="cs-small cs-muted">Role: ${esc(known(f.role))}. Use More / advanced to preview a separate guarded role change for an active seat.</p>${terminal({label:f.label,id:f.seat})}</details>` : '<p class="cs-small cs-muted">The new seat starts inactive. Sign in through the terminal manager, then reactivate it.</p>'}<p id="seat-validation" class="cs-error"></p><div class="cs-actions"><button id="seat-save" class="cs-button cs-primary" type="submit" ${busy() || !!stateError ? 'disabled' : ''}>Review ${add ? 'addition' : 'changes'}</button>${button('Close editor','close-editor',busy())}</div></form>`;
  }
  function render() {
    document.title = `codex-seats · ${page[0].toUpperCase()+page.slice(1)}`;
    root.querySelectorAll('[data-page]').forEach(el => { if (el.closest('nav')) { if (el.dataset.page === page) el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current'); } });
    let html = state ? (page === 'inventory' ? inventory() : page === 'settings' ? settings() : claude()) : '<h1>Inventory unavailable</h1><p>The registry has not been read. No account state is assumed.</p>';
    html = (stateError ? notice(stateError) + button('Retry cached state','retry-state') : '') + (state ? list(state.warnings).map(notice).join('') : '') + html;
    preserveDOM(content,html);
    document.getElementById('connection').textContent = stateError ? 'Disconnected · last observation may be outdated' : state ? `State observed ${date(state.observed_at)}${hasWork(state.refresh?.active) ? ' · watcher refresh active' : hasWork(state.refresh?.queued) ? ' · watcher refresh queued' : ''}` : 'Connecting to the local manager';
    updateValidation(); renderOperation();
  }
  function validate(scope) {
    const d = scope === 'settings' ? settingsDraft : scope === 'claude' ? claudeDraft : seatForm?.draft;
    if (!d) return '';
    if (scope === 'settings' && (!Number.isInteger(d['execution.inline_max_minutes']) || d['execution.inline_max_minutes'] < 1 || d['execution.inline_max_minutes'] > 240)) return 'Inline estimate must be a whole number from 1 to 240.';
    if (scope === 'claude' || scope === 'seat') {
      const small = d.small_task_min_headroom_pct, big = d.big_task_min_headroom_pct;
      if (number(small) && number(big) && small > big) return 'Small task headroom must not exceed big task headroom.';
      if (scope === 'claude' && [small,big].some(v => !number(v) || v < 0 || v > 100)) return 'Headroom must be between 0 and 100 percent.';
    }
    return '';
  }
  function updateValidation() {
    for (const scope of ['settings','claude','seat']) {
      const element = document.getElementById(`${scope}-validation`);
      if (!element) continue;
      const error = validate(scope); element.textContent = error;
      const save = document.getElementById(`${scope}-save`);
      if (save) save.disabled = busy() || !!stateError || !!error;
    }
  }
  function renderOperation() {
    const target = document.getElementById('operation');
    target.hidden = !operation && !postPending && !operationError;
    if (target.hidden) { target.innerHTML = ''; return; }
    const op = operation, preview = op?.preview;
    const pending = postPending || ['preparing','running'].includes(op?.status);
    const credit = preview?.credit;
    const reset = op?.kind === 'redeem-reset';
    // Missing identity/credit evidence must never allow reset confirmation.
    const resetEvidence = !reset || (preview?.account_email && preview?.seat && credit?.fingerprint);
    const canConfirm = op?.status === 'ready' && !postPending && !confirmSent && resetEvidence;
    let html = `<h2 id="operation-title" tabindex="-1">${esc(preview?.title || (op?.status === 'running' ? 'Applying confirmed action' : op?.status === 'preparing' || !op ? 'Preparing action' : 'Action result'))}</h2>`;
    if (op) html += `<p class="cs-small cs-mono">${esc(op.kind)}${op.seat ? ' · ' + esc(op.seat) : ''} · ${esc(op.status)} · ${esc(op.id)}</p>`;
    if (pending) html += '<p class="cs-loading">Please wait. This action will not be submitted again automatically.</p>';
    if (preview) {
      html += `<p>${esc(preview.description || '')}</p>`;
      if (reset) html += facts([['Seat',preview.seat],['Account',preview.account_email],['Credit',credit?.title || credit?.fingerprint],['Granted',date(credit?.granted_at)],['Expires',credit?.expiry_status === 'none' ? 'No expiry reported by provider' : date(credit?.expires_at)],['Expiry status',credit?.expiry_status],['Available credits',preview.available_count]]) + '<p>This irreversibly spends one provider reset credit for the named account. Quota and credit balances will be re-read after completion.</p>';
      html += list(preview.diff).map(diff => `<pre>${esc(diff)}</pre>`).join('') + list(preview.warnings).map(notice).join('');
    }
    if (!resetEvidence && op?.status === 'ready') html += notice('The preview is missing account or credit identity. Cancel and prepare a new preview.');
    if (op?.error) html += notice(op.error);
    if (operationError) html += notice(operationError);
    if (op?.result) html += `<p>${esc(op.result.message)}</p>${op.result.outcome ? `<p>Outcome: ${esc(op.result.outcome)}</p>` : ''}${notice(op.result.refresh_error)}`;
    if (operationPaused) html += notice('Automatic status checks paused. The operation is retained; check its status before taking further action.');
    html += '<div class="cs-actions">';
    if (op && !terminalStates.has(op.status)) {
      if (['preparing','ready'].includes(op.status) && !confirmSent) html += button('Cancel preview','cancel',postPending);
      if (op.status === 'ready' && !confirmSent) html += button(reset ? 'Confirm · spend one credit' : 'Confirm changes','confirm',!canConfirm,true);
      if (operationPaused || operationError || confirmSent && op.status === 'ready') html += button('Check operation status','check-operation',postPending || operationReading);
      if (confirmSent && op.status === 'ready' && !postPending) html += button('Cancel and re-preview','repreview',false);
    } else if (op && terminalStates.has(op.status)) {
      html += button('Dismiss result','dismiss-operation');
      if (op.status === 'error' && !['unknown','unknown_outcome'].includes(op.result?.outcome)) html += button('Prepare a new preview','repreview',postPending);
    } else if (!postPending) html += button('Dismiss','dismiss-operation');
    html += '</div>';
    target.className = 'cs-confirm'; preserveDOM(target,html);
  }
  function validateOperation(op) {
    if (!op || typeof op.id !== 'string' || !['preparing','ready','running','done','error','canceled'].includes(op.status)) throw new Error('Invalid operation response. Do not repeat a submitted action until its outcome is known.');
    return op;
  }
  async function acceptOperation(next) {
    const previous = operation?.status;
    operation = validateOperation(next);
    if (previous !== operation.status) announce(`Action ${operation.status}`);
    if (terminalStates.has(operation.status)) {
      clearTimeout(operationTimer);
      if (previous !== operation.status) {
        const result = operation.result;
        notify([result?.message || operation.error || `Action ${operation.status}`,result?.outcome ? `Outcome: ${result.outcome}` : '',result?.refresh_error ? `Refresh error: ${result.refresh_error}` : ''].filter(Boolean).join(' · '));
        if (operation.status === 'done') {
          if (['settings','claude'].includes(operation.kind)) resetDraftAfterRead = operation.kind;
          if (['edit-seat','add-seat'].includes(operation.kind)) seatForm = null;
          if (operation.kind === 'set-role') roleChoice = null;
          if (operation.kind === 'add-seat') notify('Seat added inactive. Run codex-seat dash --manage, choose the new seat to sign in, then reactivate it.');
        }
        stateGeneration++;
        await readState();
      }
    }
    render(); scheduleOperationRead();
  }
  function scheduleOperationRead() {
    clearTimeout(operationTimer);
    if (!operation || !['preparing','running'].includes(operation.status) || operationPaused || document.hidden || stopped) return;
    if (operationReads >= 60) { operationPaused = true; renderOperation(); return; }
    operationTimer = setTimeout(readOperation, 2000);
  }
  async function readOperation() {
    if (!operation || operationReading || stopped || document.hidden) return;
    operationReading = true; operationReads++;
    const expected = operation;
    try {
      const next = await api(`/api/operations/${encodeURIComponent(operation.id)}`);
      if (operation !== expected || postPending) return;
      if (next.id !== operation.id) throw new Error('Operation identity changed unexpectedly.');
      operationError = ''; await acceptOperation(next);
    } catch (error) {
      if (!stopped) { operationError = `Status unavailable: ${error.message}. The outcome is not yet known; do not repeat the action.`; operationPaused = true; announce(operationError); }
    } finally { operationReading = false; renderOperation(); }
  }
  async function prepare(request) {
    if (busy() || stateError) return;
    clearTimeout(operationTimer); operation = null; operationRequest = request;
    postPending = true; confirmSent = false; operationError = ''; operationPaused = false; operationReads = 0; render();
    document.getElementById('operation-title')?.focus();
    try { await acceptOperation(await api('/api/preview',request)); }
    catch (error) { operationError = `Could not prepare preview: ${error.message}. No confirmation was sent. Prepare a new preview when ready.`; notify(operationError); }
    finally { postPending = false; render(); }
  }
  async function confirm() {
    if (postPending || confirmSent || operation?.status !== 'ready') return;
    const p = operation.preview;
    if (operation.kind === 'redeem-reset' && !(p?.account_email && p?.seat && p?.credit?.fingerprint)) return;
    // Set before the network call: a lost response must not unlock duplicate POSTs.
    confirmSent = true; postPending = true; operationError = ''; render();
    try { await acceptOperation(await api('/api/confirm',{id:operation.id})); }
    catch (error) {
      operationError = `Confirmation response failed: ${error.message}. Outcome unknown; check operation status. A conflict requires cancellation and a fresh preview.`;
      notify(operationError); operationPaused = true;
    } finally { postPending = false; render(); }
  }
  async function cancel(repreview = false) {
    if (!operation || postPending || !['preparing','ready'].includes(operation.status)) return;
    postPending = true; clearTimeout(operationTimer); render();
    try {
      await acceptOperation(await api('/api/cancel',{id:operation.id}));
      if (operation.status !== 'canceled') throw new Error('Cancellation is not confirmed. Check operation status.');
      postPending = false;
      if (repreview) await prepare(operationRequest);
    } catch (error) { operationError = `Cancellation failed: ${error.message}`; notify(operationError); operationPaused = true; }
    finally { postPending = false; render(); }
  }
  function openEditor(add) {
    const s = selectedSeat();
    seatForm = add ? {kind:'add-seat',draft:{label:'',home:'',profile:'unverified'}} : {kind:'edit-seat',seat:s.id,label:s.label || s.id,role:s.role,draft:{label:s.label || '',profile:'',reserve_pct:'',small_task_min_headroom_pct:'',big_task_min_headroom_pct:'',relative_capacity:'',capacity_source:''}};
    render(); document.getElementById('seat-label')?.focus();
  }
  root.addEventListener('input', event => {
    const el = event.target, scope = el.dataset.scope, key = el.dataset.key;
    if (!scope || !key) return;
    let value = el.type === 'checkbox' ? el.checked : el.type === 'number' ? el.value === '' ? '' : Number(el.value) : el.value;
    if (scope === 'claude' && key === 'subscription_tier' && value === '') value = null;
    if (scope === 'settings') { settingsDraft[key] = value; settingsDirty = true; document.getElementById('settings-delta').innerHTML = delta(state.policy,settingsDraft); }
    if (scope === 'claude') { claudeDraft[key] = value; claudeDirty = true; document.getElementById('claude-delta').innerHTML = delta(claudeValues(),claudeDraft); }
    if (scope === 'seat') seatForm.draft[key] = value;
    if (scope === 'role') {
      roleChoice = value;
      const review = document.getElementById('action-set-role');
      if (review) review.disabled = busy() || !!stateError || selectedSeat()?.lifecycle !== 'active' || selectedSeat()?.role === value;
    }
    updateValidation();
  });
  root.addEventListener('submit', event => {
    event.preventDefault();
    if (busy() || stateError || !event.target.reportValidity()) return;
    const scope = event.target.id.replace('-form','');
    if (validate(scope)) return;
    if (scope === 'settings' || scope === 'claude') {
      const changes = differences(scope === 'settings' ? state.policy : claudeValues(),scope === 'settings' ? settingsDraft : claudeDraft);
      if (!Object.keys(changes).length) { notify('No changes to save.'); return; }
      prepare({kind:scope,changes});
    } else if (scope === 'seat') {
      const changes = Object.fromEntries(Object.entries(seatForm.draft).filter(([,value]) => value !== ''));
      prepare({kind:seatForm.kind,...(seatForm.seat ? {seat:seatForm.seat} : {}),changes});
    }
  });
  root.addEventListener('click', async event => {
    const el = event.target.closest('button');
    if (!el || el.disabled || el.type === 'submit') return;
    if (el.dataset.dismiss) { const i = notifications.findIndex(n => n.id === Number(el.dataset.dismiss)); if (i >= 0) notifications.splice(i,1); renderNotifications(); return; }
    if (el.dataset.page) { page = el.dataset.page; render(); document.getElementById(`nav-${page}`)?.focus({preventScroll:true}); return; }
    if (el.dataset.select) { selected = el.dataset.select; seatForm = null; roleChoice = null; render(); return; }
    const action = el.dataset.action, s = selectedSeat();
    if (action === 'retry-state') { await readState(); return; }
    if (action === 'confirm') { await confirm(); return; }
    if (action === 'cancel') { await cancel(); return; }
    if (action === 'check-operation') { operationPaused = false; operationReads = 0; await readOperation(); return; }
    if (action === 'repreview') {
      if (operation?.status === 'ready') await cancel(true);
      else if (operation?.status === 'error') await prepare(operationRequest);
      return;
    }
    if (action === 'dismiss-operation' && !busy()) { operation = null; operationError = ''; renderOperation(); return; }
    if (busy()) return;
    if (action === 'discard-settings') { settingsDraft = {...state.policy}; settingsDirty = false; render(); return; }
    if (action === 'discard-claude') { claudeDraft = claudeValues(); claudeDirty = false; render(); return; }
    if (action === 'add' || action === 'add-empty') { openEditor(true); return; }
    if (action === 'edit' && s) { openEditor(false); return; }
    if (action === 'close-editor') { seatForm = null; render(); return; }
    if (stateError || !s) return;
    if (action === 'refresh' && !refreshPending) {
      refreshPending = true; render();
      try { const result = await api('/api/refresh',{seat:s.id}); notify(result.message || 'Selected refresh queued. Awaiting watcher observations.'); }
      catch (error) { notify(`Refresh request failed: ${error.message}. It was not retried.`); }
      finally { refreshPending = false; await readState(); render(); }
    } else if (action === 'redeem-reset' && !resetReason(s)) await prepare({kind:'redeem-reset',seat:s.id});
    else if (action === 'set-role' && s.lifecycle === 'active' && roleChoice && roleChoice !== s.role) await prepare({kind:'set-role',seat:s.id,changes:{role:roleChoice}});
    else if (action === 'pause') await prepare({kind:'pause',seat:s.id});
    else if (action === 'reactivate') await prepare({kind:'reactivate',seat:s.id});
    else if (action === 'reactivate-primary' && s.lifecycle !== 'active') await prepare({kind:'reactivate',seat:s.id,changes:{as_primary:true}});
    else if (action === 'remove-seat' && s.lifecycle === 'inactive' && s.busy === false && !['primary','deputy'].includes(s.role)) await prepare({kind:'remove-seat',seat:s.id});
  });
  root.addEventListener('keydown', event => {
    if (event.key === 'Escape' && operation && ['preparing','ready'].includes(operation.status) && !postPending && !confirmSent) { event.preventDefault(); cancel(); }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { clearTimeout(stateTimer); clearTimeout(operationTimer); }
    else { readState(); scheduleOperationRead(); }
  });
  window.addEventListener('pagehide', () => { stopped = true; clearTimeout(stateTimer); clearTimeout(operationTimer); requests.forEach(c => c.abort()); });
  window.addEventListener('pageshow', event => { if (event.persisted) { stopped = false; readState(); scheduleOperationRead(); } });
  window.addEventListener('beforeunload', event => { if (busy()) { event.preventDefault(); event.returnValue = ''; } });
  readState();
})();
