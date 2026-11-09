'use strict';
const $ = id => document.getElementById(id);
const states = {queued:'Queued', retry:'Waiting to retry', processing:'Processing', dead:'Stopped (dead)', complete:'Completed', cancelled:'Cancelled', dry_run:'Dry run'};
const stages = ['Match', 'Collect', 'Analyze', 'Publish', 'Done'];
let offset = 0, busy = false;
const limit = 25;
const errorMessage = error => error.name === 'TimeoutError' || error.name === 'AbortError' ? 'The request timed out. Displayed data may be stale. Refresh to try again.' : error instanceof TypeError ? 'Unable to reach the queue service. Displayed data may be stale.' : error instanceof SyntaxError ? 'The service returned an invalid response. Displayed data may be stale.' : error.name === 'Error' ? error.message : 'Unable to complete the request. Refresh to try again.';
const date = n => n ? new Date(n * 1000).toLocaleString('en-GB', {hour12:false}) : '—';
function node(tag, value, parent) {
  const el = document.createElement(tag);
  if (value !== undefined && value !== null) el.textContent = String(value);
  if (parent) parent.appendChild(el);
  return el;
}
async function api(action, data) {
  const response = await fetch('/queue/api/' + action, {method:'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json', 'X-SPI-Queue':'1'}, body:JSON.stringify(data),
    signal:AbortSignal.timeout(18000)});
  if (response.redirected || !(response.headers.get('Content-Type') || '').includes('application/json'))
    throw new Error('Check your SSO session: reopen this page.');
  const result = await response.json();
  if (!response.ok) {
    const messages = {AUTH_UNAVAILABLE:'SSO is temporarily unavailable. Displayed data may be stale.', FORBIDDEN:'You do not have permission to manage the queue.', AUTH_REQUIRED:'Your session has expired. Reopen /queue/ to sign in with SSO.', QUEUE_ITEM_CHANGED:'The job has changed. Refresh the list.', QUEUE_ITEM_NOT_CANCELLABLE:'The job is already running or has publication activity and cannot be cancelled.', QUEUE_UNAVAILABLE:'The backend is temporarily unavailable. Displayed data may be stale.'};
    throw new Error(messages[result.code] || 'Request failed (' + (result.code || response.status) + ').');
  }
  return result;
}
async function cancel(item, button) {
  if (busy || !confirm('Cancel pending job "' + (item.alertname || item.id) + '"?\nIt will no longer be processed automatically. Its history will be preserved.')) return;
  busy = true; button.disabled = true;
  try {
    await api('cancel', {job_id:item.id, expected_updated:item.updated});
    $('status').textContent = 'Job cancelled. History preserved.';
  } catch (error) { $('status').textContent = errorMessage(error); }
  finally { busy = false; }
  await refresh(true);
}
function draw(data) {
  const c = data.counts;
  $('counts').textContent = 'Active: ' + (c.processing || 0) + ' · Queued: ' + (c.queued || 0) + ' · Retry: ' + (c.retry || 0) + ' · Dead: ' + (c.dead || 0) + ' · Completed: ' + (c.complete || 0) + ' · Cancelled: ' + (c.cancelled || 0);
  $('limits').textContent = 'Concurrency: ' + data.limits.parallel_jobs + ' job · LLM: ' + data.limits.requests_per_minute + ' RPM / ' + data.limits.tokens_per_minute + ' TPM · Updated: ' + date(data.now);
  $('items').replaceChildren();
  for (const item of data.items) {
    const row = node('tr', null, $('items'));
    if (item.state === 'processing') row.className = 'active';
    const name = node('td', null, row);
    node('strong', item.alertname || 'Unnamed alert', name);
    node('small', [item.alert_status, item.severity, item.namespace, item.instance].filter(Boolean).join(' · '), name);
    if (item.summary) node('div', item.summary, name);
    const details = node('details', null, name); node('summary', item.id.slice(0, 12) + '…', details); node('small', item.id, details);
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
    node('small', 'Created: ' + date(item.created), timing);
    if (item.state === 'retry') node('small', 'Available at: ' + date(item.available), timing);
    if (item.state === 'processing' && item.deadline) node('small', 'Deadline: ' + date(item.deadline), timing);
    const progress = node('td', null, row);
    node('small', 'Claims: ' + item.attempts + ' · Failures: ' + item.failures + '/8', progress);
    if (item.review_catalog !== null) node('small', 'Match: ' + (item.review_batches || 0) + ' batches saved · ' + item.review_catalog + ' issues in snapshot', progress);
    if (item.error) node('small', 'Last error: ' + item.error, progress);
    const controls = node('td', null, row);
    if (item.can_cancel) {
      const button = node('button', 'Cancel', controls); button.type = 'button'; button.onclick = () => cancel(item, button);
    } else node('small', '—', controls);
  }
  if (!data.items.length) { const td = node('td', 'No jobs in this list.', node('tr', null, $('items'))); td.colSpan = 5; }
  $('page').textContent = data.total ? (offset + 1) + '–' + Math.min(offset + limit, data.total) + ' of ' + data.total : '0 jobs';
  $('prev').disabled = offset === 0; $('next').disabled = offset + limit >= data.total;
}
async function refresh(keepMessage = false) {
  if (busy) return;
  busy = true; $('refresh').disabled = true;
  try {
    const data = await api('list', {state:$('filter').value, offset, limit});
    draw(data); if (!keepMessage) $('status').textContent = '';
  } catch (error) { $('status').textContent = errorMessage(error); }
  finally { busy = false; $('refresh').disabled = false; }
}
$('refresh').onclick = () => refresh();
$('filter').onchange = () => { if (busy) return; offset = 0; refresh(); };
$('prev').onclick = () => { if (busy) return; offset = Math.max(0, offset - limit); refresh(); };
$('next').onclick = () => { if (busy) return; offset += limit; refresh(); };
setInterval(() => { if ($('auto').checked && !document.hidden) refresh(); }, 15000);
refresh();

$('logout').onclick = async () => {
  try {
    const response = await fetch('/logout', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json', 'X-SPI-Queue':'1'}, body:'{}',
      signal:AbortSignal.timeout(10000)});
    // 401 means no valid local session exists: logout is already complete.
    if (response.ok || response.status === 401) window.location.assign('/signed-out');
    else $('status').textContent = `Unable to sign out (HTTP ${response.status}). Please try again.`;
  } catch (error) {
    $('status').textContent = error?.name === 'TimeoutError'
      ? 'Sign-out request timed out. Please try again.'
      : 'Unable to reach the server to sign out. Check your connection and try again.';
  }
};
