// Shared category cascade for the AI review pages (receipt / mail).
//
// Each `.review-item` carries the AI-suggested values in data-major/minor/sub.
// The selects are (re)built here so that every option is selectable: the
// server used to render the suggested value as the *only* <option> of the
// minor/sub selects, which made the category impossible to edit.
//
// Requires: global `allCats` (rows of the categories table) and `catLabel()`.

function reviewEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Build a select from `values`, keeping `current` selected even when the AI
// returned a category that is not in the scheme (it is added to the top).
// The empty option shows the select's data-placeholder so an unset category
// reads as "대분류/소분류/세부" instead of an anonymous blank.
function reviewFill(sel, values, current) {
  if (!sel) return;
  const vals = values.filter(v => v !== '' && v != null);
  const cur = current || '';
  if (cur && vals.indexOf(cur) === -1) vals.unshift(cur);
  const blank = reviewEsc(sel.dataset.placeholder || '');
  sel.innerHTML = `<option value="">${blank}</option>` + vals.map(v =>
    `<option value="${reviewEsc(v)}">${reviewEsc(catLabel(v))}</option>`).join('');
  sel.value = cur;
  sel.classList.toggle('is-unset', !cur);
}

function reviewCats(item) {
  const type = item.querySelector('.field-type');
  const want = type ? type.value : '';
  // Fall back to all rows if the type has no categories of its own.
  const rows = allCats.filter(c => !want || c.type === want);
  return rows.length ? rows : allCats;
}

// type -> major. `keep` = value to preselect (AI suggestion on first render).
function reviewLoadMajor(typeSel, keep) {
  const item = typeSel.closest('.review-item');
  const majorSel = item.querySelector('.field-major');
  const majors = [...new Set(reviewCats(item).map(c => c.major))];
  reviewFill(majorSel, majors, keep !== undefined ? keep : majorSel.value);
  reviewLoadMinor(majorSel, keep !== undefined ? item.dataset.minor : undefined);
}

// major -> minor
function reviewLoadMinor(majorSel, keep) {
  const item = majorSel.closest('.review-item');
  const major = majorSel.value;
  const minorSel = item.querySelector('.field-minor');
  const minors = [...new Set(reviewCats(item).filter(c => c.major === major).map(c => c.minor))];
  reviewFill(minorSel, minors, keep !== undefined ? keep : '');
  reviewLoadSub(minorSel, keep !== undefined ? item.dataset.sub : undefined);
}

// minor -> sub
function reviewLoadSub(minorSel, keep) {
  const item = minorSel.closest('.review-item');
  const major = item.querySelector('.field-major').value;
  const minor = minorSel.value;
  const subs = reviewCats(item)
    .filter(c => c.major === major && c.minor === minor).map(c => c.sub);
  reviewFill(item.querySelector('.field-sub'), [...new Set(subs)], keep !== undefined ? keep : '');
}

// Expense red / income green, so a sign typo is visible at a glance.
function reviewColorAmount(input) {
  const v = parseFloat(input.value);
  input.classList.toggle('amt-expense', v < 0);
  input.classList.toggle('amt-income', v > 0);
}

// Build every row's selects from its AI suggestion.
function reviewInitItems(root) {
  (root || document).querySelectorAll('.review-item').forEach(item => {
    reviewLoadMajor(item.querySelector('.field-type'), item.dataset.major || '');
    const amount = item.querySelector('.field-amount');
    if (amount) {
      reviewColorAmount(amount);
      amount.addEventListener('input', () => reviewColorAmount(amount));
    }
  });
}

document.addEventListener('DOMContentLoaded', () => reviewInitItems(document));

// Runs after the inline onchange rebuilt the child selects: mark still-empty ones.
document.addEventListener('change', e => {
  const el = e.target;
  if (el && el.matches && el.matches('.field-major, .field-minor, .field-sub')) {
    el.classList.toggle('is-unset', !el.value);
  }
});

// Shop / item autocomplete on every review row (suggest.js, loaded by base.html)
if (typeof attachReviewSuggest === 'function') attachReviewSuggest();
