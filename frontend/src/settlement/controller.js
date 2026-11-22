import {request} from '../api.js';

const section = id => `/plans/${id}/pilot-settlements`;
const routes = {submit: '', correct: '/corrections', review: '/review'};
const messages = {submit: '票房结算已登记', correct: '补正已登记，利用率已一次性切换', review: '收益复核已记录'};

export async function loadSettlements(state) {
  if (state.current !== 'settlements') return;
  const id = state.selectedPlan?.id;
  state.pilotSettlements = id ? (await request(section(id))).items : [];
}

export function bindSettlement(state, render, run, setMessage) {
  if (state.current !== 'settlements') return;
  for (const form of document.querySelectorAll('form[data-settlement]')) {
    form.addEventListener('submit', event => {
      event.preventDefault();
      run(async () => {
        const button = form.querySelector('button');
        button.disabled = true;
        try {
          const action = form.dataset.action || 'submit';
          const body = Object.fromEntries(new FormData(form));
          for (const name of Object.keys(body)) {
            if (name.endsWith('_cents') || name === 'tickets_sold' || name === 'expected_settlement_id') body[name] = Number(body[name]);
          }
          const route = `${section(state.selectedPlan.id)}/${form.dataset.settlement}${routes[action]}`;
          const result = await request(route, {method:'POST', body:JSON.stringify(body)});
          await loadSettlements(state);
          render();
          setMessage(result?.state === 'held' ? '已登记：三方对账未对齐，暂缓复核' : messages[action]);
        } finally {
          button.disabled = false;
        }
      });
    });
  }
}
