// Popcorn's scripts. Each part only runs on pages that have the elements it needs.
(() => {
  const fetchHeaders = { 'X-Requested-With': 'fetch' };

  // --- Forms that need a second thought, e.g. removing a friend: <form data-confirm="Question?">. ---
  // Registered first (and capturing) so a cancelled form never reaches the handlers below.
  document.addEventListener('submit', (event) => {
    const question = event.target.dataset.confirm;
    if (question && !window.confirm(question)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);

  // --- Stars on your own list: saved in the background, no page reload. Tapping the rating again clears it. ---
  document.addEventListener('submit', async (event) => {
    const form = event.target.closest('.rate');
    if (!form) return;
    event.preventDefault();
    const button = event.submitter;
    const data = new FormData(form);
    data.set('rating', button.value);
    try {
      const response = await fetch(form.action, { method: 'POST', body: data, headers: fetchHeaders });
      if (!response.ok) throw new Error(response.status);
      const { rating, status } = await response.json();
      form.querySelectorAll('.star-btn').forEach((b) => {
        const n = Number(b.value);
        b.classList.toggle('on', rating !== null && n <= rating);
        b.setAttribute('aria-pressed', n === rating);
      });
      const row = form.closest('.entry-row');
      if (row) row.className = row.className.replace(/status-\w+/, 'status-' + status);
    } catch (e) {
      // Fall back to a normal form post (which reloads the page).
      form.append(Object.assign(document.createElement('input'), { type: 'hidden', name: 'rating', value: button.value }));
      form.submit();
    }
  });

  // --- Search a list as you type. ---
  document.querySelectorAll('input[data-filter]').forEach((input) => {
    const rows = document.querySelectorAll(input.dataset.filter);
    input.addEventListener('input', () => {
      const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      rows.forEach((row) => { row.hidden = !words.every((w) => row.dataset.search.includes(w)); });
    });
  });

  // --- Copy (or, on phones, share) a link: <button data-copy="#input-id">. ---
  document.querySelectorAll('[data-copy]').forEach((button) => {
    const input = document.querySelector(button.dataset.copy);
    const title = button.dataset.shareTitle;
    if (title && navigator.share && matchMedia('(pointer: coarse)').matches) button.textContent = 'Share';
    button.addEventListener('click', async () => {
      if (button.textContent === 'Share') {
        try { await navigator.share({ title, url: input.value }); } catch (e) { /* cancelled */ }
        return;
      }
      try {
        await navigator.clipboard.writeText(input.value);
      } catch (e) {
        input.select();
        document.execCommand('copy');
      }
      button.textContent = 'Copied ✓';
      setTimeout(() => { button.textContent = 'Copy'; }, 2000);
    });
  });
  // A read-only link selects itself when tapped, ready to copy by hand.
  document.querySelectorAll('.copy-row input[readonly]').forEach((input) => input.addEventListener('focus', () => input.select()));

  // --- Switches that save as soon as they're flipped. ---
  document.querySelectorAll('[data-autosubmit]').forEach((input) => input.addEventListener('change', () => input.form.submit()));

  // --- Installable app: pass-through service worker. ---
  const swUrl = document.body.dataset.swUrl;
  if (swUrl && 'serviceWorker' in navigator) navigator.serviceWorker.register(swUrl);
})();
