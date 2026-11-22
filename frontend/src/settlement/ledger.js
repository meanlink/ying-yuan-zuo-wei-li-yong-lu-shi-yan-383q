import {escape} from '../operations.js';

const currency = new Intl.NumberFormat('zh-CN', {style:'currency', currency:'CNY'});
const money = cents => currency.format(cents / 100);
const pct = value => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
const status = {submitted:'待收益复核', held:'暂缓复核', approved:'已确认收益', rejected:'已驳回', superseded:'已被补正取代', recheck:'待重核'};
const sourceLabels = {schedule:'排片回执实际投放', observation:'实验单元观测售出'};
const amounts = [
  ['gross_tickets_cents','票务实收（分）'], ['ticket_commission_cents','票务分账（分）'],
  ['concession_margin_cents','卖品毛利（分）'], ['incremental_cost_cents','增量成本（分）'], ['subsidy_cents','活动补贴（分）'],
];
const field = (name, label, attrs = '') => `<label>${label}<input name="${name}" ${attrs} required></label>`;
const amountFields = () => amounts.map(([name,label]) => field(name,label,'type="number" min="0" max="10000000" step="1"')).join('');

function input(item) {
  return `<form class="settlement-form" data-settlement="${item.pilot_id}" data-action="submit">
    ${field('pos_reference','结算凭证号','maxlength="80"')}
    ${field('tickets_sold','正式排片售出座位','type="number" min="0" max="10000" step="1"')}
    ${amountFields()}
    ${field('notes','票房和成本口径','minlength="5" maxlength="400"')}
    <button class="button primary" type="submit">登记场次结算</button></form>`;
}

function correctForm(item) {
  const row = item.settlement;
  return `<details class="settlement-correct"><summary>登记补正 · 基于当前 v${row.version}</summary>
    <form class="settlement-form" data-settlement="${item.pilot_id}" data-action="correct">
    <input type="hidden" name="expected_settlement_id" value="${row.id}">
    ${field('pos_reference','补正 POS 凭证号','maxlength="80"')}
    ${field('tickets_sold','补正售出座位','type="number" min="0" max="10000" step="1"')}
    ${amountFields()}
    ${field('notes','补正依据说明','minlength="5" maxlength="400"')}
    <button class="button primary" type="submit">提交补正并一次性切换利用率</button></form></details>`;
}

function review(item) {
  const row = item.settlement;
  return `<form class="settlement-form" data-settlement="${item.pilot_id}" data-action="review">
    <input type="hidden" name="expected_settlement_id" value="${row.id}">
    <label>收益复核决定<select name="decision"><option value="approved">确认收益</option><option value="rejected">驳回结算</option></select></label>
    ${field('note','复核意见','minlength="5" maxlength="400"')}
    <button class="button ink" type="submit">提交复核</button></form>`;
}

function sourceRow(item) {
  const row = item.settlement;
  return `<div class="settlement-sources">
    <div><small>排片回执实投</small><strong>${item.seats} 座</strong></div>
    <div><small>实验单元观测售出</small><strong>${item.observed.sold} / ${item.observed.observed}</strong></div>
    <div><small>POS 售出</small><strong>${row ? row.tickets_sold : '—'}</strong></div></div>`;
}

function diffNotice(item) {
  if (!item.alignment?.length) return '';
  const text = item.alignment.map(source => `POS 售出超过${sourceLabels[source]}`).join('；');
  return `<p class="settlement-diff">差异来源：${text}，暂缓复核。</p>`;
}

function history(item) {
  if (!item.history?.length) return '';
  const rows = item.history.map(v => `<li class="${v.is_current ? 'current' : ''}">
    <b>v${v.version}</b> · ${status[v.state]}${v.is_current ? ' · 当前版本' : ''}
    <p>售出 ${v.tickets_sold}/${v.seats_offered} · 利用率 ${pct(v.utilization)} · 净贡献 ${money(v.contribution_cents)}</p>
    <small>${escape(v.pos_reference)} · ${escape(v.notes)}${v.review_note ? ` · 复核：${escape(v.review_note)}` : ''}</small>
    ${v.diff_sources?.length ? `<small>登记时差异来源：${v.diff_sources.map(s => sourceLabels[s]).join('、')}</small>` : ''}
  </li>`).join('');
  return `<details class="settlement-history"><summary>历史明细 · ${item.history.length} 版</summary><ol>${rows}</ol></details>`;
}

export function settlementPage(state) {
  const {role} = state.user;
  const options = state.pilotSettlements ?? [];
  const cards = options.map(item => {
    const row = item.settlement;
    let details = item.can_submit ? '<p>已开场，可由票房岗位登记实际结算。</p>' : '<p>等待正式排片开场。</p>';
    if (!row && item.can_submit && ['boxoffice','admin'].includes(role)) details += input(item);
    if (row) {
      details = `${sourceRow(item)}${diffNotice(item)}<div class="settlement-metrics">
        <div><small>实际售出</small><strong>${row.tickets_sold} / ${row.seats_offered}</strong></div>
        <div><small>座位利用率</small><strong>${pct(row.utilization)}</strong></div>
        <div><small>场次净贡献</small><strong>${money(row.contribution_cents)}</strong></div>
        <div><small>盈亏平衡门槛</small><strong>${row.breakeven_tickets ?? '票务净额为零'}</strong></div></div>
        <dl class="settlement-breakdown">${amounts.map(([name,label]) => `<div><dt>${label.replace('（分）','')}</dt><dd>${money(row[name])}</dd></div>`).join('')}</dl>
        <p>${escape(row.pos_reference)} · ${escape(row.notes)}</p><p><b>${status[row.state]}</b> ${escape(row.review_note ?? '')}</p>`;
      if (row.state === 'held') details += '<p class="settlement-held">三方对账未对齐，暂缓复核，需票房补正对齐后再审。</p>';
      if (row.state === 'submitted' && ['revenue','admin'].includes(role) && Number(state.user.id) !== row.submitted_by) details += review(item);
      if (item.can_correct && ['boxoffice','admin'].includes(role)) details += correctForm(item);
      details += history(item);
    }
    return `<article class="panel pilot-settlement"><h3>${escape(item.hall)} · ${escape(item.receipt)}</h3>
      <p>${escape(item.show_date)} ${escape(item.show_time)} · 可售 ${item.seats} 座</p>${details}</article>`;
  });
  return `<section class="settlement-board">${cards.join('') || '<p class="panel">此排片版本暂无已确认的试行场次。</p>'}</section>`;
}
