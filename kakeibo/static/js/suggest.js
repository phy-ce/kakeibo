// Autocomplete for shop / item inputs, backed by /api/suggest (past values, most used first).
//
// attachSuggest(input, field, opts)
//   field        'shop' | 'item'
//   opts.shopOf  () => current shop, so item suggestions of that shop come first
//   opts.onPick  (entry) => ...  called when a suggested item is chosen; entry has the
//                latest {type, major, minor, sub, amount} used with it
//
// Uses a native <datalist>: keyboard navigation and mobile pickers come for free.

let suggestSeq = 0;

function attachSuggest(input, field, opts) {
  if (!input || input.dataset.suggest) return;
  opts = opts || {};
  input.dataset.suggest = field;
  input.setAttribute('autocomplete', 'off');     // the browser's own history would compete

  const list = document.createElement('datalist');
  list.id = 'suggest-' + (++suggestSeq);
  document.body.appendChild(list);
  input.setAttribute('list', list.id);

  let meta = {};         // value -> suggestion entry of the current list
  let timer = null;
  let lastQuery = null;

  async function refresh() {
    const shop = opts.shopOf ? (opts.shopOf() || '') : '';
    const key = input.value + '\u0000' + shop;
    if (key === lastQuery) return;
    lastQuery = key;
    const params = new URLSearchParams({field, q: input.value.trim(), shop});
    let rows = [];
    try {
      rows = await fetch('/api/suggest?' + params).then(r => r.json());
    } catch (e) { return; }
    if (!Array.isArray(rows)) return;
    meta = {};
    list.replaceChildren(...rows.map(r => {
      meta[r.value] = r;
      const o = document.createElement('option');
      o.value = r.value;          // textContent-free: values are never parsed as HTML
      return o;
    }));
  }

  input.addEventListener('focus', refresh);
  input.addEventListener('input', e => {
    // Picking an option fires `input` too; report it before refreshing the list
    if (meta[input.value] && (!e.inputType || e.inputType === 'insertReplacementText')) {
      if (opts.onPick) opts.onPick(meta[input.value]);
    }
    clearTimeout(timer);
    timer = setTimeout(refresh, 150);
  });
}

// Review pages: rows are many and some are added later, so attach on first focus.
function attachReviewSuggest() {
  document.addEventListener('focusin', e => {
    const el = e.target;
    if (!el.matches) return;
    const row = el.closest('.review-item');
    if (!row) return;
    if (el.matches('.field-shop')) attachSuggest(el, 'shop');
    if (el.matches('.field-item')) {
      attachSuggest(el, 'item', {
        shopOf: () => row.querySelector('.field-shop')?.value,
        onPick: s => {
          // Only fill a category the row does not have yet; never override the user
          if (!s.major || row.querySelector('.field-major').value) return;
          const typeSel = row.querySelector('.field-type');
          if (s.type && typeSel) typeSel.value = s.type;
          row.dataset.minor = s.minor;
          row.dataset.sub = s.sub;
          reviewLoadMajor(typeSel, s.major);
        },
      });
    }
  });
}
