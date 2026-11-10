'use strict';
const $ = id => document.getElementById(id);
const states = {queued:'Queued', retry:'Waiting to retry', processing:'Processing', dead:'Stopped (dead)', complete:'Completed', cancelled:'Cancelled', dry_run:'Dry run'};
const stages = ['Match', 'Collect', 'Analyze', 'Publish', 'Done'];
const limit = 50;
let loadFailed = false, retryReset = true;
let busy = false, loading = false, generation = 0, controller, loadTimer, cursor = null;
let items = [], total = 0, sort = 'created', direction = 'asc', selectAll = false;
let gesture = null, scrollFrame = 0, skipClick = false;
const selected = new Map(), rows = new Map();
const errorMessage = error => error.name === 'TimeoutError' || error.name === 'AbortError' ? 'The request timed out. Displayed data may be stale. Refresh to try again.' : error instanceof TypeError ? 'Unable to reach the queue service. Displayed data may be stale.' : error instanceof SyntaxError ? 'The service returned an invalid response. Displayed data may be stale.' : error.name === 'Error' ? error.message : 'Unable to complete the request. Refresh to try again.';
const date = n => n ? new Date(n * 1000).toLocaleString('en-GB', {hour12:false}) : '—';
function node(tag, value, parent) {
  const el = document.createElement(tag);
  if (value !== undefined && value !== null) el.textContent = String(value);
  if (parent) parent.appendChild(el);
  return el;
}
async function api(action, data, signal) {
  const timeout = AbortSignal.timeout(18000);
  const response = await fetch('/api/' + action, {method:'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json', 'X-SPI-Queue':'1'}, body:JSON.stringify(data),
    signal:signal ? AbortSignal.any([timeout, signal]) : timeout});
  if (response.redirected || !(response.headers.get('Content-Type') || '').includes('application/json'))
    throw new Error('Check your SSO session: reopen this page.');
  const result = await response.json();
  if (!response.ok) {
    const messages = {SERVICE_BUSY:'The service is busy. Refresh to try again.', AUTH_UNAVAILABLE:'SSO is temporarily unavailable. Displayed data may be stale.', FORBIDDEN:'You do not have permission to manage the queue.', AUTH_REQUIRED:'Your session has expired. Reopen this site to sign in with SSO.', QUEUE_ITEM_CHANGED:'The job has changed. Refresh the list.', QUEUE_ITEM_NOT_CANCELLABLE:'The job is already running or has publication activity and cannot be cancelled.', QUEUE_UNAVAILABLE:'The backend is temporarily unavailable. Displayed data may be stale.'};
    const error = new Error(messages[result.code] || 'Request failed (' + (result.code || response.status) + ').');
    error.code = result.code; error.status = response.status; throw error;
  }
  return result;
}
function controls() {
  $('refresh').disabled = busy;
  $('filter').disabled = busy;
  $('select-all').disabled = busy;
  for (const key of ['created', 'alert', 'state', 'progress']) {
    const button = $('sort-' + key);
    button.disabled = busy;
    button.parentElement.setAttribute('aria-sort', key === sort ? (direction === 'asc' ? 'ascending' : 'descending') : 'none');
    button.textContent = {created:'Timing · Created', alert:'Alert / job', state:'State / stage', progress:'Progress / history'}[key] + (key === sort ? (direction === 'asc' ? ' ↑' : ' ↓') : ' ↕');
  }
}
function selectionUI() {
  for (const [id, row] of rows) {
    row.classList.toggle('selected', selected.has(id));
    row.setAttribute('aria-selected', String(selected.has(id)));
  }
  const eligible = [...selected.values()].filter(item => item.can_cancel).length;
  $('bulkbar').hidden = selected.size === 0 && !selectAll;
  $('selection-count').textContent = selected.size + ' selected · ' + eligible + ' cancellable' + (selectAll && (cursor || loading) ? ' · selecting remaining jobs…' : '');
  $('select-all').checked = selectAll || (items.length > 0 && items.every(item => selected.has(item.id)));
  $('select-all').indeterminate = !selectAll && selected.size > 0 && !items.every(item => selected.has(item.id));
  $('cancel-selected').disabled = busy || !eligible || (selectAll && (loading || !!cursor));
  $('clear-selection').disabled = busy;
}
function clearSelection() {
  selected.clear(); selectAll = false; selectionUI();
}
function toggle(item) {
  selectAll = false;
  if (selected.has(item.id)) selected.delete(item.id); else selected.set(item.id, {...item});
  selectionUI();
}
function draw(data, append = false) {
  const c = data.counts;
  $('counts').textContent = 'Active: ' + (c.processing || 0) + ' · Queued: ' + (c.queued || 0) + ' · Retry: ' + (c.retry || 0) + ' · Dead: ' + (c.dead || 0) + ' · Completed: ' + (c.complete || 0) + ' · Cancelled: ' + (c.cancelled || 0);
  $('limits').textContent = 'Concurrency: ' + data.limits.parallel_jobs + ' job · LLM: ' + data.limits.requests_per_minute + ' RPM / ' + data.limits.tokens_per_minute + ' TPM · Updated: ' + date(data.now);
  if (!append) { $('items').replaceChildren(); rows.clear(); }
  const fragment = document.createDocumentFragment();
  for (const item of data.items) {
    const row = node('tr', null, fragment);
    row.dataset.jobId = item.id; row.tabIndex = rows.size === 0 ? 0 : -1;
    rows.set(item.id, row);
    if (item.state === 'processing') row.classList.add('active');
    const name = node('td', null, row);
    node('strong', item.alertname || 'Unnamed alert', name);
    node('small', [item.alert_status, item.severity, item.namespace, item.instance].filter(Boolean).join(' · '), name);
    if (item.summary) node('div', item.summary, name);
    node('small', item.id, name).className = 'job-id';
    const state = node('td', null, row);
    node('div', states[item.state] || item.state, state);
    node('small', item.owner_stage && item.state === 'processing' ? item.owner_stage : stages[item.stage], state);
    if (item.state === 'processing' && item.owner_phase === 'waiting') node('small', 'Wait until ' + date(item.due), state);
    if (item.execution_id && /^\d+$/.test(item.execution_id)) {
      const link = node('a', 'n8n #' + item.execution_id, state);
      link.href = 'https://n8n.socrat.alchemmist.xyz/workflow/J9akzlydrf9XZiID/executions/' + item.execution_id;
      link.target = '_blank'; link.rel = 'noopener noreferrer';
    }
    const timing = node('td', null, row);
    node('div', 'Created: ' + date(item.created), timing);
    if (item.state === 'retry') node('small', 'Available at: ' + date(item.available), timing);
    if (item.state === 'processing' && item.deadline) node('small', 'Deadline: ' + date(item.deadline), timing);
    const progress = node('td', null, row);
    node('small', 'Claims: ' + item.attempts + ' · Failures: ' + item.failures + '/8', progress);
    if (item.review_catalog !== null) node('small', 'Match: ' + (item.review_batches || 0) + ' batches saved · ' + item.review_catalog + ' issues in snapshot', progress);
    if (item.error) node('small', 'Last error: ' + item.error, progress);
    const action = node('td', null, row);
    if (item.can_cancel) {
      const button = node('button', 'Cancel', action); button.type = 'button'; button.onclick = () => cancel(item, button); button.disabled = busy;
    } else node('small', '—', action);
  }
  if (!append && !data.items.length) { const td = node('td', 'No jobs in this list.', node('tr', null, fragment)); td.colSpan = 5; }
  $('items').appendChild(fragment);
  selectionUI(); controls();
}
function loadedStatus() {
  $('loaded').textContent = items.length + ' of ' + total + ' jobs loaded' + (loading ? ' · loading…' : loadFailed ? ' · loading interrupted; displayed data may be stale' : cursor ? '' : ' · all loaded');
  selectionUI();
}
function stopLoading() {
  generation++; controller?.abort(); clearTimeout(loadTimer); loading = false;
}
function scheduleLoad() {
  clearTimeout(loadTimer);
  if (cursor && !loadFailed && !loading && !busy && !document.hidden) loadTimer = setTimeout(() => loadMore(), 350);
}
async function loadPage(reset, keepMessage = false) {
  if (busy || (!reset && (loading || !cursor))) return;
  if (reset) {
    stopLoading(); cursor = null; items = []; total = 0; rows.clear(); $('items').replaceChildren();
    node('td', 'Loading jobs…', node('tr', null, $('items'))).colSpan = 5;
  }
  const version = generation;
  controller = new AbortController(); loading = true; loadFailed = false; retryReset = reset; loadedStatus();
  $('retry-load').hidden = true;
  try {
    const data = await api('list', {state:$('filter').value, limit, sort, direction, ...(reset ? {} : {after:cursor})}, controller.signal);
    if (version !== generation) return;
    if (!Array.isArray(data.items) || !Number.isInteger(data.total) || !Object.hasOwn(data, 'next_cursor')) throw new SyntaxError();
    if (reset) items = [];
    const seen = new Set(items.map(item => item.id));
    const fresh = data.items.filter(item => !seen.has(item.id) && seen.add(item.id));
    items.push(...fresh); total = data.total; cursor = data.next_cursor;
    if (selectAll) for (const item of fresh) selected.set(item.id, {...item});
    draw({...data, items:fresh}, !reset);
    if (reset && !keepMessage) $('status').textContent = '';
  } catch (error) {
    if (version !== generation) return;
    loadFailed = true;
    $('status').textContent = (keepMessage ? $('status').textContent + ' ' : '') + errorMessage(error);
    $('retry-load').hidden = false;
    return; // No automatic retry loop, particularly after an expired SSO session.
  } finally {
    if (version === generation) { loading = false; loadedStatus(); }
  }
  scheduleLoad();
}
const loadMore = (manual = false) => !loadFailed || manual ? loadPage(false) : undefined;
const refresh = (keepMessage = false) => loadPage(true, keepMessage);
async function runCancellations(snapshot, protectedCount = 0) {
  busy = true; stopLoading(); selectAll = false; controls(); selectionUI();
  for (const row of rows.values()) for (const button of row.querySelectorAll('button')) button.disabled = true;
  let done = 0, skipped = 0, failure = '';
  try {
    for (const item of snapshot) {
      $('status').textContent = 'Cancelling ' + (done + skipped + 1) + ' of ' + snapshot.length + '…';
      try {
        const result = await api('cancel', {job_id:item.id, expected_updated:item.updated});
        if (result.status !== 'cancelled') throw new SyntaxError();
        done++; selected.delete(item.id);
      } catch (error) {
        if (['QUEUE_ITEM_CHANGED', 'QUEUE_ITEM_NOT_CANCELLABLE', 'JOB_NOT_FOUND'].includes(error.code)) { skipped++; selected.delete(item.id); continue; }
        failure = errorMessage(error); break; // Uncertain response/auth failure: stop, never replay automatically.
      }
    }
  } finally {
    busy = false; controls(); selectionUI();
  }
  $('status').textContent = done + ' cancelled · ' + (skipped + protectedCount) + ' changed/protected and skipped · ' + (snapshot.length - done - skipped) + ' not confirmed. History preserved.' + (failure ? ' ' + failure : '');
  if (failure) {
    loadFailed = true; retryReset = true; $('retry-load').hidden = false; loadedStatus();
    for (const row of rows.values()) for (const button of row.querySelectorAll('button')) button.disabled = false;
    return;
  }
  await refresh(true);
}
async function cancel(item, button) {
  if (busy || !confirm('Cancel pending job "' + (item.alertname || item.id) + '"?\nIt will no longer be processed automatically. Its history will be preserved.')) return;
  button.disabled = true;
  await runCancellations([{...item}]);
}
async function cancelSelected() {
  if (busy || (selectAll && (cursor || loading))) return;
  const snapshot = [...selected.values()].filter(item => item.can_cancel).map(item => ({...item}));
  if (!snapshot.length || !confirm('Cancel ' + snapshot.length + ' selected pending jobs?\nActive/protected jobs will be skipped. History and evidence will be preserved.\nOnly this selection will be affected; new arrivals will not be included.')) return;
  await runCancellations(snapshot, selected.size - snapshot.length);
}
// Preserve native text selection and links. Only an initial drag on blank row
// space enters selection mode; existing selection also supports ordinary clicks.
function interactive(target) { return target.closest('a,button,input,select,textarea,[contenteditable]'); }
function textHit(event) {
  const caret = document.caretPositionFromPoint?.(event.clientX, event.clientY);
  const text = caret?.offsetNode || document.caretRangeFromPoint?.(event.clientX, event.clientY)?.startContainer;
  if (!text || text.nodeType !== 3) return false;
  const range = document.createRange(); range.selectNodeContents(text);
  return [...range.getClientRects()].some(r => event.clientX >= r.left && event.clientX <= r.right && event.clientY >= r.top && event.clientY <= r.bottom);
}
function paintRange(index) {
  selected.clear(); for (const [id, item] of gesture.before) selected.set(id, item);
  for (let n = Math.min(gesture.anchor, index); n <= Math.max(gesture.anchor, index); n++) selected.set(items[n].id, {...items[n]});
  selectAll = false; selectionUI();
}
function dragMove() {
  if (!gesture?.dragged) return;
  const row = document.elementFromPoint(gesture.x, Math.max(1, Math.min(innerHeight - 1, gesture.y)))?.closest('tr[data-job-id]');
  if (row) { const index = items.findIndex(item => item.id === row.dataset.jobId); if (index >= 0) paintRange(index); }
}
function autoScroll() {
  if (!gesture?.dragged) { scrollFrame = 0; return; }
  const delta = gesture.y < 55 ? -18 : gesture.y > innerHeight - 55 ? 18 : 0;
  if (delta) { window.scrollBy(0, delta); dragMove(); }
  scrollFrame = requestAnimationFrame(autoScroll);
}
function endGesture(cancelled = false) {
  if (!gesture) return;
  if (cancelled && gesture.dragged) { selected.clear(); for (const [id, item] of gesture.before) selected.set(id, item); }
  const pointer = gesture.pointer; gesture = null;
  if ($('items').hasPointerCapture?.(pointer)) $('items').releasePointerCapture(pointer);
  cancelAnimationFrame(scrollFrame); scrollFrame = 0;
  document.body.classList.remove('selecting'); selectionUI();
}
$('items').onpointerdown = event => {
  if (busy || event.button !== 0 || event.isPrimary === false || interactive(event.target) || textHit(event)) return;
  const row = event.target.closest('tr[data-job-id]'); if (!row) return;
  const index = items.findIndex(item => item.id === row.dataset.jobId); if (index < 0) return;
  gesture = {pointer:event.pointerId, anchor:index, before:new Map(selected), startX:event.clientX, startY:event.clientY, x:event.clientX, y:event.clientY, dragged:false};
  if (selected.size) window.getSelection()?.removeAllRanges();
  $('items').setPointerCapture(event.pointerId); event.preventDefault();
};
window.addEventListener('pointermove', event => {
  if (!gesture || event.pointerId !== gesture.pointer) return;
  gesture.x = event.clientX; gesture.y = event.clientY;
  if (!gesture.dragged && Math.hypot(gesture.x - gesture.startX, gesture.y - gesture.startY) > 5) {
    gesture.dragged = true; document.body.classList.add('selecting'); window.getSelection()?.removeAllRanges();
    scrollFrame = requestAnimationFrame(autoScroll);
  }
  dragMove();
});
window.addEventListener('pointerup', event => {
  if (!gesture || event.pointerId !== gesture.pointer) return;
  if (!gesture.dragged && selected.size) toggle(items[gesture.anchor]);
  skipClick = true; setTimeout(() => { skipClick = false; }, 0);
  endGesture();
});
window.addEventListener('pointercancel', () => endGesture(true));
window.addEventListener('blur', () => endGesture(true));
$('items').onclick = event => {
  if (skipClick) { skipClick = false; return; }
  if (busy || !selected.size || interactive(event.target) || !window.getSelection()?.isCollapsed) return;
  const row = event.target.closest('tr[data-job-id]');
  const item = row && items.find(item => item.id === row.dataset.jobId); if (item) toggle(item);
};
$('items').onfocusin = event => {
  const row = event.target.closest('tr[data-job-id]'); if (!row) return;
  for (const candidate of rows.values()) candidate.tabIndex = candidate === row ? 0 : -1;
};
$('items').onkeydown = event => {
  if (busy || interactive(event.target)) return;
  const row = event.target.closest('tr[data-job-id]'), index = row && items.findIndex(item => item.id === row.dataset.jobId);
  if (!row || index < 0) return;
  if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); toggle(items[index]); }
  else if (event.key === 'Escape') clearSelection();
  else if (['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : Math.max(0, Math.min(items.length - 1, index + (event.key === 'ArrowUp' ? -1 : 1)));
    rows.get(items[next].id)?.focus();
  }
};
$('select-all').onchange = () => {
  if (busy) return;
  if (!$('select-all').checked) clearSelection();
  else { selectAll = true; for (const item of items) selected.set(item.id, {...item}); selectionUI(); scheduleLoad(); }
};
$('clear-selection').onclick = clearSelection;
$('cancel-selected').onclick = cancelSelected;
$('refresh').onclick = () => { selectAll = false; refresh(); };
$('filter').onchange = () => { if (!busy) { clearSelection(); refresh(); } };
for (const key of ['created', 'alert', 'state', 'progress']) $('sort-' + key).onclick = () => {
  if (busy) return;
  endGesture(true); selectAll = false;
  direction = sort === key && direction === 'asc' ? 'desc' : 'asc'; sort = key; controls(); refresh();
};
$('retry-load').onclick = () => retryReset ? refresh(true) : loadMore(true);
if (typeof IntersectionObserver !== 'undefined') new IntersectionObserver(entries => {
  if (entries.some(entry => entry.isIntersecting)) loadMore();
}, {rootMargin:'600px'}).observe($('sentinel'));
document.addEventListener('keydown', event => { if (event.key === 'Escape' && !busy) { endGesture(true); clearSelection(); } });
document.addEventListener('visibilitychange', () => { if (!document.hidden) scheduleLoad(); });
setInterval(() => {
  // No jumps under the pointer or while reading/selecting lower rows.
  if ($('auto').checked && !document.hidden && !busy && !loading && !cursor && !loadFailed && !gesture && !selected.size && !selectAll && window.scrollY < 100) refresh();
}, 15000);
refresh();
$('logout').onclick = async () => {
  if (busy) { $('status').textContent = 'Wait for the current cancellation request to finish before signing out.'; return; }
  stopLoading();
  try {
    const response = await fetch('/logout', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json', 'X-SPI-Queue':'1'}, body:'{}',
      signal:AbortSignal.timeout(10000)});
    if (response.ok || response.status === 401) window.location.assign('/signed-out');
    else $('status').textContent = `Unable to sign out (HTTP ${response.status}). Please try again.`;
  } catch (error) {
    $('status').textContent = error?.name === 'TimeoutError'
      ? 'Sign-out request timed out. Please try again.'
      : 'Unable to reach the server to sign out. Check your connection and try again.';
  }
};
