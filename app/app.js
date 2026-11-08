'use strict';
const $ = id => document.getElementById(id);
const states = {queued:'Новая', retry:'Ждёт повторной попытки', processing:'В обработке', dead:'Остановлена', complete:'Завершена', cancelled:'Отменена', dry_run:'Dry run'};
const stages = ['Match', 'Collect', 'Analyze', 'Publish', 'Готово'];
let offset = 0, busy = false;
const limit = 25;
const date = n => n ? new Date(n * 1000).toLocaleString('ru-RU') : '—';
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
    throw new Error('Проверьте SSO-сессию: откройте эту страницу заново.');
  const result = await response.json();
  if (!response.ok) {
    const messages = {AUTH_UNAVAILABLE:'Сервис SSO временно недоступен. Данные могут быть устаревшими.', FORBIDDEN:'Нет прав на управление очередью.', AUTH_REQUIRED:'Сессия истекла. Откройте /queue/ заново для входа через SSO.', QUEUE_ITEM_CHANGED:'Задание изменилось. Обновите список.', QUEUE_ITEM_NOT_CANCELLABLE:'Задание уже выполняется или имеет публикацию; отмена запрещена.', QUEUE_UNAVAILABLE:'Backend временно недоступен. Данные на экране могут быть устаревшими.'};
    throw new Error(messages[result.code] || 'Запрос не выполнен (' + (result.code || response.status) + ').');
  }
  return result;
}
async function cancel(item, button) {
  if (busy || !confirm('Отменить ожидающее задание «' + (item.alertname || item.id) + '»?\nОно больше не будет обрабатываться автоматически. История сохранится.')) return;
  busy = true; button.disabled = true;
  try {
    await api('cancel', {job_id:item.id, expected_updated:item.updated});
    $('status').textContent = 'Задание отменено. История сохранена.';
  } catch (error) { $('status').textContent = error.message; }
  finally { busy = false; }
  await refresh(true);
}
function draw(data) {
  const c = data.counts;
  $('counts').textContent = 'Активно: ' + (c.processing || 0) + ' · Новые: ' + (c.queued || 0) + ' · Retry: ' + (c.retry || 0) + ' · Dead: ' + (c.dead || 0) + ' · Завершены: ' + (c.complete || 0) + ' · Отменены: ' + (c.cancelled || 0);
  $('limits').textContent = 'Параллельность: ' + data.limits.parallel_jobs + ' job · LLM: ' + data.limits.requests_per_minute + ' RPM / ' + data.limits.tokens_per_minute + ' TPM · Обновлено: ' + date(data.now);
  $('items').replaceChildren();
  for (const item of data.items) {
    const row = node('tr', null, $('items'));
    if (item.state === 'processing') row.className = 'active';
    const name = node('td', null, row);
    node('strong', item.alertname || 'Алерт без имени', name);
    node('small', [item.alert_status, item.severity, item.namespace, item.instance].filter(Boolean).join(' · '), name);
    if (item.summary) node('div', item.summary, name);
    const details = node('details', null, name); node('summary', item.id.slice(0, 12) + '…', details); node('small', item.id, details);
    const state = node('td', null, row);
    node('div', states[item.state] || item.state, state);
    node('small', item.owner_stage && item.state === 'processing' ? item.owner_stage : stages[item.stage], state);
    if (item.state === 'processing' && item.owner_phase === 'waiting') node('small', 'Wait до ' + date(item.due), state);
    if (item.execution_id && /^\d+$/.test(item.execution_id)) {
      const link = node('a', 'n8n #' + item.execution_id, state);
      link.href = 'https://n8n.socrat.alchemmist.xyz/workflow/J9akzlydrf9XZiID/executions/' + item.execution_id;
      link.target = '_blank'; link.rel = 'noopener noreferrer';
    }
    const timing = node('td', null, row);
    node('small', 'Создано: ' + date(item.created), timing);
    if (item.state === 'retry') node('small', 'Доступно с: ' + date(item.available), timing);
    if (item.state === 'processing' && item.deadline) node('small', 'Крайний срок: ' + date(item.deadline), timing);
    const progress = node('td', null, row);
    node('small', 'Claims: ' + item.attempts + ' · Ошибки: ' + item.failures + '/8', progress);
    if (item.review_catalog !== null) node('small', 'Match: ' + (item.review_batches || 0) + ' пакетов сохранено · ' + item.review_catalog + ' issues в снимке', progress);
    if (item.error) node('small', 'Последняя ошибка: ' + item.error, progress);
    const controls = node('td', null, row);
    if (item.can_cancel) {
      const button = node('button', 'Отменить', controls); button.type = 'button'; button.onclick = () => cancel(item, button);
    } else node('small', '—', controls);
  }
  if (!data.items.length) { const td = node('td', 'В этом списке заданий нет.', node('tr', null, $('items'))); td.colSpan = 5; }
  $('page').textContent = data.total ? (offset + 1) + '–' + Math.min(offset + limit, data.total) + ' из ' + data.total : '0 заданий';
  $('prev').disabled = offset === 0; $('next').disabled = offset + limit >= data.total;
}
async function refresh(keepMessage = false) {
  if (busy) return;
  busy = true; $('refresh').disabled = true;
  try {
    const data = await api('list', {state:$('filter').value, offset, limit});
    draw(data); if (!keepMessage) $('status').textContent = '';
  } catch (error) { $('status').textContent = error.message; }
  finally { busy = false; $('refresh').disabled = false; }
}
$('refresh').onclick = () => refresh();
$('filter').onchange = () => { if (busy) return; offset = 0; refresh(); };
$('prev').onclick = () => { if (busy) return; offset = Math.max(0, offset - limit); refresh(); };
$('next').onclick = () => { if (busy) return; offset += limit; refresh(); };
setInterval(() => { if ($('auto').checked && !document.hidden) refresh(); }, 15000);
refresh();
