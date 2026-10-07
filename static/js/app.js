// Popcorn's scripts. Each part only runs on pages that have the elements it needs.
(() => {
  const fetchHeaders = { 'X-Requested-With': 'fetch' };

  // --- Forms that need a second thought, e.g. removing a friend: <form data-confirm="Question?">. ---
  // Registered first (and capturing) so a cancelled form never reaches the handlers below.
  document.addEventListener('submit', (event) => {
    const question = (event.submitter && event.submitter.dataset.confirm) || event.target.dataset.confirm;
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

  // --- Search a list as you type. In a grouped list, groups with matches open and the others hide. ---
  document.querySelectorAll('input[data-filter]').forEach((input) => {
    const rows = document.querySelectorAll(input.dataset.filter);
    const groups = document.querySelectorAll('details.group');
    groups.forEach((group) => { group.dataset.open = group.open; });
    input.addEventListener('input', () => {
      const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      rows.forEach((row) => { row.hidden = !words.every((w) => row.dataset.search.includes(w)); });
      groups.forEach((group) => {
        group.hidden = words.length > 0 && !group.querySelector('.entry-row:not([hidden])');
        group.open = words.length > 0 || group.dataset.open === 'true';
      });
    });
  });

  // --- Finding a title on TMDB as you type: <div class="title-search" data-search-url data-kind>. ---
  // Picking one fills the form's tmdb_id, kind, title and year (those it has) and sends it. In the add
  // form, Enter without picking adds the title as typed; in a search-only form (input "q") it picks the first.
  document.querySelectorAll('.title-search').forEach((box) => {
    const input = box.querySelector('input');
    const list = box.querySelector('.suggestions');
    const form = input.form;
    const pickOnly = input.name === 'q';
    let timer = null;
    let latest = 0;

    const close = () => { list.hidden = true; list.replaceChildren(); };
    const submit = (result, season) => {
      ['tmdb_id', 'kind', 'title', 'year'].forEach((name) => {
        const field = form.elements[name];
        if (field) field.value = result[name] ?? '';
      });
      if (form.elements.season) form.elements.season.value = season ?? '';
      close();
      form.requestSubmit();
    };
    const option = (label, detail, onList, onClick) => {
      const button = Object.assign(document.createElement('button'), { type: 'button', className: 'suggestion' });
      const text = Object.assign(document.createElement('span'), { className: 'suggestion-text' });
      const meta = [detail, onList ? 'on your list' : ''].filter(Boolean).join(' · ');
      text.append(Object.assign(document.createElement('b'), { textContent: label }),
        Object.assign(document.createElement('span'), { className: 'muted small', textContent: meta }));
      button.append(text);
      button.addEventListener('click', onClick);
      const item = document.createElement('li');
      item.append(button);
      return item;
    };
    // A show in a form that takes a season: choose the whole show or one of its seasons first.
    const chooseSeason = async (result) => {
      let data = { seasons: [], on_list: [] };
      try {
        const url = new URL(box.dataset.seasonsUrl, location.href);
        url.searchParams.set('tmdb_id', result.tmdb_id);
        data = await (await fetch(url, { headers: fetchHeaders })).json();
      } catch (e) { /* offer the whole show only */ }
      if (!data.seasons.length) return submit(result, null);
      const heading = Object.assign(document.createElement('li'), { className: 'suggestions-heading', textContent: `${result.title}: add` });
      list.replaceChildren(
        heading,
        option('The whole show', 'as it is now', data.on_list.includes(null), () => submit(result, null)),
        ...data.seasons.map((s) => option(
          s.name,
          [s.year, s.episodes ? `${s.episodes} episodes` : ''].filter(Boolean).join(' · '),
          data.on_list.includes(s.number),
          () => submit(result, s.number),
        )),
      );
      list.hidden = false;
      list.querySelector('button').focus();
    };
    const pick = (result) => {
      if (result.kind === 'show' && form.elements.season && box.dataset.seasonsUrl) chooseSeason(result);
      else submit(result, null);
    };
    const render = (results) => {
      list.replaceChildren(...results.map((result) => {
        const button = Object.assign(document.createElement('button'), { type: 'button', className: 'suggestion' });
        const poster = result.poster
          ? Object.assign(document.createElement('img'), { src: result.poster, alt: '', width: 30, height: 45, loading: 'lazy' })
          : Object.assign(document.createElement('span'), { className: 'poster none', textContent: result.kind === 'movie' ? '🎬' : '📺' });
        const text = document.createElement('span');
        text.className = 'suggestion-text';
        const title = Object.assign(document.createElement('b'), { textContent: result.title });
        const kind = result.kind === 'movie' ? 'Movie' : 'TV show';
        const meta = Object.assign(document.createElement('span'), {
          className: 'muted small',
          textContent: [result.year, box.dataset.kind ? '' : kind, result.on_list ? 'on your list' : ''].filter(Boolean).join(' · '),
        });
        text.append(title, meta);
        button.append(poster, text);
        button.addEventListener('click', () => pick(result));
        const item = document.createElement('li');
        item.append(button);
        return item;
      }));
      list.hidden = results.length === 0;
    };
    const lookup = async () => {
      const query = input.value.trim();
      if (query.length < 2) return close();
      const url = new URL(box.dataset.searchUrl, location.href);
      url.searchParams.set('q', query);
      if (box.dataset.kind) url.searchParams.set('kind', box.dataset.kind);
      const mine = ++latest;
      try {
        const response = await fetch(url, { headers: fetchHeaders });
        const data = await response.json();
        if (mine === latest) render(data.results || []);
      } catch (e) {
        close();
      }
    };

    input.addEventListener('input', () => {
      if (form.elements.tmdb_id) form.elements.tmdb_id.value = '';
      clearTimeout(timer);
      timer = setTimeout(lookup, 250);
    });
    input.addEventListener('keydown', (event) => {
      const first = list.querySelector('button');
      if (event.key === 'ArrowDown' && first) { event.preventDefault(); first.focus(); }
      if (event.key === 'Escape') close();
      if (event.key === 'Enter' && pickOnly) {
        event.preventDefault();
        if (first && !list.hidden) first.click(); else lookup();
      }
    });
    list.addEventListener('keydown', (event) => {
      const buttons = [...list.querySelectorAll('button')];
      const i = buttons.indexOf(document.activeElement);
      if (event.key === 'ArrowDown') { event.preventDefault(); (buttons[i + 1] || buttons[i]).focus(); }
      if (event.key === 'ArrowUp') { event.preventDefault(); (i > 0 ? buttons[i - 1] : input).focus(); }
      if (event.key === 'Escape') { close(); input.focus(); }
    });
    document.addEventListener('click', (event) => { if (!box.contains(event.target)) close(); });

    // Searches for the prefilled title straight away (or once its section is opened).
    if (input.hasAttribute('data-search-now')) {
      const section = box.closest('details');
      if (section) section.addEventListener('toggle', () => { if (section.open) lookup(); });
      else lookup();
    }
  });

  // --- Badges with suggestions, e.g. genres and where to watch: <div class="token-input" data-options="#json">
  // around a text input of comma-separated values, which stays what the form sends. Picking a suggestion or
  // typing a name and pressing Enter (or a comma) adds a badge; × or Backspace removes one. data-locked="#json":
  // badges shown but not removable or sent (TMDB's services while synced); unticking the form's
  // providers_sync makes them ordinary badges. ---
  document.querySelectorAll('.token-input').forEach((box) => {
    const field = box.querySelector('input');
    const source = document.querySelector(box.dataset.options);
    const options = source ? JSON.parse(source.textContent) : [];
    const known = (name) => options.find((o) => o.name.toLowerCase() === name.toLowerCase());
    const logoUrl = (logo) => `https://image.tmdb.org/t/p/w45${logo}`;
    let values = field.value.split(',').map((v) => v.trim()).filter(Boolean);
    const lockedSource = box.dataset.locked && document.querySelector(box.dataset.locked);
    const lockedAtStart = lockedSource ? JSON.parse(lockedSource.textContent) : [];
    let locked = lockedAtStart;
    const isLocked = (name) => locked.some((p) => p.name.toLowerCase() === name.toLowerCase());
    let shown = [];
    let active = -1;

    const tokens = Object.assign(document.createElement('span'), { className: 'tokens' });
    const input = Object.assign(document.createElement('input'), {
      type: 'text', autocomplete: 'off', placeholder: field.placeholder,
    });
    input.setAttribute('aria-label', field.getAttribute('aria-label') || '');
    const menu = Object.assign(document.createElement('ul'), { className: 'suggestions', hidden: true });
    field.type = 'hidden';
    box.append(tokens, input, menu);

    const sync = () => {
      field.value = values.join(', ');
      input.placeholder = values.length ? '' : field.placeholder;
    };
    const badge = (name, logo) => {
      const parts = [];
      if (logo) parts.push(Object.assign(document.createElement('img'), { src: logoUrl(logo), alt: '', width: 18, height: 18 }));
      parts.push(document.createTextNode(name));
      return parts;
    };
    const renderTokens = () => {
      const fixed = locked.map((p) => {
        const token = Object.assign(document.createElement('span'), { className: 'token locked', title: 'From TMDB, kept up to date' });
        token.append(...badge(p.name, p.logo));
        return token;
      });
      tokens.replaceChildren(...fixed, ...values.map((name, i) => {
        const token = Object.assign(document.createElement('span'), { className: 'token' });
        const remove = Object.assign(document.createElement('button'), { type: 'button', className: 'token-remove', textContent: '×' });
        remove.setAttribute('aria-label', `Remove ${name}`);
        remove.addEventListener('click', () => { values.splice(i, 1); update(); input.focus(); });
        token.append(...badge(name, box.hasAttribute('data-logos') && (known(name) || {}).logo), remove);
        return token;
      }));
    };
    const renderMenu = () => {
      const query = input.value.trim().toLowerCase();
      const taken = new Set([...values, ...locked.map((p) => p.name)].map((v) => v.toLowerCase()));
      const matches = options
        .filter((o) => !taken.has(o.name.toLowerCase()) && o.name.toLowerCase().includes(query))
        .sort((a, b) => (query && b.name.toLowerCase().startsWith(query)) - (query && a.name.toLowerCase().startsWith(query)))
        .slice(0, 8);
      shown = matches.map((o) => ({ name: o.name, logo: o.logo }));
      if (query && !known(query) && !taken.has(query)) shown.push({ name: input.value.trim(), custom: true });
      active = query && shown.length ? 0 : -1;
      menu.replaceChildren(...shown.map((option, i) => {
        const item = document.createElement('li');
        const button = Object.assign(document.createElement('button'), { type: 'button', className: 'suggestion' + (i === active ? ' active' : '') });
        if (option.custom) button.textContent = `Add “${option.name}”`;
        else button.append(...badge(option.name, option.logo));
        button.addEventListener('mousedown', (event) => event.preventDefault());  // keep the focus in the input
        button.addEventListener('click', () => add(option.name));
        item.append(button);
        return item;
      }));
      menu.hidden = shown.length === 0 || document.activeElement !== input;
    };
    const highlight = (i) => {
      active = i;
      menu.querySelectorAll('.suggestion').forEach((b, j) => b.classList.toggle('active', j === i));
      if (menu.children[i]) menu.children[i].scrollIntoView({ block: 'nearest' });
    };
    const update = () => { sync(); renderTokens(); renderMenu(); };
    const add = (name) => {
      name = name.replace(/,/g, ' ').replace(/\s+/g, ' ').trim();
      if (!name) return;
      name = (known(name) || { name }).name;  // as the suggestion spells it
      if (!isLocked(name) && !values.some((v) => v.toLowerCase() === name.toLowerCase())) values.push(name);
      input.value = '';
      update();
    };

    input.addEventListener('input', () => {
      if (input.value.includes(',')) {
        input.value.split(',').slice(0, -1).forEach(add);
        input.value = input.value.split(',').pop();
      }
      renderMenu();
    });
    input.addEventListener('focus', renderMenu);
    input.addEventListener('blur', () => { add(input.value); menu.hidden = true; });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        if (!input.value.trim() && active < 0) return;  // nothing typed: Enter sends the form
        event.preventDefault();
        add(active >= 0 ? shown[active].name : input.value);
      } else if (event.key === 'ArrowDown' && shown.length) {
        event.preventDefault();
        menu.hidden = false;
        highlight(Math.min(active + 1, shown.length - 1));
      } else if (event.key === 'ArrowUp' && shown.length) {
        event.preventDefault();
        highlight(Math.max(active - 1, 0));
      } else if (event.key === 'Escape') {
        menu.hidden = true;
      } else if (event.key === 'Backspace' && !input.value && values.length) {
        values.pop();
        update();
      }
    });
    box.addEventListener('click', (event) => { if (event.target === box || event.target === tokens) input.focus(); });
    const syncSwitch = field.form && field.form.elements.providers_sync;
    if (syncSwitch && lockedAtStart.length) {
      // Unticked: TMDB's become ordinary badges, and the form says the field now has all of them.
      const complete = Object.assign(document.createElement('input'), { type: 'hidden', name: 'providers_complete', value: '1', disabled: true });
      box.append(complete);
      syncSwitch.addEventListener('change', () => {
        const names = lockedAtStart.map((p) => p.name);
        complete.disabled = syncSwitch.checked;
        if (syncSwitch.checked) {
          locked = lockedAtStart;
          values = values.filter((v) => !names.some((n) => n.toLowerCase() === v.toLowerCase()));
        } else {
          locked = [];
          values = [...names, ...values.filter((v) => !names.some((n) => n.toLowerCase() === v.toLowerCase()))];
        }
        update();
      });
    }
    update();
    menu.hidden = true;
  });

  // --- An import being read: wait for it, then show what it found. ---
  const importing = document.querySelector('#import-running[data-status-url]');
  if (importing) {
    const poll = async () => {
      try {
        const response = await fetch(importing.dataset.statusUrl, { headers: fetchHeaders });
        if ((await response.json()).finished) {
          location.hash = 'pending';
          location.reload();
          return;
        }
      } catch (e) { /* try again */ }
      setTimeout(poll, 2500);
    };
    setTimeout(poll, 2500);
  }

  // --- "Show all": <button data-show-more=".selector"> reveals the elements hidden with class "more". ---
  document.querySelectorAll('[data-show-more]').forEach((button) => button.addEventListener('click', () => {
    document.querySelectorAll(button.dataset.showMore).forEach((el) => el.classList.remove('more'));
    button.remove();
  }));

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
