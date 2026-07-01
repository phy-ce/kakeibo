// 미분류 배지 업데이트
document.addEventListener('DOMContentLoaded', () => {
  fetch('/uncategorized').then(r => r.text()).then(html => {
    const match = html.match(/badge bg-danger">(\d+)</);
    if (match && parseInt(match[1]) > 0) {
      const badge = document.getElementById('uncategorized-badge');
      if (badge) { badge.textContent = match[1]; badge.style.display = ''; }
    }
  }).catch(() => {});
});
